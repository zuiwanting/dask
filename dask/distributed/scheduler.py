from __future__ import print_function

import zmq
import socket
import dill
import uuid
from collections import defaultdict
import itertools
from multiprocessing.pool import ThreadPool
import random
from datetime import datetime
from threading import Thread, Lock
from contextlib import contextmanager
from toolz import curry, partial
from ..compatibility import Queue, unicode
try:
    import cPickle as pickle
except ImportError:
    import pickle

from ..core import get_dependencies, flatten
from .. import core
from ..async import finish_task, start_state_from_dask as dag_state_from_dask

with open('log.scheduler', 'w') as f:  # delete file
    pass

def log(*args):
    with open('log.scheduler', 'a') as f:
        print(*args, file=f)

@contextmanager
def logerrors():
    try:
        yield
    except Exception as e:
        log('Error!', str(e))
        raise

class Scheduler(object):
    """ Disitributed scheduler for dask computations

    State
    -----

    workers - dict
        Maps worker identities to information about that worker
    who_has - dict
        Maps data keys to sets of workers that own that data
    worker_has - dict
        Maps workers to data that they own
    data - dict
        Maps data keys to metadata about the computation that produced it
    to_workers - zmq.Socket (ROUTER)
        Socket to communicate to workers
    to_clients - zmq.Socket (ROUTER)
        Socket to communicate with users
    address_to_workers - string
        ZMQ address of our connection to workers
    address_to_clients - string
        ZMQ address of our connection to clients
    """
    def __init__(self, address_to_workers=None, address_to_clients=None):
        self.context = zmq.Context()
        hostname = socket.gethostname()

        # Bind routers to addresses (and create addresses if necessary)
        self.to_workers = self.context.socket(zmq.ROUTER)
        if address_to_workers is None:
            port = self.to_workers.bind_to_random_port('tcp://*')
            self.address_to_workers = ('tcp://%s:%d' % (hostname, port)).encode()
        else:
            if isinstance(address_to_workers, unicode):
                address_to_workers = address_to_workers.encode()
            self.address_to_workers = address_to_workers
            self.to_workers.bind(self.address_to_workers)

        self.to_clients = self.context.socket(zmq.ROUTER)
        if address_to_clients is None:
            port = self.to_clients.bind_to_random_port('tcp://*')
            self.address_to_clients = ('tcp://%s:%d' % (hostname, port)).encode()
        else:
            if isinstance(address_to_clients, unicode):
                address_to_clients = address_to_clients.encode()
            self.address_to_clients = address_to_clients
            self.to_clients.bind(self.address_to_clients)

        # State about my workers and computed data
        self.workers = dict()
        self.who_has = defaultdict(set)
        self.worker_has = defaultdict(set)
        self.available_workers = Queue()
        self.data = defaultdict(dict)

        self.pool = ThreadPool(100)
        self.lock = Lock()
        self.status = 'run'
        self.queues = dict()

        # RPC functions that workers and clients can trigger
        self.worker_functions = {'register': self.worker_registration,
                                 'status': self.status_to_worker,
                                 'finished-task': self.worker_finished_task,
                                 'setitem-ack': self.setitem_ack,
                                 'getitem-ack': self.getitem_ack}
        self.client_functions = {'status': self.status_to_client,
                                 'schedule': self.schedule_from_client}

        # Away we go!
        log(self.address_to_workers, 'Start')
        self._listen_to_workers_thread = Thread(target=self.listen_to_workers)
        self._listen_to_workers_thread.start()
        self._listen_to_clients_thread = Thread(target=self.listen_to_clients)
        self._listen_to_clients_thread.start()

        self.active_tasks = set()

    def listen_to_workers(self):
        """ Event loop: Listen to worker router """
        while self.status != 'closed':
            if not self.to_workers.poll(100):
                continue
            address, header, payload = self.to_workers.recv_multipart()

            header = pickle.loads(header)
            if 'address' not in header:
                header['address'] = address
            log(self.address_to_workers, 'Receive job from worker', header)

            try:
                function = self.worker_functions[header['function']]
            except KeyError:
                log(self.address_to_workers, 'Unknown function', header)
            else:
                future = self.pool.apply_async(function, args=(header, payload))

    def listen_to_clients(self):
        """ Event loop: Listen to client router """
        while self.status != 'closed':
            if not self.to_clients.poll(100):
                continue
            address, header, payload = self.to_clients.recv_multipart()
            header = pickle.loads(header)
            if 'address' not in header:
                header['address'] = address
            log(self.address_to_clients, 'Receive job from client', header)

            try:
                function = self.client_functions[header['function']]
            except KeyError:
                log(self.address_to_clients, 'Unknown function', header)
            else:
                self.pool.apply_async(function, args=(header, payload))

    def worker_registration(self, header, payload):
        """ Worker came in, register them """
        payload = pickle.loads(payload)
        address = header['address']
        self.workers[address] = payload
        self.available_workers.put(address)

    def worker_finished_task(self, header, payload):
        """ Worker reports back as having finished task, ready for more

        See also:
            Scheduler.trigger_task
            Scheduler.schedule
        """
        with logerrors():
            address = header['address']

            payload = pickle.loads(payload)
            key = payload['key']
            duration = payload['duration']
            dependencies = payload['dependencies']

            log(self.address_to_workers, 'Finish task', payload)
            self.active_tasks.remove(key)

            self.data[key]['duration'] = duration
            self.who_has[key].add(address)
            self.worker_has[address].add(key)
            for dep in dependencies:
                self.who_has[dep].add(address)
                self.worker_has[address].add(dep)
            self.available_workers.put(address)

            self.queues[payload['queue']].put(payload)

    def status_to_client(self, header, payload):
        with logerrors():
            out_header = {'jobid': header.get('jobid')}
            log(self.address_to_clients, 'Status')
            self.send_to_client(header['address'], out_header, 'OK')

    def status_to_worker(self, header, payload):
        out_header = {'jobid': header.get('jobid')}
        log(self.address_to_workers, 'Status sending')
        self.send_to_worker(header['address'], out_header, 'OK')

    def send_to_worker(self, address, header, payload):
        """ Send packet to worker """
        log(self.address_to_workers, 'Send to worker', address, header)
        header['address'] = self.address_to_workers
        loads = header.get('loads', pickle.loads)
        dumps = header.get('dumps', pickle.dumps)
        if isinstance(address, unicode):
            address = address.encode()
        header['timestamp'] = datetime.utcnow()
        with self.lock:
            self.to_workers.send_multipart([address,
                                            pickle.dumps(header),
                                            dumps(payload)])

    def send_to_client(self, address, header, result):
        """ Send packet to client """
        log(self.address_to_clients, 'Send to client', address, header)
        header['address'] = self.address_to_clients
        loads = header.get('loads', pickle.loads)
        dumps = header.get('dumps', pickle.dumps)
        if isinstance(address, unicode):
            address = address.encode()
        header['timestamp'] = datetime.utcnow()
        with self.lock:
            self.to_clients.send_multipart([address,
                                            pickle.dumps(header),
                                            dumps(result)])

    def trigger_task(self, dsk, key, queue):
        """ Send a single task to the next available worker

        See also:
            Scheduler.schedule
            Scheduler.worker_finished_task
        """
        deps = get_dependencies(dsk, key)
        worker = self.available_workers.get()
        locations = dict((dep, self.who_has[dep]) for dep in deps)

        header = {'function': 'compute', 'jobid': key,
                  'dumps': dill.dumps, 'loads': dill.loads}
        payload = {'key': key, 'task': dsk[key], 'locations': locations,
                   'queue': queue}
        self.send_to_worker(worker, header, payload)
        self.active_tasks.add(key)

    def release_key(self, key):
        """ Release data from all workers

        Example
        -------

        >>> scheduler.release_key('x')  # doctest: +SKIP

        Protocol
        --------

        This sends a 'delitem' request to all workers known to have this key.
        This operation is fire-and-forget.  Local indices will be updated
        immediately.
        """
        with logerrors():
            workers = list(self.who_has[key])
            log(self.address_to_workers, 'Release data', key, workers)
            header = {'function': 'delitem', 'jobid': key}
            payload = {'key': key}
            for worker in workers:
                self.send_to_worker(worker, header, payload)
                self.who_has[key].remove(worker)
                self.worker_has[worker].remove(key)

    def send_data(self, key, value, address=None, reply=True):
        """ Send data up to some worker

        If no address is given we select one worker randomly

        Example
        -------

        >>> scheduler.send_data('x', 10)  # doctest: +SKIP
        >>> scheduler.send_data('x', 10, 'tcp://bob:5000', reply=False)  # doctest: +SKIP

        Protocol
        --------

        1.  Scheduler makes a queue
        2.  Scheduler selects a worker at random (or uses prespecified worker)
        3.  Scheduler sends 'setitem' operation to that worker
            {'key': ..., 'value': ..., 'queue': ...}
        4.  Worker gets data and stores locally, send 'setitem-ack'
            {'key': ..., 'queue': ...}
        5.  Scheduler gets from queue, send_data cleans up queue and returns

        See also:
            Scheduler.setitem_ack
            Worker.setitem
            Scheduler.scatter
        """
        if reply:
            queue = Queue()
            qkey = str(uuid.uuid1())
            self.queues[qkey] = queue
        else:
            qkey = None
        if address is None:
            address = random.choice(list(self.workers))
        header = {'function': 'setitem', 'jobid': key}
        payload = {'key': key, 'value': value, 'queue': qkey}
        self.send_to_worker(address, header, payload)

        if reply:
            queue.get()
            del self.queues[qkey]

    def scatter(self, key_value_pairs, block=True):
        """ Scatter data to workers

        Parameters
        ----------

        key_value_pairs: Iterator or dict
            Data to send
        block: bool
            Block on completion or return immediately (defaults to True)

        Example
        -------

        >>> scheduler.scatter({'x': 1, 'y': 2})  # doctest: +SKIP

        Protocol
        --------

        1.  Scheduler starts up a uniquely identified queue.
        2.  Scheduler sends 'setitem' requests to workers with
            {'key': ..., 'value': ... 'queue': ...}
        3.  Scheduler waits on queue for all responses
        3.  Workers receive 'setitem' requests, send back on 'setitem-ack' with
            {'key': ..., 'queue': ...}
        4.  Scheduler's 'setitem-ack' function pushes keys into the queue
        5.  Once the same number of replies is heard scheduler scatter function
            returns
        6.  Scheduler cleans up queue

        See Also:
            Scheduler.setitem_ack
            Worker.setitem_scheduler
        """
        workers = list(self.workers)
        log(self.address_to_workers, 'Scatter', workers, key_value_pairs)
        workers = itertools.cycle(workers)

        if isinstance(key_value_pairs, dict):
            key_value_pairs = key_value_pairs.items()
        queue = Queue()
        qkey = str(uuid.uuid1())
        self.queues[qkey] = queue
        counter = 0
        for (k, v), w in zip(key_value_pairs, workers):
            header = {'function': 'setitem', 'jobid': k}
            payload = {'key': k, 'value': v}
            if block:
                payload['queue'] = qkey
            self.send_to_worker(w, header, payload)
            counter += 1

        if block:
            for i in range(counter):
                queue.get()

            del self.queues[qkey]

    def gather(self, keys):
        """ Gather data from workers

        Parameters
        ----------

        keys: key, list of keys, nested list of lists of keys
            Keys to collect from workers

        Example
        -------

        >>> scheduler.gather('x')  # doctest: +SKIP
        >>> scheduler.gather([['x', 'y'], ['z']])  # doctest: +SKIP

        Protocol
        --------

        1.  Scheduler starts up a uniquely identified queue.
        2.  Scheduler sends 'getitem' requests to workers with payloads
            {'key': ...,  'queue': ...}
        3.  Scheduler waits on queue for all responses
        3.  Workers receive 'getitem' requests, send data back on 'getitem-ack'
            {'key': ..., 'value': ..., 'queue': ...}
        4.  Scheduler's 'getitem-ack' function pushes key/value pairs onto queue
        5.  Once the same number of replies is heard the gather function
            collects data into form specified by keys input and returns
        6.  Scheduler cleans up queue before returning

        See Also:
            Scheduler.getitem_ack
            Worker.getitem_scheduler
        """
        qkey = str(uuid.uuid1())
        queue = Queue()
        self.queues[qkey] = queue

        # Send of requests
        self._gather_send(qkey, keys)

        # Wait for replies
        cache = dict()
        for i in flatten(keys):
            k, v = queue.get()
            cache[k] = v
        del self.queues[qkey]

        # Reshape to keys
        return core.get(cache, keys)

    def _gather_send(self, qkey, key):
        if isinstance(key, list):
            for k in key:
                self._gather_send(qkey, k)
        else:
            header = {'function': 'getitem', 'jobid': key}
            payload = {'key': key, 'queue': qkey}
            seq = list(self.who_has[key])
            worker = random.choice(seq)
            self.send_to_worker(worker, header, payload)

    def getitem_ack(self, header, payload):
        """ Receive acknowledgement from worker about a getitem request

        See also:
            Scheduler.gather
            Worker.getitem
        """
        payload = pickle.loads(payload)
        log(self.address_to_workers, 'Getitem ack', payload)
        with logerrors():
            assert header['status'] == 'OK'
            self.queues[payload['queue']].put((payload['key'],
                                               payload['value']))

    def setitem_ack(self, header, payload):
        """ Receive acknowledgement from worker about a setitem request

        See also:
            Scheduler.scatter
            Worker.setitem
        """
        address = header['address']
        payload = pickle.loads(payload)
        key = payload['key']
        self.who_has[key].add(address)
        self.worker_has[address].add(key)
        queue = payload.get('queue')
        if queue:
            self.queues[queue].put(key)

    def close(self):
        """ Close Scheduler """
        self.status = 'closed'

    def schedule(self, dsk, result, **kwargs):
        """ Execute dask graph against workers

        Parameters
        ----------

        dsk: dict
            Dask graph
        result: list
            keys to return (possibly nested)

        Example
        -------

        >>> scheduler.get({'x': 1, 'y': (add, 'x', 2)}, 'y')  # doctest: +SKIP
        3

        Protocol
        --------

        1.  Scheduler scatters precomputed data in graph to workers
            e.g. nodes like ``{'x': 1}``.  See Scheduler.scatter
        2.


        """
        if isinstance(result, list):
            result_flat = set(flatten(result))
        else:
            result_flat = set([result])
        results = set(result_flat)

        cache = dict()
        dag_state = dag_state_from_dask(dsk, cache=cache)
        self.scatter(cache.items())  # send data in dask up to workers

        tick = [0]

        if dag_state['waiting'] and not dag_state['ready']:
            raise ValueError("Found no accessible jobs in dask graph")

        event_queue = Queue()
        qkey = str(uuid.uuid1())
        self.queues[qkey] = event_queue

        def fire_task():
            tick[0] += 1  # Update heartbeat

            # Choose a good task to compute
            key = dag_state['ready'].pop()
            dag_state['ready-set'].remove(key)
            dag_state['running'].add(key)

            self.trigger_task(dsk, key, qkey)  # Fire

        # Seed initial tasks
        while dag_state['ready'] and self.available_workers.qsize() > 0:
            fire_task()

        # Main loop, wait on tasks to finish, insert new ones
        while dag_state['waiting'] or dag_state['ready'] or dag_state['running']:
            payload = event_queue.get()

            if isinstance(payload['status'], Exception):
                raise payload['status']

            key = payload['key']
            finish_task(dsk, key, dag_state, results,
                        release_data=self._release_data)

            while dag_state['ready'] and self.available_workers.qsize() > 0:
                fire_task()

        return self.gather(result)

    def schedule_from_client(self, header, payload):
        """

        Input Payload: keys, dask
        Output Payload: keys, result
        Sent to client on 'schedule-ack'
        """
        loads = header.get('loads', dill.loads)
        payload = loads(payload)
        address = header['address']
        dsk = payload['dask']
        keys = payload['keys']

        header2 = {'jobid': header.get('jobid'),
                   'function': 'schedule-ack'}
        try:
            result = self.schedule(dsk, keys)
            header2['status'] = 'OK'
        except Exception as e:
            result = e
            header2['status'] = 'Error'

        payload2 = {'keys': keys, 'result': result}
        self.send_to_client(address, header2, payload2)

    def _release_data(self, key, state, delete=True):
        """ Remove data from temporary storage during scheduling run

        See Also
            Scheduler.schedule
            dask.async.finish_task
        """
        if key in state['waiting_data']:
            assert not state['waiting_data'][key]
            del state['waiting_data'][key]

        state['released'].add(key)

        if delete:
            self.release_key(key)
