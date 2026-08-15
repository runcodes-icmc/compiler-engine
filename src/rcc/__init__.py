from __future__ import annotations, unicode_literals

import argparse
import asyncio
import logging
import logging.handlers
import multiprocessing as mp
import multiprocessing.queues as mp_queues
import sys
import time
from typing import cast

from . import config, util
from .model import Commit
from .provider import data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="The run.codes compiler")
    _ = parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file or config mode.",
        default="env",
    )
    return parser.parse_args()


def setup_logger(name: str, log_config: dict[str, object] | None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stderr)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(module)s:%(lineno)d: <%(process)d> %(message)s"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    if log_config is not None:
        fmt = "%(asctime)s [%(levelname)s] <%(process)d> %(message)s"
        formatter = logging.Formatter(fmt)
        handler = logging.handlers.TimedRotatingFileHandler(
            str(log_config["file"]), when="D"
        )
        handler.setLevel(str(log_config["level"]))
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def commit_filter(_: Commit) -> bool:
    return True


def select_new_commits(
    commits: list[Commit], recently_enqueued: dict[int, float], suppression: float
) -> list[Commit]:
    """Filter out recently enqueued commits and prune stale tracking entries.

    ``recently_enqueued`` maps a commit id to the ``time.monotonic()`` value
    at which it was put on the task queue. An entry is pruned when:

    * its commit left the ``STATUS_IN_QUEUE`` fetch — a worker claimed it, so
      a commit released back to the queue after a retryable failure is
      re-enqueued immediately; or
    * ``suppression`` seconds elapsed — so a commit whose worker died between
      pulling and claiming it is retried instead of being suppressed forever.
    """
    fetched_ids = {commit.id for commit in commits}
    cutoff = time.monotonic() - suppression
    for commit_id in list(recently_enqueued):
        if commit_id not in fetched_ids or recently_enqueued[commit_id] < cutoff:
            del recently_enqueued[commit_id]
    return [commit for commit in commits if commit.id not in recently_enqueued]


async def _stop_workers(
    engine_workers: list[mp.Process],
    task_queue: mp_queues.JoinableQueue[Commit | None],
    logger: logging.Logger,
) -> None:
    """Ask the workers to stop and wait for them to finish.

    The ``None`` hints are put through a thread: the task queue is bounded,
    so a full queue would otherwise block the event loop while the workers
    drain it. A second interruption aborts the wait and terminates every
    worker.
    """
    try:
        for worker in engine_workers:
            # The 'None' task is a hint for workers to stop processing
            if worker.is_alive():
                await asyncio.to_thread(task_queue.put, None)

        # Join each worker to ensure all of them are done
        for worker in engine_workers:
            worker.join()
    except KeyboardInterrupt:
        # Give up and terminate everything
        logger.info("Aborted")
        for worker in engine_workers:
            worker.terminate()


def _total_slots(cfg: config.Config) -> int:
    """Total number of commits that may be in flight across all workers."""
    concurrency = int(
        str(cfg.get("concurrency_per_worker", config.DEFAULT_CONCURRENCY_PER_WORKER))
    )
    return int(str(cfg.num_workers)) * concurrency


def task_queue_maxsize(cfg: config.Config) -> int:
    """Capacity of the bounded task queue: 2x the total commit slots.

    The factor of two gives the pipeline some headroom while still letting a
    blocking ``put`` act as the backpressure mechanism that keeps the parent
    from overproducing work.
    """
    return 2 * _total_slots(cfg)


async def main() -> None:
    # Imported here (and not at module level) to break the import cycle:
    # rcc.engine imports submodules of this package.
    from . import engine

    args = vars(parse_args())
    config_arg = str(args.get("config"))
    if config_arg == "env":
        cfg = config.from_env(config.DEFAULT_CONFIG)
    else:
        cfg = config.from_json(config.DEFAULT_CONFIG, config_arg)

    log_config = cast(dict[str, object], cfg.log) if isinstance(cfg.log, dict) else None
    logger = setup_logger(config.DEFAULT_LOGGER, log_config)

    with util.SingletonContext(cast(str, cfg.lock_file)):
        logger.info("Started")
        logger.debug("Configuration: {}".format(cfg))

        data_provider = data.from_config(cfg)

        # Bounded task queue (2x the commit slots across all workers): a
        # blocking put() is the backpressure mechanism. Putting is offloaded
        # to a thread so a full queue never blocks the event loop.
        task_queue: mp_queues.JoinableQueue[Commit | None] = mp.JoinableQueue(
            maxsize=task_queue_maxsize(cfg)
        )
        engine_workers = [
            mp.Process(target=engine.run_worker, args=(data_provider, task_queue, cfg))
            for _ in range(cast(int, cfg.num_workers))
        ]

        # Poll for new commits and put them in our internal processing queue
        try:
            sleeper = util.Sleeper(
                cast(float, cfg.min_sleep_time), cast(float, cfg.max_sleep_time)
            )
            for worker in engine_workers:
                worker.start()

            # Open this process's own connection pool. This happens after the
            # workers have been spawned so the pool is never forked into or
            # pickled towards a child process (every process opens its own
            # pool). Connections are established lazily, so a database that is
            # not up yet does not crash the process: poll cycles simply fail
            # and are retried
            await data_provider.open()

            # Suppress duplicate enqueueing: the poller re-fetches every
            # commit that is still STATUS_IN_QUEUE on each cycle, so without
            # this a commit waiting in the queue for a free worker would be
            # put on it again and again. Worker-side claiming already makes
            # such duplicates harmless; this only avoids wasting queue
            # capacity and claim round trips on them. See
            # :func:`select_new_commits` for the pruning rules.
            recently_enqueued: dict[int, float] = {}
            commit_suppression = float(
                str(
                    cfg.get(
                        "commit_enqueue_suppression",
                        config.DEFAULT_COMMIT_ENQUEUE_SUPPRESSION,
                    )
                )
            )

            while True:
                try:
                    commits = await data_provider.fetch_commits_in_queue()
                    commits = list(filter(commit_filter, commits))
                    commits = select_new_commits(
                        commits, recently_enqueued, commit_suppression
                    )
                except Exception:
                    logger.error("Could not fetch commits", exc_info=True)
                    commits = []
                if len(commits) > 0:
                    for commit in commits:
                        # Blocking put on a bounded queue = backpressure:
                        # the loop stalls here while the workers drain, so
                        # no join() barrier is needed.
                        await asyncio.to_thread(task_queue.put, commit)
                        recently_enqueued[commit.id] = time.monotonic()
                    sleeper.reset()
                await asyncio.sleep(sleeper.sleep_time())
        except KeyboardInterrupt:
            # Only possible for a second Ctrl-C while the first one is already
            # being handled (see the CancelledError branch below).
            logger.info("Interrupted; waiting for workers")
            await _stop_workers(engine_workers, task_queue, logger)
        except asyncio.CancelledError:
            # asyncio.run() (Python >= 3.11) translates the first Ctrl-C into
            # cancellation of this task. Run the same graceful shutdown, then
            # re-raise so asyncio.run() turns the cancellation back into a
            # KeyboardInterrupt for the caller.
            logger.info("Interrupted; waiting for workers")
            await _stop_workers(engine_workers, task_queue, logger)
            raise
        finally:
            await data_provider.close()
            # Also reached on (gracefully handled) interruption.
            logger.info("Exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() re-raises KeyboardInterrupt after gracefully stopping
        # the workers: nothing left to do.
        pass
