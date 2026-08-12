from __future__ import unicode_literals

import argparse
import asyncio
import logging
import logging.handlers
import multiprocessing as mp
import sys

from six.moves import range

import rcc.config
import rcc.engine
import rcc.provider
import rcc.provider.data
import rcc.util


def parse_args():
    parser = argparse.ArgumentParser(description="The run.codes compiler")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file or config mode.",
        default="env",
    )
    return parser.parse_args()


def setup_logger(name, log_config):
    # NOTE: should we handle multiprocessing?
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Console Logger
    console_handler = logging.StreamHandler(sys.stderr)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(module)s:%(lineno)d: <%(process)d> %(message)s"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # File Logger
    if log_config is not None:
        fmt = "%(asctime)s [%(levelname)s] <%(process)d> %(message)s"
        formatter = logging.Formatter(fmt)
        handler = logging.handlers.TimedRotatingFileHandler(
            log_config["file"], when="D"
        )
        handler.setLevel(log_config["level"])
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def commit_filter(_):
    return True


async def _stop_workers(engine_workers, task_queue, logger):
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


def _total_slots(cfg):
    """Total number of commits that may be in flight across all workers."""
    concurrency = int(
        cfg.get("concurrency_per_worker", rcc.config.DEFAULT_CONCURRENCY_PER_WORKER)
    )
    return int(cfg.num_workers) * concurrency


def _task_queue_maxsize(cfg):
    """Capacity of the bounded task queue: 2x the total commit slots.

    The factor of two gives the pipeline some headroom while still letting a
    blocking ``put`` act as the backpressure mechanism that keeps the parent
    from overproducing work.
    """
    return 2 * _total_slots(cfg)


async def main():
    args = parse_args()
    if args.config == "env":
        cfg = rcc.config.from_env(rcc.config.DEFAULT_CONFIG)
    else:
        cfg = rcc.config.from_json(rcc.config.DEFAULT_CONFIG, args.config)

    logger = setup_logger(rcc.config.DEFAULT_LOGGER, cfg.log)

    with rcc.util.SingletonContext(cfg.lock_file):
        logger.info("Started")
        logger.debug("Configuration: {}".format(cfg))

        # Provides acccess to metadata on things such as commits, exercises,
        # test cases, etc.
        data_provider = rcc.provider.data.from_config(cfg)

        # Queue and workers used to distribute the workload of commit processing.
        # The queue is bounded (2x the commit slots across all workers): a
        # blocking put() is the backpressure mechanism that replaces the old
        # per-batch join() barrier. Putting is offloaded to a thread so that a
        # full queue never blocks the event loop.
        task_queue = mp.JoinableQueue(maxsize=_task_queue_maxsize(cfg))
        engine_workers = [
            mp.Process(
                target=rcc.engine.run_worker, args=(data_provider, task_queue, cfg)
            )
            for _ in range(cfg.num_workers)
        ]

        # Poll for new commits and put them in our internal processing queue
        try:
            sleeper = rcc.util.Sleeper(cfg.min_sleep_time, cfg.max_sleep_time)
            for worker in engine_workers:
                worker.start()

            # Open this process's own connection pool. This happens after the
            # workers have been spawned so the pool is never forked into or
            # pickled towards a child process (every process opens its own
            # pool). Connections are established lazily, so a database that is
            # not up yet does not crash the process: poll cycles simply fail
            # and are retried
            await data_provider.open()

            while True:
                try:
                    commits = await data_provider.fetch_commits_in_queue()
                    commits = list(filter(commit_filter, commits))
                except Exception:
                    logger.error("Could not fetch commits", exc_info=True)
                    commits = []
                if len(commits) > 0:
                    for commit in commits:
                        # Blocking put on a bounded queue = backpressure:
                        # the loop stalls here while the workers drain, so
                        # there is no need for a join() barrier anymore.
                        await asyncio.to_thread(task_queue.put, commit)
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
