"""
Collection of utilities.
"""

from __future__ import unicode_literals
from six.moves import urllib

import datetime
import fcntl
import os
import signal
import time

from .languages import language_from_extension


class SingletonContext(object):
    """
    Provides a context of execution that uses a lock file to check if this
    context is being used elsewhere, failing to enter if that is the case.
    """

    def __init__(self, lock_fname, remove_lock_at_exit=True):
        self.lock_fname = lock_fname
        self.remove_at_exit = remove_lock_at_exit

    def __enter__(self):
        self.lock_file = open(self.lock_fname, "w")
        try:
            fcntl.lockf(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(os.getpid(), file=self.lock_file)
        except IOError:
            self.close()
            raise RuntimeError(
                "Cannot enter singleton context (lock file: {})".format(
                    self.lock_file.name
                )
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lock_file.close()
        if self.remove_at_exit:
            os.unlink(self.lock_fname)


class UninterruptibleContext(object):
    """Make a region of code 'immune' to Ctrl-C."""

    def __enter__(self):
        self.sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        signal.signal(signal.SIGINT, self.sigint_handler)


class Sleeper(object):
    """
    Used for variable sleep times. Successive calls to sleep() increase sleep
    time towards the max sleep time. A call to reset() goes back to mininum
    sleep time.
    """

    def __init__(self, min_time, max_time, steps_to_max=10):
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
        self.reset()

    def reset(self):
        self.alpha = 0

    def sleep(self, increase=True):
        t = self.alpha * self.alpha
        t = self.min_time * (1.0 - t) + self.max_time * t
        time.sleep(t)

        if increase and self.alpha < 1:
            self.alpha += self.step_size


def count_if(pred, iterable):
    """Returns the number of elements of `iterable` for which `pred` holds."""
    return sum(1 for x in iterable if pred(x))


def standardize_extension(ext_raw):
    """
    This function transforms similar extensions into a single name. These are
    not supposed to be 'correct' extensions, but to reference files from the
    same language in a unified way.
    """
    ext = ext_raw.split('.')[-1]
    
    if ext in ("zip"):
        return ext
    
    language = language_from_extension(ext)
    if language is not None:
        return language.standard_extension
    
    return None


def deduce_language(zip_file):
    counts = dict()
    fnames = zip_file.namelist()
    for fname in fnames:
        name, ext = os.path.splitext(fname)
        if ext == "":
            continue
        ext = standardize_extension(ext[1:])
        if ext is None:
            continue
        counts[ext] = 1 + counts.get(ext, 0)
    if len(counts) == 0:
        raise ValueError("No files with extensions were found")
    language = max(counts.keys(), key=lambda x: counts[x])
    return language


def is_compilable(ext):
    """
    Given some extension (as returned by `standardize_extension()`), is it of
    compilable source code?
    """
    language = language_from_extension(ext)
    if language is not None:
        return language.compilable
    return False


def from_datetime_to_timestamp(dt: datetime.datetime) -> int:
    epoch = datetime.datetime.utcfromtimestamp(0)
    return (dt - epoch).total_seconds()
