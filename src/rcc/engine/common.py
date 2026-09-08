"""Shared helpers used across the engine package."""

import asyncio
import os
from collections.abc import Iterable

from ..languages import standardize_extension
from ..model import Commit

# Permissions for the per-commit work directories (and everything created
# inside them).
DEFAULT_MKDIR_PERMISSIONS = 0o777


def set_extension(commit: Commit) -> None:
    if commit.fname is None:
        raise ValueError("Commit has no filename; cannot deduce its extension")
    _, extension = os.path.splitext(commit.fname)
    commit.extension = standardize_extension(extension[1:])


def raise_first_error(results: Iterable[BaseException | None]) -> None:
    """Re-raise the first exception in a ``gather(return_exceptions=True)`` list.

    Used after each concurrent download batch: every in-flight download has
    finished by the time this is called, so the caller's cleanup (which may
    delete ``base_dir``) can never race a worker thread still writing into it.
    """
    for result in results:
        if isinstance(result, BaseException):
            raise result


def mark_task_done(task: asyncio.Task[None]) -> None:
    """Retrieve a finished task's outcome (suppress 'never retrieved' warnings).

    Used on background tasks whose result may never be awaited on some paths
    (e.g. when the enclosing coroutine is cancelled mid-prefetch). Awaiting
    the task later still yields the same result/exception.
    """
    if not task.cancelled():
        _ = task.exception()


async def await_task_result(task: asyncio.Task[None]) -> Exception | None:
    """Await ``task`` and return its exception, or ``None`` on success.

    Non-Exception BaseExceptions (e.g. CancelledError) propagate, so a
    cancellation is never mistaken for a failed download.
    """
    try:
        await task
    except Exception as error:  # noqa: BLE001
        return error
    return None
