"""One SQLite owner thread; collectors and inference exchange immutable results."""

from concurrent.futures import Future
from queue import Queue
from threading import Thread

from .config import MinerError
from .state import State


class StateWriter:
    def __init__(self, path):
        self.jobs = Queue()
        self.ready = Future()
        self.closed = False
        self.thread = Thread(target=self._run, args=(path,), name='sqlite-writer')
        self.thread.start()
        try:
            self.ready.result()
        except BaseException:
            self.thread.join()
            raise

    def _run(self, path):
        try:
            state = State(path)
        except BaseException as error:
            self.ready.set_exception(error)
            return
        self.ready.set_result(True)
        try:
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                name, args, kwargs, answer = job
                try:
                    answer.set_result(getattr(state, name)(*args, **kwargs))
                except BaseException as error:
                    answer.set_exception(error)
        finally:
            state.close()

    def __getattr__(self, name):
        if name.startswith('_') or name == 'connection' or not callable(getattr(State, name, None)):
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            if self.closed:
                raise MinerError('SQLite writer 已关闭。')
            answer = Future()
            self.jobs.put((name, args, kwargs, answer))
            return answer.result()
        return invoke

    def close(self):
        if not self.closed:
            self.closed = True
            self.jobs.put(None)
            self.thread.join()
