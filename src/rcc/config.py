"""
Module for handling configuration.
"""

from __future__ import annotations

import json
import os
from typing import cast, override

DEFAULT_CONFIG = "run.codes"
DEFAULT_LOGGER = "run.codes"

# Default number of worker processes spawned to process commits. The
# workload is IO-bound (containers, S3, database), so sizing is deliberately
# *not* tied to the CPU count: the real ceiling for in-flight work is how
# many compilation containers the Docker host can run at once, not the
# number of cores. Worker processes are also the expensive part of the
# pipeline (each owns an event loop and a database connection pool), so
# prefer raising the per-worker concurrency over spawning more processes.
DEFAULT_NUM_WORKERS = 2

# Default number of commits a single worker process may process concurrently.
# Used as the fallback for JSON configuration files that do not define
# ``concurrency_per_worker``. Together with `DEFAULT_NUM_WORKERS` this bounds
# the total number of in-flight commits (workers x concurrency); the
# practical ceiling is the Docker host capacity, not the CPU count.
DEFAULT_CONCURRENCY_PER_WORKER = 4

# How long (seconds) the poller suppresses re-enqueueing a commit it already
# put on the task queue. The poller re-fetches every commit that is still
# ``STATUS_IN_QUEUE`` on each cycle; this window keeps a commit waiting for a
# free worker from being put on the queue again and again (worker-side
# claiming already makes such duplicates harmless, so this only saves queue
# capacity and claim round trips).
DEFAULT_COMMIT_ENQUEUE_SUPPRESSION = 60


class ConfigError(ValueError):
    """Raised when a configuration value is missing, unparseable or invalid."""


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, raising `ConfigError` with a clear message."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("{} must be an integer, got {!r}".format(name, raw)) from None


__config__: dict[str, Config] = dict()


class Config:
    """
    Acts as a view for `dict` objects. All keys must be of type `str`. Useful
    for accessing config params in a more readable way.

    Values are deliberately typed as `object`: configuration dictionaries mix
    strings, numbers, booleans and nested dictionaries, and the type of each
    key is only known at its point of use (callers convert with `str()`,
    `int()`, `float()` or `bool()` as appropriate).
    """

    __config__: dict[str, object]

    def __init__(self, config_dict: dict[str, object]) -> None:
        _check_dict(config_dict)
        self.__config__ = config_dict

    def update(self, other: Config | dict[str, object]) -> None:
        """Update stored configuration using another `Config` or `dict`."""
        if isinstance(other, Config):
            self.__config__.update(other.get_dict())
        else:
            _check_dict(other)
            self.__config__.update(other)

    def get_dict(self) -> dict[str, object]:
        """Return the underlying configuration dictionary."""
        return self.__config__

    def get(self, key: str, default: object | None = None) -> object:
        """Return ``config[key]``, or ``default`` when the key is missing.

        Mirrors ``dict.get``; used for optional keys (such as
        ``concurrency_per_worker``) that JSON configuration files may not
        define. Attribute access raises ``KeyError`` for those.
        """
        return self.__config__.get(key, default)

    def __getattr__(self, name: str) -> object:
        return self.__config__[name]

    @override
    def __getstate__(self) -> dict[str, dict[str, object]]:
        """Return state for pickling."""
        return {"__config__": self.__config__}

    def __setstate__(self, state: dict[str, dict[str, object]]) -> None:
        """Restore state from pickling."""
        self.__config__ = state["__config__"]

    @override
    def __repr__(self) -> str:
        return repr(self.__config__)


def parallelism_values(cfg: Config) -> tuple[int, int]:
    """Return ``(num_workers, concurrency_per_worker)`` from ``cfg``.

    Missing keys fall back to `DEFAULT_NUM_WORKERS` and
    `DEFAULT_CONCURRENCY_PER_WORKER`, so both env- and JSON-built configs
    behave identically. Raises `ConfigError` when a value cannot be parsed
    as an integer.
    """

    def _parse_int(value: object, key: str, env_var: str) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            raise ConfigError(
                "{} ({}) must be an integer, got {!r}".format(key, env_var, value)
            ) from None

    return (
        _parse_int(
            cfg.get("num_workers", DEFAULT_NUM_WORKERS),
            "num_workers",
            "RUNCODES_COMPILER_NUM_WORKERS",
        ),
        _parse_int(
            cfg.get("concurrency_per_worker", DEFAULT_CONCURRENCY_PER_WORKER),
            "concurrency_per_worker",
            "RUNCODES_COMPILER_CONCURRENCY",
        ),
    )


def total_slots(cfg: Config) -> int:
    """Total number of commits that may be in flight across all workers."""
    num_workers, concurrency = parallelism_values(cfg)
    return num_workers * concurrency


def queue_maxsize(cfg: Config) -> int:
    """Capacity of the bounded task queue: 2x the total commit slots.

    The factor of two gives the pipeline some headroom while still letting a
    blocking ``put`` act as the backpressure mechanism that keeps the parent
    from overproducing work.
    """
    return 2 * total_slots(cfg)


