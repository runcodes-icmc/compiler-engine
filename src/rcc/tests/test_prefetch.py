"""
Unit tests for the parallel prefetch phase of ``process_commit()`` (no docker).

The prefetch overlaps three independent IO steps — ``fetch_test_cases``,
``delete_commit_test_results`` and the commit-file S3 download — and downloads
exercise/test-case files concurrently behind a semaphore. The overlap tests
use event-based coordination: each mock fetcher waits for the other one to
*start*, so a sequential implementation would deadlock and time out.
Completing within the timeout therefore proves the operations ran
concurrently.
"""

import asyncio
import datetime
import os
import tempfile
import threading
import time
import unittest
from typing import override
from unittest import mock

import rcc.config
import rcc.engine
import rcc.provider.storage
from rcc.model import Commit, TestCase, TestCaseResult
from rcc.provider.data import DataProvider
from rcc.provider.storage import StorageProvider


def make_commit(commit_id: int = 1) -> Commit:
    return Commit(
        commit_id,
        "user{}@example.com".format(commit_id),
        1,
        1,
        Commit.STATUS_IN_QUEUE,
        "",
        0,
        0.0,
        False,
        "",
        datetime.datetime(2026, 1, 1),
        None,
        None,
        None,
        "",
        "1.2.3.4",
        "commits/{}/main.c".format(commit_id),
        1,
        1,
        1,
        "main.c",
    )


def make_cfg(exec_dir: str, **overrides: object) -> rcc.config.Config:
    values: dict[str, object] = {
        "provider": {"data": "postgres", "storage": "s3"},
        "concurrency_per_worker": 4,
        "exec_dir": exec_dir,
        "exec_dir_remote": exec_dir,
        "src_dir": "src",
        "output_files_dir": "outputfiles",
        "container_cfg_file": "container.config",
        "monitor_max_file_size": 5242880,
        "monitor_max_mem_size": 268435456,
        "compilation_timeout": 10.0,
        "base_exec_timeout": 5.0,
        "max_output_file_size": 1048576,
        "cleanup_on_error": True,
    }
    values.update(overrides)
    cfg = rcc.config.Config(values)
    # process_commit()'s helper functions read the registered default config.
    _ = rcc.config.from_dict(rcc.config.DEFAULT_CONFIG, values)
    return cfg


class NoopStorage(object):
    """Synchronous storage provider whose downloads do nothing."""

    def __init__(self, cfg: rcc.config.Config | None) -> None:
        pass

    def fetch_commit_file(self, _commit: Commit, _destination: str) -> None:
        pass

    def fetch_exercise_file(self, _source: str, _destination: str) -> None:
        pass

    def fetch_test_case_input_file(
        self, _test_case: TestCase, _destination: str
    ) -> None:
        pass

    def fetch_test_case_output_file(
        self, _test_case: TestCase, _destination: str
    ) -> None:
        pass

    def fetch_test_case_files(self, _test_case: TestCase, _destination: str) -> None:
        pass

    def store_commit_output(self, _commit: Commit, _commit_output_fname: str) -> None:
        pass


class RecordingProvider(DataProvider):
    """No-op data provider that records commit statuses and update calls."""

    statuses: dict[int, int]
    update_count: int
    delete_count: int

    def __init__(self) -> None:
        self.statuses = {}
        self.update_count = 0
        self.delete_count = 0

    @override
    async def update_commit(self, commit: Commit) -> None:
        self.update_count += 1
        self.statuses[commit.id] = commit.status

    @override
    async def store_commit_test_results(
        self, commit: Commit, test_results: list[TestCaseResult]
    ) -> None:
        pass

    @override
    async def delete_commit_test_results(self, commit: Commit) -> None:
        self.delete_count += 1

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        return []

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        return []


