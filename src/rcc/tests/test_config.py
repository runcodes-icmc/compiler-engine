"""
Tests for configuration defaults, parallelism validation and helpers.

No external services required: these tests exercise ``EnvConfig`` (with a
scrubbed environment), the parallelism helpers (``parallelism_values``,
``total_slots``, ``queue_maxsize``) and ``validate`` directly.
"""

import os
import unittest
from typing import cast, override
from unittest import mock

import rcc.config
from rcc.config import Config, ConfigError, EnvConfig


class TestEnvConfigParallelismDefaults(unittest.TestCase):
    _saved_environ: dict[str, str] | None

    @override
    def __init__(self, method_name: str = "runTest") -> None:
        self._saved_environ = None
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        # Isolate every test from the host environment: env vars must not
        # leak into the defaults under test.
        self._saved_environ = dict(os.environ)
        os.environ.clear()

    @override
    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ or {})

    def test_num_workers_defaults_to_two(self) -> None:
        self.assertEqual(rcc.config.DEFAULT_NUM_WORKERS, 2)
        cfg = EnvConfig()
        self.assertEqual(int(str(cfg.num_workers)), 2)

    def test_concurrency_defaults_to_four(self) -> None:
        cfg = EnvConfig()
        self.assertEqual(
            int(str(cfg.concurrency_per_worker)),
            rcc.config.DEFAULT_CONCURRENCY_PER_WORKER,
        )
        self.assertEqual(rcc.config.DEFAULT_CONCURRENCY_PER_WORKER, 4)

    def test_env_vars_override_worker_count_and_concurrency(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUNCODES_COMPILER_NUM_WORKERS": "3",
                "RUNCODES_COMPILER_CONCURRENCY": "5",
            },
            clear=True,
        ):
            cfg = EnvConfig()
        self.assertEqual(rcc.config.parallelism_values(cfg), (3, 5))
        self.assertEqual(rcc.config.total_slots(cfg), 15)

    def test_total_in_flight_is_workers_times_concurrency(self) -> None:
        cfg = Config({"num_workers": 2, "concurrency_per_worker": 4})
        self.assertEqual(rcc.config.total_slots(cfg), 8)

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        cfg = Config({})
        self.assertEqual(
            rcc.config.parallelism_values(cfg),
            (
                rcc.config.DEFAULT_NUM_WORKERS,
                rcc.config.DEFAULT_CONCURRENCY_PER_WORKER,
            ),
        )

    def test_pool_max_size_absent_when_env_var_unset(self) -> None:
        # The Postgres provider derives the maximum from the concurrency
        # when RUNCODES_DB_POOL_MAX_SIZE is not configured explicitly.
        cfg = EnvConfig()
        db = cast(dict[str, object], cfg.get_dict()["db"])
        self.assertNotIn("pool_max_size", db)

    def test_pool_max_size_present_when_env_var_set(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUNCODES_DB_POOL_MAX_SIZE": "20"}, clear=True
        ):
            cfg = EnvConfig()
        db = cast(dict[str, object], cfg.get_dict()["db"])
        self.assertIn("pool_max_size", db)
        self.assertEqual(int(str(db["pool_max_size"])), 20)

    def test_non_integer_env_worker_count_raises_clear_error(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"RUNCODES_COMPILER_NUM_WORKERS": "many"}, clear=True
            ),
            self.assertRaises(ConfigError) as raised,
        ):
            _ = EnvConfig()
        self.assertIn("RUNCODES_COMPILER_NUM_WORKERS", str(raised.exception))

    def test_non_integer_env_concurrency_raises_clear_error(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"RUNCODES_COMPILER_CONCURRENCY": "4.5"}, clear=True
            ),
            self.assertRaises(ConfigError) as raised,
        ):
            _ = EnvConfig()
        self.assertIn("RUNCODES_COMPILER_CONCURRENCY", str(raised.exception))


class TestParallelismValidation(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        cfg = Config({"num_workers": 2, "concurrency_per_worker": 4})
        rcc.config.validate(cfg)  # must not raise

    def test_config_with_only_defaults_passes(self) -> None:
        rcc.config.validate(Config({}))

    def test_zero_workers_rejected(self) -> None:
        cfg = Config({"num_workers": 0, "concurrency_per_worker": 4})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_negative_workers_rejected(self) -> None:
        cfg = Config({"num_workers": -1, "concurrency_per_worker": 4})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_zero_concurrency_rejected(self) -> None:
        # A semaphore of size 0 would deadlock every worker.
        cfg = Config({"num_workers": 2, "concurrency_per_worker": 0})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_negative_concurrency_rejected(self) -> None:
        cfg = Config({"num_workers": 2, "concurrency_per_worker": -3})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_non_integer_worker_count_rejected(self) -> None:
        cfg = Config({"num_workers": "many", "concurrency_per_worker": 4})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_non_integer_concurrency_rejected(self) -> None:
        cfg = Config({"num_workers": 2, "concurrency_per_worker": None})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_error_message_names_the_offending_key(self) -> None:
        cfg = Config({"num_workers": 0, "concurrency_per_worker": 4})
        with self.assertRaises(ConfigError) as raised:
            rcc.config.validate(cfg)
        self.assertIn("num_workers", str(raised.exception))


class TestQueueMaxsize(unittest.TestCase):
    def test_two_times_total_slots(self) -> None:
        cfg = Config({"num_workers": 3, "concurrency_per_worker": 4})
        self.assertEqual(rcc.config.queue_maxsize(cfg), 24)

    def test_falls_back_to_default_concurrency(self) -> None:
        cfg = Config({"num_workers": 2})
        expected = 2 * 2 * rcc.config.DEFAULT_CONCURRENCY_PER_WORKER
        self.assertEqual(rcc.config.queue_maxsize(cfg), expected)

    def test_at_least_total_slots(self) -> None:
        for workers in (1, 2, 5):
            cfg = Config({"num_workers": workers, "concurrency_per_worker": 1})
            self.assertGreaterEqual(
                rcc.config.queue_maxsize(cfg), rcc.config.total_slots(cfg)
            )


if __name__ == "__main__":
    _ = unittest.main()