def validate(cfg: Config) -> None:
    """Validate the parallelism-related values of ``cfg``.

    Raises `ConfigError` with a human-readable message when a value is
    missing, unparseable, or nonsensical: ``num_workers >= 1``,
    ``concurrency >= 1``, and a bounded task queue at least as large as the
    total number of in-flight commit slots.
    """
    num_workers, concurrency = parallelism_values(cfg)
    if num_workers < 1:
        raise ConfigError(
            "num_workers (RUNCODES_COMPILER_NUM_WORKERS) must be >= 1, got {}".format(
                num_workers
            )
        )
    if concurrency < 1:
        raise ConfigError(
            "concurrency_per_worker (RUNCODES_COMPILER_CONCURRENCY) must be >= 1, got {}".format(
                concurrency
            )
        )
    total = total_slots(cfg)
    qsize = queue_maxsize(cfg)
    if qsize < total:
        raise ConfigError(
            "task queue size ({}) must be >= total in-flight slots ({})".format(
                qsize, total
            )
        )


class EnvConfig(Config):
    def __init__(self) -> None:
        db_config: dict[str, object] = {
            "name": os.environ.get("RUNCODES_DB_DATABASE", "runcodes"),
            "host": os.environ.get("RUNCODES_DB_HOST", "database"),
            "port": int(os.environ.get("RUNCODES_DB_PORT", "5432")),
            "username": os.environ.get("RUNCODES_DB_USERNAME", "runcodes"),
            "password": os.environ.get("RUNCODES_DB_PASSWORD", "asdasd33"),
            "pool_min_size": int(os.environ.get("RUNCODES_DB_POOL_MIN_SIZE", "1")),
            "pool_timeout": float(os.environ.get("RUNCODES_DB_POOL_TIMEOUT", "30")),
        }
        # ``pool_max_size`` is deliberately *omitted* when the env var is
        # unset: the Postgres provider derives the maximum from the
        # per-process concurrency (concurrency + 2, clamped to at least
        # ``pool_min_size``). An explicitly configured
        # ``RUNCODES_DB_POOL_MAX_SIZE`` always wins.
        pool_max_env = os.environ.get("RUNCODES_DB_POOL_MAX_SIZE")
        if pool_max_env is not None:
            db_config["pool_max_size"] = int(pool_max_env)
        env_configs: dict[str, object] = {
            "provider": {
                "data": "postgres",
                "storage": "s3",
            },
            "db": db_config,
            "s3": {
                "region": os.environ.get("RUNCODES_S3_REGION", "sa-east-1"),
                "endpoint": os.environ.get(
                    "RUNCODES_S3_ENDPOINT", "http://seaweed:8333"
                ),
                "access_key": os.environ.get("RUNCODES_S3_CREDENTIALS_KEY", "test_key"),
                "secret_key": os.environ.get(
                    "RUNCODES_S3_CREDENTIALS_SECRET", "test_secret"
                ),
                "commits_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-commits",
                "outputfiles_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-outputfiles",
                "files_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-files",
                "cases_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-cases",
                "compilation_files_dir": "compilationfiles",
            },
            "lock_file": "compiler.lock",
            "num_workers": _env_int(
                "RUNCODES_COMPILER_NUM_WORKERS", DEFAULT_NUM_WORKERS
            ),
            # Number of commits each worker process handles concurrently.
            "concurrency_per_worker": _env_int(
                "RUNCODES_COMPILER_CONCURRENCY", DEFAULT_CONCURRENCY_PER_WORKER
            ),
            # Seconds the poller waits before re-enqueueing a commit it
            # already put on the task queue.
            "commit_enqueue_suppression": float(
                os.environ.get(
                    "RUNCODES_COMPILER_ENQUEUE_SUPPRESSION",
                    str(DEFAULT_COMMIT_ENQUEUE_SUPPRESSION),
                )
            ),
            "min_sleep_time": 1,
            "max_sleep_time": 15,
            "exec_dir": os.environ.get("RUNCODES_COMPILER_EXEC_DIR", "/tmp"),
            "exec_dir_remote": os.environ.get(
                "RUNCODES_COMPILER_EXEC_DIR_REMOTE",
                os.environ.get("RUNCODES_COMPILER_EXEC_DIR", "/tmp"),
            ),
            "src_dir": "src",
            "output_files_dir": "outputfiles",
            "max_output_file_size": 1048576,
            "compilation_error_file": "compilation.err",
            "compilation_output_file": "compilation.out",
            "compilation_timeout": float(
                os.environ.get("RUNCODES_DEFAULT_COMPILATION_TIMEOUT", "10")
            ),
            "base_exec_timeout": float(
                os.environ.get("RUNCODES_DEFAULT_EXEC_TIMEOUT", "5")
            ),
            "monitor_max_file_size": 5242880,
            "monitor_max_mem_size": 268435456,
            "container_cfg_file": "container.config",
            "slack": None,
            "log": None,
            "cleanup_on_error": False,
        }
        super().__init__(env_configs)


def get_config(name: str) -> Config | None:
    """Return the `Config` registered under the given name, or `None`."""
    return __config__.get(name)


def from_json(name: str, fname: str) -> Config:
    """Register a new `Config` with the given name, read from a JSON file."""
    with open(fname, "r") as config_file:
        config_dict = cast(dict[str, object], json.load(config_file))
        c = Config(config_dict)
        __config__[name] = c
        return c


def from_dict(name: str, d: dict[str, object]) -> Config:
    """Register a new `Config` with the given name, built from a regular `dict`."""
    c = Config(d)
    __config__[name] = c
    return c


def from_env(name: str) -> Config:
    """Register a new `Config` with the given name, built from environment variables."""
    c = EnvConfig()
    __config__[name] = c
    return c


def _check_dict(d: object) -> None:
    if not isinstance(d, dict):
        raise TypeError("Given argument is not a dict")
    if not all(isinstance(key, str) for key in cast(dict[object, object], d)):
        raise TypeError("All dictionary keys must be strings")