class EventCoordinatedProvider(RecordingProvider):
    """``fetch_test_cases`` flags its own start, then waits for the commit-file
    download to start. Completing at all proves the two overlap."""

    _fetch_started: threading.Event
    _download_started: threading.Event
    _wait_timeout: float

    def __init__(
        self,
        fetch_started: threading.Event,
        download_started: threading.Event,
        wait_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self._fetch_started = fetch_started
        self._download_started = download_started
        self._wait_timeout = wait_timeout

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        self._fetch_started.set()
        started = await asyncio.to_thread(
            self._download_started.wait, self._wait_timeout
        )
        if not started:
            raise RuntimeError(
                "commit-file download never started: prefetch is not concurrent"
            )
        return []


class EventCoordinatedStorage(NoopStorage):
    """``fetch_commit_file`` flags its own start, then waits for
    ``fetch_test_cases`` to start."""

    _fetch_started: threading.Event
    _download_started: threading.Event
    _wait_timeout: float

    def __init__(
        self,
        fetch_started: threading.Event,
        download_started: threading.Event,
        wait_timeout: float = 10.0,
    ) -> None:
        super().__init__(None)
        self._fetch_started = fetch_started
        self._download_started = download_started
        self._wait_timeout = wait_timeout

    @override
    def fetch_commit_file(self, commit: Commit, destination: str) -> None:
        # Runs in a worker thread (asyncio.to_thread): threading.Event is the
        # thread-safe signalling primitive here.
        self._download_started.set()
        if not self._fetch_started.wait(self._wait_timeout):
            raise RuntimeError(
                "fetch_test_cases never started: prefetch is not concurrent"
            )


class CountingStorage(NoopStorage):
    """Tracks how many downloads run at the same time."""

    _lock: threading.Lock
    _sleep: float
    active: int
    max_active: int

    def __init__(self, cfg: rcc.config.Config | None, sleep: float = 0.05) -> None:
        super().__init__(cfg)
        self._lock = threading.Lock()
        self._sleep = sleep
        self.active = 0
        self.max_active = 0

    def _download(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self._sleep)
        finally:
            with self._lock:
                self.active -= 1

    @override
    def fetch_commit_file(self, commit: Commit, destination: str) -> None:
        self._download()

    @override
    def fetch_exercise_file(self, source: str, destination: str) -> None:
        self._download()


class ExerciseFilesProvider(RecordingProvider):
    _fnames: list[str]

    def __init__(self, fnames: list[str]) -> None:
        super().__init__()
        self._fnames = fnames

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        return list(self._fnames)


class TestPrefetch(unittest.IsolatedAsyncioTestCase):
    async def _fake_run_tests(
        self,
        _data_provider: DataProvider,
        _storage_provider: StorageProvider,
        _commit: Commit,
        _test_cases: list[TestCase],
        _base_dir: str,
        _remote_dir: str,
    ) -> list[TestCaseResult]:
        # The container phase is out of scope: pretend every test passed.
        return []

    async def test_fetch_test_cases_and_commit_download_overlap(self) -> None:
        """The two first-step IO operations must run at the same time.

        Each mock waits for the other one to *start*: a sequential prefetch
        would wait forever and the ``wait_for`` timeout would fail the test.
        """
        fetch_started = threading.Event()
        download_started = threading.Event()
        provider = EventCoordinatedProvider(fetch_started, download_started)
        storage = EventCoordinatedStorage(fetch_started, download_started)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir)
            commit = make_commit()
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
            ):
                await asyncio.wait_for(
                    rcc.engine.process_commit(provider, commit, cfg), timeout=5
                )

        self.assertEqual(commit.status, Commit.STATUS_COMPLETED)
        self.assertEqual(provider.delete_count, 1)
        self.assertEqual(provider.statuses[commit.id], Commit.STATUS_COMPLETED)

    async def test_processing_update_not_delayed_by_download(self) -> None:
        """A slow commit-file download must not delay STATUS_PROCESSING.

        The poller re-enqueues every commit it still sees as STATUS_IN_QUEUE,
        so the PROCESSING write has to land as soon as the DB prefetch pair
        finishes; otherwise a slow S3 leaves the commit queued for the whole
        download time and several workers process copies of it (colliding on
        the same base_dir).
        """
        download_entered = threading.Event()
        release_download = threading.Event()
        processing_written = asyncio.Event()

        class WatchingProvider(RecordingProvider):
            @override
            async def update_commit(self, commit: Commit) -> None:
                await super().update_commit(commit)
                if commit.status == Commit.STATUS_PROCESSING:
                    processing_written.set()

        class BlockingStorage(NoopStorage):
            @override
            def fetch_commit_file(self, commit: Commit, destination: str) -> None:
                download_entered.set()
                if not release_download.wait(5):
                    raise RuntimeError("download never released")

        provider = WatchingProvider()
        storage = BlockingStorage(None)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir)
            commit = make_commit()
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
            ):
                task = asyncio.create_task(
                    rcc.engine.process_commit(provider, commit, cfg)
                )
                try:
                    # Wait until the download has started (and is blocked).
                    _ = await asyncio.to_thread(download_entered.wait, 5)
                    # The PROCESSING update must land while the download is
                    # still blocked; a regression that awaits the download
                    # first would time out here.
                    _ = await asyncio.wait_for(processing_written.wait(), timeout=2)
                finally:
                    release_download.set()
                await task

        self.assertEqual(commit.status, Commit.STATUS_COMPLETED)
        self.assertEqual(provider.statuses[commit.id], Commit.STATUS_COMPLETED)

    async def test_exercise_downloads_run_concurrently_and_bounded(self) -> None:
        """Exercise files download in parallel, capped at the semaphore bound."""
        storage = CountingStorage(None, sleep=0.05)
        provider = ExerciseFilesProvider(["f{}.c".format(i) for i in range(6)])

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir, concurrency_per_worker=2)
            commit = make_commit()
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
            ):
                await rcc.engine.process_commit(provider, commit, cfg)

        # Six independent downloads bounded by min(concurrency_per_worker,
        # PREFETCH_MAX_CONCURRENT_DOWNLOADS) = 2: they must overlap (more than
        # one at a time) and never exceed the bound. The single commit-file
        # download of the prefetch phase contributes at most max_active == 1.
        self.assertGreater(storage.max_active, 1)
        self.assertLessEqual(storage.max_active, 2)

    async def test_fetch_test_cases_failure_yields_internal_error(self) -> None:
        class FailingProvider(RecordingProvider):
            @override
            async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
                raise RuntimeError("db unavailable")

        provider = FailingProvider()
        storage = NoopStorage(None)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir)  # cleanup_on_error=True
            commit = make_commit()
            base_dir = os.path.join(tmpdir, "commit_{}".format(commit.id))
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
                self.assertLogs(rcc.config.DEFAULT_LOGGER, level="ERROR") as logs,
            ):
                await rcc.engine.process_commit(provider, commit, cfg)

        self.assertEqual(commit.status, Commit.STATUS_INTERNAL_ERROR)
        self.assertEqual(provider.statuses[commit.id], Commit.STATUS_INTERNAL_ERROR)
        # cleanup_on_error removed the work directory.
        self.assertFalse(os.path.exists(base_dir))
        self.assertTrue(any("Failed to fetch test cases" in r for r in logs.output))

    async def test_commit_download_failure_yields_internal_error(self) -> None:
        class FailingStorage(NoopStorage):
            @override
            def fetch_commit_file(self, commit: Commit, destination: str) -> None:
                raise RuntimeError("s3 unavailable")

        provider = RecordingProvider()
        storage = FailingStorage(None)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir)
            commit = make_commit()
            base_dir = os.path.join(tmpdir, "commit_{}".format(commit.id))
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
                self.assertLogs(rcc.config.DEFAULT_LOGGER, level="ERROR") as logs,
            ):
                await rcc.engine.process_commit(provider, commit, cfg)

        self.assertEqual(commit.status, Commit.STATUS_INTERNAL_ERROR)
        self.assertFalse(os.path.exists(base_dir))
        self.assertTrue(any("Failed to prepare runs" in r for r in logs.output))

    async def test_delete_failure_propagates_and_keeps_commit_queued(self) -> None:
        class FailingProvider(RecordingProvider):
            @override
            async def delete_commit_test_results(self, commit: Commit) -> None:
                raise RuntimeError("delete failed")

        provider = FailingProvider()
        storage = NoopStorage(None)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(tmpdir)
            commit = make_commit()
            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", self._fake_run_tests),
            ):
                with self.assertRaisesRegex(RuntimeError, "delete failed"):
                    await rcc.engine.process_commit(provider, commit, cfg)

        # The commit was never reset nor marked PROCESSING: it stays in the
        # queue to be retried, exactly like the sequential version.
        self.assertEqual(commit.status, Commit.STATUS_IN_QUEUE)
        self.assertEqual(provider.update_count, 0)


if __name__ == "__main__":
    _ = unittest.main()
