"""
Tests for configuration defaults, validation and helpers.

No external services required: these tests exercise ``EnvConfig`` (with a
scrubbed environment), ``get_concurrency``, ``queue_maxsize`` and ``validate``
directly.
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

    def test_concurrency_defaults_to_eight(self) -> None:
        self.assertEqual(rcc.config.DEFAULT_CONCURRENCY, 8)
        cfg = EnvConfig()
        self.assertEqual(int(str(cfg.concurrency)), 8)

    def test_env_var_overrides_concurrency(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUNCODES_COMPILER_CONCURRENCY": "5"}, clear=True
        ):
            cfg = EnvConfig()
        self.assertEqual(rcc.config.get_concurrency(cfg), 5)

    def test_missing_key_falls_back_to_default(self) -> None:
        cfg = Config({})
        self.assertEqual(
            rcc.config.get_concurrency(cfg), rcc.config.DEFAULT_CONCURRENCY
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

    def test_non_integer_env_concurrency_raises_clear_error(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"RUNCODES_COMPILER_CONCURRENCY": "4.5"}, clear=True
            ),
            self.assertRaises(ConfigError) as raised,
        ):
            _ = EnvConfig()
        self.assertIn("RUNCODES_COMPILER_CONCURRENCY", str(raised.exception))


class TestConcurrencyValidation(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        cfg = Config({"concurrency": 4})
        rcc.config.validate(cfg)  # must not raise

    def test_config_with_only_defaults_passes(self) -> None:
        rcc.config.validate(Config({}))

    def test_zero_concurrency_rejected(self) -> None:
        # A semaphore of size 0 would deadlock the consumer.
        cfg = Config({"concurrency": 0})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_negative_concurrency_rejected(self) -> None:
        cfg = Config({"concurrency": -3})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_non_integer_concurrency_rejected(self) -> None:
        cfg = Config({"concurrency": None})
        with self.assertRaises(ConfigError):
            rcc.config.validate(cfg)

    def test_error_message_names_the_offending_key(self) -> None:
        cfg = Config({"concurrency": 0})
        with self.assertRaises(ConfigError) as raised:
            rcc.config.validate(cfg)
        self.assertIn("concurrency", str(raised.exception))


class TestQueueMaxsize(unittest.TestCase):
    def test_two_times_concurrency(self) -> None:
        cfg = Config({"concurrency": 4})
        self.assertEqual(rcc.config.queue_maxsize(cfg), 8)

    def test_falls_back_to_default_concurrency(self) -> None:
        cfg = Config({})
        expected = 2 * rcc.config.DEFAULT_CONCURRENCY
        self.assertEqual(rcc.config.queue_maxsize(cfg), expected)

    def test_at_least_concurrency(self) -> None:
        for concurrency in (1, 2, 5):
            cfg = Config({"concurrency": concurrency})
            self.assertGreaterEqual(
                rcc.config.queue_maxsize(cfg), rcc.config.get_concurrency(cfg)
            )


if __name__ == "__main__":
    _ = unittest.main()
