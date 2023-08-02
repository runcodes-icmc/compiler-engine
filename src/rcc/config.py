"""
Module for handling configuration.
"""

import json
import os


DEFAULT_CONFIG = "run.codes"
DEFAULT_LOGGER = "run.codes"


# Configs are registered here
__config__ = dict()


class Config:
    """
    Acts as a view for `dict` objects. All keys must be of type `str`. Useful
    for accessing config params in a more readable way.
    """

    def __init__(self, config_dict):
        _check_dict(config_dict)
        self.__config__ = config_dict

    def update(self, other):
        """Update stored configuration using another `Config` or `dict`."""
        if isinstance(other, Config):
            self.__config__.update(other.__config__)
        elif isinstance(other, dict):
            _check_dict(other)
            self.__config__.update(other)

    def __getattr__(self, name):
        return self.__config__[name]

    def __repr__(self):
        return self.__config__.__repr__()


class EnvConfig(Config):
    def __init__(self):
        env_configs = {
            "provider": {"data": "postgres", "storage": "s3", },
            "db": {
                "name": os.environ.get("RUNCODES_DB_DATABASE", "runcodes"),
                "host": os.environ.get("RUNCODES_DB_HOST", "database"),
                "port": int(os.environ.get("RUNCODES_DB_PORT", "5432")),
                "username": os.environ.get("RUNCODES_DB_USERNAME", "runcodes"),
                "password": os.environ.get("RUNCODES_DB_PASSWORD", "asdasd33"),
            },
            "s3": {
                "region": os.environ.get("RUNCODES_S3_REGION", "sa-east-1"),
                "endpoint": os.environ.get(
                    "RUNCODES_S3_ENDPOINT", "http://seaweed:8333"
                ),
                "access_key": os.environ.get("RUNCODES_S3_CREDENTIALS_KEY", "test_key"),
                "secret_key": os.environ.get("RUNCODES_S3_CREDENTIALS_SECRET", "test_secret"),
                "commits_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-commits",
                "outputfiles_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-outputfiles",
                "files_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-files",
                "cases_bucket": f"{os.environ.get('RUNCODES_S3_BUCKET_PREFIX', 'runcodes')}-cases",
                "compilation_files_dir": "compilationfiles",
            },
            "lock_file": "compiler.lock",
            "num_workers": 1,
            "min_sleep_time": 1,
            "max_sleep_time": 15,
            "exec_dir": os.environ.get("RUNCODES_COMPILER_EXEC_DIR", "/tmp"),
            "exec_dir_remote": os.environ.get("RUNCODES_COMPILER_EXEC_DIR_REMOTE", os.environ.get("RUNCODES_COMPILER_EXEC_DIR", "/tmp")),
            "src_dir": "src",
            "output_files_dir": "outputfiles",
            "max_output_file_size": 1048576,
            "compilation_error_file": "compilation.err",
            "compilation_output_file": "compilation.out",
            "compilation_timeout": 10,
            "base_exec_timeout": 5,
            "monitor_max_file_size": 5242880,
            "monitor_max_mem_size": 268435456,
            "container_cfg_file": "container.config",
            "slack": None,
            "log": None,
            "cleanup_on_error": False,
        }
        super().__init__(env_configs)


def get_config(name):
    """
    Returns the `Config` object registered to the given name or `None` if the
    name is not registered.
    """
    return __config__.get(name)


def from_json(name, fname):
    """
    Registers a new `Config` object with the given name, read from a JSON file. Returns
    the created `Config` object.
    """
    with open(fname, "r") as config_file:
        config_dict = json.load(config_file)
        c = Config(config_dict)
        __config__[name] = c
        return c


def from_dict(name, d):
    """
    Registers a new config with the given name, built from a regular `dict`.
    Returns the created `Config` object.
    """
    c = Config(d)
    __config__[name] = c
    return c


def from_env(name):
    """
    Registers a new config with the given name, building it from environment variables.
    """
    c = EnvConfig()
    __config__[name] = c
    return c


def _check_dict(d):
    if not isinstance(d, dict):
        raise TypeError("Given argument is not a dict")
    if not all(isinstance(key, str) for key in d.keys()):
        raise TypeError("All dictionary keys must be strings")
