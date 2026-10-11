"""Signals only set a flag; an active item is allowed to commit before exit."""

import signal
import time
from contextlib import contextmanager

from .config import MinerError


class StopRequested(MinerError):
    """An idle/discovery wait was interrupted; no cursor should advance."""


class Shutdown:
    def __init__(self, output=print):
        self.requested = False
        self.active = 0
        self.output = output
        self.signum = None

    def request(self, signum=None, frame=None):
        # Signal handlers must not perform I/O or touch SQLite.
        self.requested = True
        self.signum = signum

    @contextmanager
    def signals(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                signal.signal(sig, self.request)
            yield self
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    @contextmanager
    def item(self):
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    def wait(self, seconds, label="休眠", countdown=False):
        deadline = time.monotonic() + max(0, seconds)
        last_logged = None
        while True:
            if self.requested and not self.active:
                raise StopRequested("已收到退出信号，保留当前断点。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            whole = int(remaining + 0.999)
            if countdown and whole != last_logged:
                self.output("[WAIT] " + label + "：剩余 " + str(whole) + " 秒" +
                            ("；退出信号待当前条目完成后处理" if self.requested else ""))
                last_logged = whole
            time.sleep(min(1, remaining))
