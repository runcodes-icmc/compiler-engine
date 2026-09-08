"""The poller: feed queued commits from the database into the task queue."""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

from ..config import DEFAULT_COMMIT_ENQUEUE_SUPPRESSION, Config
from ..model import Commit
from ..util import Sleeper

if TYPE_CHECKING:
    from ..provider.data import DataProvider


def select_new_commits(
    commits: list[Commit], recently_enqueued: dict[int, float], suppression: float
) -> list[Commit]:
    """Filter out recently enqueued commits and prune stale tracking entries.

    ``recently_enqueued`` maps a commit id to the ``time.monotonic()`` value
    at which it was put on the task queue. An entry is pruned when:

    * its commit left the ``STATUS_IN_QUEUE`` fetch — a consumer claimed it,
      so a commit released back to the queue after a retryable failure is
      re-enqueued immediately; or
    * ``suppression`` seconds elapsed — so a commit whose consumer died
      between pulling and claiming it is retried instead of being suppressed
      forever.
    """
    fetched_ids = {commit.id for commit in commits}
    cutoff = time.monotonic() - suppression
    for commit_id in list(recently_enqueued):
        if commit_id not in fetched_ids or recently_enqueued[commit_id] < cutoff:
            del recently_enqueued[commit_id]
    return [commit for commit in commits if commit.id not in recently_enqueued]


async def poll_commits(
    data_provider: DataProvider,
    task_queue: asyncio.Queue[Commit | None],
    cfg: Config,
    logger: logging.Logger,
) -> None:
    """Poll the database forever, feeding new commits into ``task_queue``.

    Every commit still STATUS_IN_QUEUE is re-fetched on each cycle, so
    recently enqueued ids are suppressed (see :func:`select_new_commits`):
    without this a commit waiting in the queue for a free slot would be
    enqueued again and again. Consumer-side claiming already makes such
    duplicates harmless; this only avoids wasting queue capacity and claim
    round trips.
    """
    recently_enqueued: dict[int, float] = {}
    commit_suppression = float(
        str(
            cfg.get(
                "commit_enqueue_suppression",
                DEFAULT_COMMIT_ENQUEUE_SUPPRESSION,
            )
        )
    )
    sleeper = Sleeper(cast(float, cfg.min_sleep_time), cast(float, cfg.max_sleep_time))

    while True:
        try:
            commits = await data_provider.fetch_commits_in_queue()
            commits = select_new_commits(commits, recently_enqueued, commit_suppression)
        except Exception:
            logger.exception("Could not fetch commits")
            commits = []
        for commit in commits:
            await task_queue.put(commit)
            recently_enqueued[commit.id] = time.monotonic()
        if commits:
            sleeper.reset()
        await asyncio.sleep(sleeper.sleep_time())
