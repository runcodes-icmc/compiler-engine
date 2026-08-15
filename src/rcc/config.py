"""
Module for handling configuration.
"""

from __future__ import annotations

import json
import os
from typing import cast, override

DEFAULT_CONFIG = "run.codes"
DEFAULT_LOGGER = "run.codes"

# Default number of commits a single worker process may process concurrently.
# Used as the fallback for JSON configuration files that do not define
# ``concurrency_per_worker``.
DEFAULT_CONCURRENCY_PER_WORKER = 4

# How long (seconds) the poller suppresses re-enqueueing a commit it already
# put on the task queue. The poller re-fetches every commit that is still
# ``STATUS_IN_QUEUE`` on each cycle; this window keeps a commit waiting for a
# free worker from being put on the queue again and again (worker-side
# claiming already makes such duplicates harmless, so this only saves queue
# capacity and claim round trips).
DEFAULT_COMMIT_ENQUEUE_SUPPRESSION = 60


# Configs are registered here
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

        Mirrors ``dict.get``. Used for optional keys (such as
        ``concurrency_per_worker``) that JSON configuration files may not
        define; attribute access would raise ``KeyError`` for those.
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


class EnvConfig(Config):
    def __init__(self) -> None:
        env_configs: dict[str, object] = {
            "provider": {
                "data": "postgres",
                "storage": "s3",
            },
            "db": {
                "name": os.environ.get("RUNCODES_DB_DATABASE", "runcodes"),
                "host": os.environ.get("RUNCODES_DB_HOST", "database"),
                "port": int(os.environ.get("RUNCODES_DB_PORT", "5432")),
                "username": os.environ.get("RUNCODES_DB_USERNAME", "runcodes"),
                "password": os.environ.get("RUNCODES_DB_PASSWORD", "asdasd33"),
                "pool_min_size": int(os.environ.get("RUNCODES_DB_POOL_MIN_SIZE", "1")),
                "pool_max_size": int(os.environ.get("RUNCODES_DB_POOL_MAX_SIZE", "10")),
                "pool_timeout": float(os.environ.get("RUNCODES_DB_POOL_TIMEOUT", "30")),
            },
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
            "num_workers": int(
                os.environ.get("RUNCODES_COMPILER_NUM_WORKERS", f"{os.cpu_count()}")
            ),
            # Number of commits each worker process handles concurrently.
            "concurrency_per_worker": int(
                os.environ.get(
                    "RUNCODES_COMPILER_CONCURRENCY",
                    str(DEFAULT_CONCURRENCY_PER_WORKER),
                )
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
    """
    Returns the `Config` object registered to the given name or `None` if the
    name is not registered.
    """
    return __config__.get(name)


def from_json(name: str, fname: str) -> Config:
    """
    Registers a new `Config` object with the given name, read from a JSON file. Returns
    the created `Config` object.
    """
    with open(fname, "r") as config_file:
        config_dict = cast(dict[str, object], json.load(config_file))
        c = Config(config_dict)
        __config__[name] = c
        return c


def from_dict(name: str, d: dict[str, object]) -> Config:
    """
    Registers a new config with the given name, built from a regular `dict`.
    Returns the created `Config` object.
    """
    c = Config(d)
    __config__[name] = c
    return c


def from_env(name: str) -> Config:
    """
    Registers a new config with the given name, building it from environment variables.
    """
    c = EnvConfig()
    __config__[name] = c
    return c


def _check_dict(d: object) -> None:
    if not isinstance(d, dict):
        raise TypeError("Given argument is not a dict")
    if not all(isinstance(key, str) for key in cast(dict[object, object], d)):
        raise TypeError("All dictionary keys must be strings")
