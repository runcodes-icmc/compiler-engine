"""
Collection of utilities.
"""

from __future__ import unicode_literals

import datetime
import fcntl
import os
import signal
import time
import zipfile
from collections.abc import Callable, Iterable
from types import FrameType, TracebackType
from typing import TextIO, TypeVar

from .languages import language_from_extension as language_from_extension

T = TypeVar("T")


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

    def __enter__(self) -> "SingletonContext":
        lock_file = open(self.lock_fname, "w")
        try:
            fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(os.getpid(), file=lock_file)
        except OSError:
            lock_file.close()
            raise RuntimeError(
                "Cannot enter singleton context (lock file: {})".format(self.lock_fname)
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


class UninterruptibleContext:
    """Make a region of code 'immune' to Ctrl-C."""

    sigint_handler: (
        Callable[[int, FrameType | None], object] | int | signal.Handlers | None
    )

    def __init__(self) -> None:
        self.sigint_handler = signal.SIG_DFL

    def __enter__(self) -> "UninterruptibleContext":
        self.sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        _ = signal.signal(signal.SIGINT, self.sigint_handler)


class Sleeper:
    """
    Used for variable sleep times. Successive calls to sleep() increase sleep
    time towards the max sleep time. A call to reset() goes back to mininum
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


def count_if(pred: Callable[[T], object], iterable: Iterable[T]) -> int:
    """Returns the number of elements of `iterable` for which `pred` holds."""
    return sum(1 for x in iterable if pred(x))


def standardize_extension(ext_raw: str) -> str | None:
    """
    This function transforms similar extensions into a single name. These are
    not supposed to be 'correct' extensions, but to reference files from the
    same language in a unified way.
    """
    ext = ext_raw.split(".")[-1]

    if ext == "zip":
        return ext

    language = language_from_extension(ext)
    if language is not None:
        return language.standard_extension

    return None


def deduce_language(zip_file: zipfile.ZipFile) -> str:
    counts: dict[str, int] = {}
    fnames = zip_file.namelist()
    for fname in fnames:
        _, ext = os.path.splitext(fname)
        if ext == "":
            continue
        ext = standardize_extension(ext[1:])
        if ext is None:
            continue
        counts[ext] = 1 + counts.get(ext, 0)
    if len(counts) == 0:
        raise ValueError("No files with extensions were found")
    language = max(counts, key=lambda x: counts[x])
    return language


def is_compilable(ext: str | None) -> bool:
    """
    Given some extension (as returned by `standardize_extension()`), is it of
    compilable source code?
    """
    if ext is None:
        return False
    language = language_from_extension(ext)
    if language is not None:
        return language.compilable
    return False


def from_datetime_to_timestamp(dt: datetime.datetime) -> int:
    epoch = datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
    return int((dt - epoch).total_seconds())
