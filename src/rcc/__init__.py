from __future__ import unicode_literals

from six.moves import range

import argparse
import logging
import logging.handlers
import sys
import multiprocessing as mp
import rcc.config
import rcc.engine
import rcc.provider
import rcc.provider.data
import rcc.provider.storage
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


def setup_logger(name, log_config, slack_config=None):
    # NOTE: should we handle multiprocessing?
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Console Logger
    console_handler = logging.StreamHandler(sys.stderr)
    console_fmt = logging.Formatter("[%(asctime)s] %(module)s:%(lineno)d: <%(process)d> %(message)s")
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
    
    # Slack Logger
    if slack_config is not None:
        fmt = "[%(asctime)s] %(module)s:%(lineno)d: <%(process)d> %(message)s"
        formatter = logging.Formatter(fmt)
        handler = rcc.util.SlackLogHandler(
            slack_config["webhook_url"], slack_config["sender_name"]
        )
        handler.setLevel(slack_config["level"])
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def commit_filter(commit):
    return True


def main():
    args = parse_args()
    if args.config == "env":
        cfg = rcc.config.from_env(rcc.config.DEFAULT_CONFIG)
    else:
        cfg = rcc.config.from_json(rcc.config.DEFAULT_CONFIG, args.config)

    logger = setup_logger(rcc.config.DEFAULT_LOGGER, cfg.log, cfg.slack)

    with rcc.util.SingletonContext(cfg.lock_file):
        logger.info("Started")
        logger.debug("Configuration: {}".format(cfg))

        # Provides acccess to metadata on things such as commits, exercises,
        # test cases, etc.
        data_provider = rcc.provider.data.from_config(cfg)

        # Queue and workers used to distribute the workload of commit processing
        task_queue = mp.JoinableQueue()
        engine_workers = [
            mp.Process(
                target=rcc.engine.process_commits, args=(data_provider, task_queue)
            )
            for i in range(cfg.num_workers)
        ]

        # Poll for new commits and put them in our internal processing queue
        try:
            sleeper = rcc.util.Sleeper(cfg.min_sleep_time, cfg.max_sleep_time)
            for worker in engine_workers:
                worker.start()

            while True:
                try:
                    commits = data_provider.fetch_commits_in_queue()
                    commits = list(filter(commit_filter, commits))
                except Exception:
                    logger.error("Could not fetch commits", exc_info=True)
                    commits = []
                if len(commits) > 0:
                    for commit in commits:
                        task_queue.put(commit)
                    task_queue.join()
                    sleeper.reset()
                sleeper.sleep()
        except KeyboardInterrupt as e:
            logger.info("Interrupted; waiting for workers")
            try:
                for worker in engine_workers:
                    # The 'None' task is a hint for workers to stop processing
                    if worker.is_alive():
                        task_queue.put(None)

                # Join each worker to ensure all of them are done
                for worker in engine_workers:
                    worker.join()
            except KeyboardInterrupt as e:
                # Give up and terminate everything
                logger.info("Aborted")
                for worker in engine_workers:
                    worker.terminate()
    logger.info("Exited")


if __name__ == "__main__":
    main()
