"""
Collection of utilities.
"""

import datetime
import fcntl
import os
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Self, TextIO


class SingletonContext:
    """
    Provides a context of execution that uses a lock file to check if this
    context is being used elsewhere, failing to enter if that is the case.
    """

    lock_fname: str
    remove_at_exit: bool
    lock_file: TextIO | None

    def __init__(self, lock_fname: str, remove_lock_at_exit: bool = True) -> None:
        self.lock_fname = lock_fname
        self.remove_at_exit = remove_lock_at_exit
        self.lock_file = None

    def __enter__(self) -> Self:
        lock_file = open(self.lock_fname, "w")
        try:
            fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(os.getpid(), file=lock_file)
        except OSError:
            lock_file.close()
            raise RuntimeError(
                f"Cannot enter singleton context (lock file: {self.lock_fname})"
            )
        self.lock_file = lock_file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.lock_file is not None:
            self.lock_file.close()
        if self.remove_at_exit:
            os.unlink(self.lock_fname)


class Sleeper:
    """
    Used for variable sleep times. Successive calls to sleep() increase sleep
    time towards the max sleep time. A call to reset() goes back to minimum
    sleep time.
    """

    min_time: float
    max_time: float
    step_size: float
    alpha: float

    def __init__(
        self, min_time: float, max_time: float, steps_to_max: int = 10
    ) -> None:
        if min_time < 0.0 or max_time < 0.0:
            raise ValueError("Minimum and maximum sleep times must be positive")
        if steps_to_max < 1:
            raise ValueError("Number of steps must be greater than 1")
        if min_time > max_time:
            # Silently ignore when min > max
            min_time, max_time = max_time, min_time

        self.min_time = min_time
        self.max_time = max_time
        self.step_size = 1.0 / steps_to_max
        self.alpha = 0.0
        self.reset()

    def reset(self) -> None:
        self.alpha = 0.0

    def sleep(self, increase: bool = True) -> None:
        time.sleep(self.sleep_time(increase))

    def sleep_time(self, increase: bool = True) -> float:
        """Compute the next sleep duration and advance the internal counter.

        Unlike :meth:`sleep`, this does not block, so it can be combined with
        ``asyncio.sleep`` in async code.
        """
        t = self.alpha * self.alpha
        t = self.min_time * (1.0 - t) + self.max_time * t

        if increase and self.alpha < 1:
            self.alpha += self.step_size
        return t


def count_if[T](pred: Callable[[T], object], iterable: Iterable[T]) -> int:
    """Returns the number of elements of `iterable` for which `pred` holds."""
    return sum(1 for x in iterable if pred(x))


def from_datetime_to_timestamp(dt: datetime.datetime) -> int:
    epoch = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)
    return int((dt - epoch).total_seconds())
