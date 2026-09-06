import argparse
import asyncio
import logging
import logging.handlers
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
        "[%(asctime)s] %(module)s:%(lineno)d: <%(taskName)s> %(message)s",
        defaults={"taskName": "-"},
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    if log_config is not None:
        fmt = "%(asctime)s [%(levelname)s] <%(taskName)s> %(message)s"
        formatter = logging.Formatter(fmt, defaults={"taskName": "-"})
        handler = logging.handlers.TimedRotatingFileHandler(
            str(log_config["file"]), when="D"
        )
        handler.setLevel(str(log_config["level"]))
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


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


async def _stop_consumers(
    engine_consumers: list[asyncio.Task[None]],
    task_queue: asyncio.Queue[Commit | None],
    logger: logging.Logger,
) -> None:
    """Ask the consumers to stop and wait for them to finish.

    The ``None`` hints are put directly on the task queue: the queue is
    bounded, so a full queue makes the puts wait until the consumers drain
    it (which is exactly the graceful stop condition). A second interruption
    aborts the wait and cancels every consumer.
    """
    try:
        for consumer in engine_consumers:
            if not consumer.done():
                await task_queue.put(None)

        # Wait for every consumer to finish draining its in-flight commits.
        results = await asyncio.gather(*engine_consumers, return_exceptions=True)
        for consumer, result in zip(engine_consumers, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("Consumer failed during shutdown", exc_info=result)
    except KeyboardInterrupt:
        # Give up and cancel everything
        logger.info("Aborted")
        for consumer in engine_consumers:
            _ = consumer.cancel()


def task_queue_maxsize(cfg: config.Config) -> int:
    """Capacity of the bounded task queue: 2x the total commit slots.

    The factor of two gives the pipeline some headroom while still letting a
    blocking ``put`` act as the backpressure mechanism that keeps the parent
    from overproducing work.
    """
    return config.queue_maxsize(cfg)


def _load_config() -> tuple[config.Config, logging.Logger]:
    """Parse arguments, build the configuration and set up logging.

    Exits the process when the configuration is invalid.
    """
    args = vars(parse_args())
    config_arg = str(args.get("config"))
    try:
        if config_arg == "env":
            cfg = config.from_env(config.DEFAULT_CONFIG)
        else:
            cfg = config.from_json(config.DEFAULT_CONFIG, config_arg)
    except config.ConfigError as e:
        # The configuration could not even be built (e.g. an unparseable
        # parallelism env var). The configured logger is not available yet,
        # so report on stderr and refuse to start.
        print(f"Invalid configuration: {e}", file=sys.stderr)
        sys.exit(1)

    log_config = cast(dict[str, object], cfg.log) if isinstance(cfg.log, dict) else None
    logger = setup_logger(config.DEFAULT_LOGGER, log_config)

    # Refuse to start on nonsensical parallelism values instead of crashing
    # obscurely later (e.g. a semaphore of size 0 deadlocking every consumer).
    try:
        config.validate(cfg)
    except config.ConfigError as e:
        logger.error(f"Invalid configuration: {e}")
        sys.exit(1)

    return cfg, logger


async def _poll_commits(
    data_provider: data.DataProvider,
    task_queue: asyncio.Queue[Commit | None],
    cfg: config.Config,
    logger: logging.Logger,
) -> None:
    """Poll the database forever, feeding new commits into ``task_queue``.

    Every commit still STATUS_IN_QUEUE is re-fetched on each cycle, so
    recently enqueued ids are suppressed (see :func:`select_new_commits`):
    without this a commit waiting in the queue for a free consumer would be
    enqueued again and again. Consumer-side claiming already makes such
    duplicates harmless; this only avoids wasting queue capacity and claim
    round trips.
    """
    recently_enqueued: dict[int, float] = {}
    commit_suppression = float(
        str(
            cfg.get(
                "commit_enqueue_suppression",
                config.DEFAULT_COMMIT_ENQUEUE_SUPPRESSION,
            )
        )
    )
    sleeper = util.Sleeper(
        cast(float, cfg.min_sleep_time), cast(float, cfg.max_sleep_time)
    )

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


async def main() -> None:
    # Imported here (and not at module level) to break the import cycle:
    # rcc.engine imports submodules of this package.
    from . import engine

    if (current := asyncio.current_task()) is not None:
        current.set_name("poller")

    cfg, logger = _load_config()

    num_workers, concurrency = config.parallelism_values(cfg)
    logger.info(
        f"Parallelism: consumers={num_workers}, concurrency={concurrency}, max_in_flight={config.total_slots(cfg)}"
    )

    with util.SingletonContext(cast(str, cfg.lock_file)):
        logger.info("Started")
        logger.debug(f"Configuration: {cfg}")

        data_provider = data.from_config(cfg)

        # Bounded task queue (2x the commit slots): the awaited put() below
        # is the backpressure mechanism.
        task_queue: asyncio.Queue[Commit | None] = asyncio.Queue(
            maxsize=task_queue_maxsize(cfg)
        )
        engine_consumers: list[asyncio.Task[None]] = []

        try:
            # Open the single process-wide connection pool before the
            # consumers start pulling, so their first claim round trips find
            # it ready. Connections are established lazily, so a database
            # that is not up yet does not crash the process: poll cycles
            # simply fail and are retried.
            await data_provider.open()

            engine_consumers = [
                asyncio.create_task(
                    engine.process_commits(
                        data_provider, task_queue, cfg, manage_pool=False
                    ),
                    name=f"consumer-{i}",
                )
                for i in range(cast(int, cfg.num_workers))
            ]

            await _poll_commits(data_provider, task_queue, cfg, logger)
        except KeyboardInterrupt:
            # Only possible for a second Ctrl-C while the first one is already
            # being handled (see the CancelledError branch below).
            logger.info("Interrupted; waiting for consumers")
            await _stop_consumers(engine_consumers, task_queue, logger)
        except asyncio.CancelledError:
            # asyncio.run() (Python >= 3.11) translates the first Ctrl-C into
            # cancellation of this task. Run the same graceful shutdown, then
            # re-raise so asyncio.run() turns the cancellation back into a
            # KeyboardInterrupt for the caller.
            logger.info("Interrupted; waiting for consumers")
            await _stop_consumers(engine_consumers, task_queue, logger)
            raise
        finally:
            await data_provider.close()
            logger.info("Exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() re-raises KeyboardInterrupt after gracefully stopping
        # the workers: nothing left to do.
        pass
