"""The queue-driven consumer: claim and process commits until stopped."""

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from ..config import DEFAULT_LOGGER, get_concurrency
from ..model import Commit
from .pipeline import process_commit

if TYPE_CHECKING:
    from ..config import Config
    from ..provider.data import DataProvider

# How long the pull loop waits on an empty task queue before checking whether
# a non-retryable failure in an in-flight commit must stop the consumer.
QUEUE_GET_POLL_TIMEOUT = 1.0

# Exceptions that stop the consumer instead of failing just one commit.
NON_RETRYABLE_EXCEPTIONS = (MemoryError, OSError, SystemExit, SystemError)


async def _claim_and_run(
    data_provider: DataProvider,
    commit: Commit,
    cfg: Config,
    semaphore: asyncio.Semaphore,
    fatal: asyncio.Event,
    logger: logging.Logger,
) -> None:
    """Claim ``commit`` and process it, releasing the slot afterwards.

    The poller can enqueue the same commit more than once (a commit stays
    STATUS_IN_QUEUE until a consumer takes it, e.g. while it waits in the
    bounded task queue), so several consumers may pull copies of it. The
    claim is a conditional UPDATE (IN_QUEUE -> PROCESSING) in the provider,
    which is the only state shared across consumers: only the consumer whose
    update wins processes the commit; the losers drop their copies.
    """
    try:
        try:
            claimed = await data_provider.claim_commit(commit)
        except Exception as e:
            # Claim failed (e.g. a database hiccup): the commit stays
            # IN_QUEUE and the poller re-enqueues it later.
            logger.warning(f"Caught retryable exception: {e}", exc_info=True)
            return

        if not claimed:
            logger.debug(f"Commit {commit.id} already taken; skipping")
            return

        try:
            await process_commit(data_provider, commit, cfg)
        except NON_RETRYABLE_EXCEPTIONS as e:
            logger.warning(f"Caught non-retryable exception: {e}")
            fatal.set()
        except Exception as e:
            logger.warning(f"Caught retryable exception: {e}", exc_info=True)
            # We still hold the claim: give the commit back so the poller
            # can retry it.
            try:
                await data_provider.release_commit(commit)
            except Exception:
                logger.warning(
                    f"Could not release commit {commit.id} back to the queue",
                    exc_info=True,
                )
    finally:
        semaphore.release()


async def process_commits(
    data_provider: DataProvider,
    commit_queue: asyncio.Queue[Commit | None],
    cfg: Config,
) -> None:
    """The consumer: pull commits from the queue and process them.

    Producer/consumer structure: a single loop pulls commits from the queue
    and spawns one task per commit, claimed and processed by
    :func:`_claim_and_run`. An ``asyncio.Semaphore`` sized by the configured
    in-flight concurrency bounds the number of commits being processed; the
    slot is acquired before the task is spawned and released in the task's
    ``finally`` block, so a failing commit can never leak a slot.

    ``queue.get`` waits with a bounded timeout so the loop can notice
    failures of in-flight tasks. When the ``None`` stop hint arrives the loop
    stops pulling and drains every in-flight commit before exiting.
    Non-retryable exceptions stop the consumer (after the in-flight commits
    finish); retryable ones are logged and skipped.

    The connection pool is opened here before the pull loop starts and closed
    when the consumer exits.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)

    # In production the main process's handlers are inherited; add handlers
    # only when running standalone (e.g. tests).
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler(sys.stderr)
        console_fmt = logging.Formatter(
            "[%(asctime)s] %(module)s:%(lineno)d: <%(taskName)s> %(message)s",
            defaults={"taskName": "-"},
        )
        console_handler.setFormatter(console_fmt)
        logger.addHandler(console_handler)

    logger.debug("Consumer started")

    concurrency = get_concurrency(cfg)

    try:
        await data_provider.open()
    except Exception:
        logger.exception("Failed to open database connection pool")
        raise

    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    fatal = asyncio.Event()

    try:
        while True:
            if fatal.is_set():
                break

            try:
                # Bounded wait: lets the loop observe `fatal` (set by an
                # in-flight task) instead of blocking on an empty queue.
                commit = await asyncio.wait_for(
                    commit_queue.get(), timeout=QUEUE_GET_POLL_TIMEOUT
                )
            except TimeoutError:
                continue
            except NON_RETRYABLE_EXCEPTIONS as e:
                logger.warning(f"Caught non-retryable exception: {e}")
                break
            except Exception as e:
                logger.warning(f"Caught retryable exception: {e}", exc_info=True)
                continue

            try:
                if commit is None:
                    break

                _ = await semaphore.acquire()
                try:
                    task = asyncio.create_task(
                        _claim_and_run(
                            data_provider, commit, cfg, semaphore, fatal, logger
                        )
                    )
                except BaseException:
                    # Spawning failed: give the slot back immediately.
                    semaphore.release()
                    raise

                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            except NON_RETRYABLE_EXCEPTIONS as e:
                logger.warning(f"Caught non-retryable exception: {e}")
                break
            except Exception as e:
                logger.warning(f"Caught retryable exception: {e}", exc_info=True)

        if in_flight:
            _ = await asyncio.gather(*in_flight)
    finally:
        await data_provider.close()

    logger.debug("Consumer stopped")
