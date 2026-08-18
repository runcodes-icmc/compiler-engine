"""
Unit tests for the parallel commit pipeline (no docker daemon required).

Covers:
- the container log reader thread adapter (ordering, sentinel, timeouts),
- the per-worker semaphore concurrency (M slots, no slot leaks, stop hints),
- the bounded task queue backpressure in ``rcc.main()``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import multiprocessing as mp
import multiprocessing.queues as mp_queues
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable
from typing import ClassVar, Self, cast, override
from unittest import mock

import rcc
import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
import rcc.util
from rcc.model import Commit, TestCase, TestCaseResult
from rcc.provider.data import DataProvider


def make_commit(commit_id: int) -> Commit:
    return Commit(
        commit_id,
        f"user{commit_id}@example.com",
        1,
        1,
        Commit.STATUS_IN_QUEUE,
        "",
        0,
        0.0,
        False,
        "",
        datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        None,
        None,
        None,
        "",
        "1.2.3.4",
        f"commits/{commit_id}/main.c",
        1,
        1,
        1,
        "main.c",
    )


def make_cfg(concurrency: int, exec_dir: str | None = None) -> rcc.config.Config:
    if exec_dir is None:
        exec_dir = tempfile.gettempdir()
    return rcc.config.Config(
        {
            "provider": {"data": "postgres", "storage": "s3"},
            "concurrency_per_worker": concurrency,
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
            "cleanup_on_error": False,
        }
    )


class FakeStorage:
    """Synchronous no-op storage provider (its methods run in worker threads)."""

    def __init__(self, cfg: rcc.config.Config) -> None:
        pass

    def fetch_commit_file(self, _commit: Commit, _destination: str) -> None:
        pass

    def fetch_exercise_file(self, _source: str, _destination: str) -> None:
        pass

    def fetch_test_case_input_file(self, _test_case: object, _destination: str) -> None:
        pass

    def fetch_test_case_output_file(
        self, _test_case: object, _destination: str
    ) -> None:
        pass

    def fetch_test_case_files(self, _test_case: object, _destination: str) -> None:
        pass

    def store_commit_output(self, _commit: Commit, _commit_output_fname: str) -> None:
        pass


class TrackingProvider(rcc.provider.data.DataProvider):
    """Counts pool lifecycle events and records commit statuses; no real DB."""

    open_count: int
    close_count: int
    commit_statuses: dict[int, int]

    def __init__(self) -> None:
        self.open_count = 0
        self.close_count = 0
        self.commit_statuses = {}

    @override
    async def open(self) -> None:
        self.open_count += 1

    @override
    async def close(self) -> None:
        self.close_count += 1

    @override
    async def fetch_commits_in_queue(self) -> list[Commit]:
        return []

    @override
    async def update_commit(self, commit: Commit) -> None:
        # The last status written for a commit is its final status.
        self.commit_statuses[commit.id] = commit.status

    @override
    async def store_commit_test_results(
        self, commit: Commit, test_results: list[TestCaseResult]
    ) -> None:
        pass

    @override
    async def delete_commit_test_results(self, commit: Commit) -> None:
        pass

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        return []

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        return []


class ClaimingProvider(TrackingProvider):
    """Mimics the Postgres claim semantics: first claim per commit wins."""

    claimed: set[int]
    claim_count: int
    release_count: int

    def __init__(self) -> None:
        super().__init__()
        self.claimed = set()
        self.claim_count = 0
        self.release_count = 0

    @override
    async def claim_commit(self, commit: Commit) -> bool:
        self.claim_count += 1
        if commit.id in self.claimed:
            return False
        self.claimed.add(commit.id)
        return True

    @override
    async def release_commit(self, commit: Commit) -> None:
        self.release_count += 1
        self.claimed.discard(commit.id)


def unfinished_tasks(q: object) -> int:
    """Number of items put on a JoinableQueue but not yet task_done()'d.

    ``multiprocessing.queues.JoinableQueue`` keeps the counter in a private
    ``Semaphore`` and does not expose it as an attribute (unlike
    ``queue.Queue``).
    """
    unfinished = cast(object, q._unfinished_tasks)
    semlock = cast(object, unfinished._semlock)
    get_value = cast(Callable[[], int], semlock._get_value)
    return get_value()


class TestContainerLogReader(unittest.IsolatedAsyncioTestCase):
    def _reader(
        self, generator: Iterable[bytes | str]
    ) -> rcc.engine.ContainerLogReader:
        reader = rcc.engine.ContainerLogReader(generator, asyncio.get_running_loop())
        reader.start()
        return reader

    async def test_lines_are_delivered_in_order(self) -> None:
        def gen() -> Iterable[bytes]:
            yield b"compilation.start\n"
            yield b"  hello\n"
            yield b"compilation.done"

        reader = self._reader(gen())
        self.assertEqual(await reader.get(), "compilation.start")
        self.assertEqual(await reader.get(), "hello")
        self.assertEqual(await reader.get(), "compilation.done")
        self.assertIs(await reader.get(), rcc.engine.ContainerLogReader.END)
        reader.stop()

    async def test_sentinel_pushed_when_generator_is_empty(self) -> None:
        reader = self._reader(iter(()))
        self.assertIs(
            await asyncio.wait_for(reader.get(), 1.0),
            rcc.engine.ContainerLogReader.END,
        )
        reader.stop()

    async def test_generator_error_is_treated_as_end_of_stream(self) -> None:
        def gen() -> Iterable[bytes]:
            yield b"first"
            raise ConnectionError("socket closed")

        reader = self._reader(gen())
        self.assertEqual(await reader.get(), "first")
        self.assertIs(
            await asyncio.wait_for(reader.get(), 1.0),
            rcc.engine.ContainerLogReader.END,
        )
        reader.stop()

    async def test_get_times_out_when_generator_stalls(self) -> None:
        def gen() -> Iterable[bytes]:
            time.sleep(30)
            yield b"late"

        reader = self._reader(gen())
        with self.assertRaises(asyncio.TimeoutError):
            _ = await asyncio.wait_for(reader.get(), 0.05)

    async def test_expect_message_matches(self) -> None:
        reader = self._reader(iter([b"compilation.start"]))
        await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()

    async def test_expect_message_wrong_line_raises(self) -> None:
        reader = self._reader(iter([b"something.else"]))
        with self.assertRaises(RuntimeError):
            await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()

    async def test_expect_message_timeout_raises(self) -> None:
        def gen() -> Iterable[bytes]:
            time.sleep(30)
            yield b"late"

        reader = self._reader(gen())
        with self.assertRaises(asyncio.TimeoutError):
            await rcc.engine.expect_message(reader, "compilation.start", 0.05)

    async def test_expect_message_stream_end_raises(self) -> None:
        reader = self._reader(iter(()))
        with self.assertRaises(RuntimeError):
            await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()


class TestWorkerConcurrency(unittest.IsolatedAsyncioTestCase):
    async def _drive(
        self,
        commits: list[Commit],
        concurrency: int,
        fake_process_commit: Callable[..., object],
    ) -> tuple[TrackingProvider, mp_queues.JoinableQueue[Commit | None]]:
        provider = TrackingProvider()
        cfg = make_cfg(concurrency)
        task_queue: mp_queues.JoinableQueue[Commit | None] = mp.JoinableQueue()
        for commit in commits:
            task_queue.put(commit)
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake_process_commit):
            await rcc.engine.process_commits(provider, task_queue, cfg)
        return provider, task_queue

    async def test_at_most_M_commits_in_flight(self) -> None:
        active = 0
        max_active = 0
        finished: list[int] = []

        async def fake(
            _data_provider: DataProvider,
            commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.05)
            finally:
                active -= 1
            finished.append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(i) for i in range(5)], 2, fake
        )
        self.assertEqual(max_active, 2)
        self.assertEqual(sorted(finished), [0, 1, 2, 3, 4])
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_stop_hint_drains_in_flight_commits(self) -> None:
        finished: list[int] = []

        async def fake(
            _data_provider: DataProvider,
            commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            await asyncio.sleep(0.1)
            finished.append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(1), make_commit(2), make_commit(3)], 2, fake
        )
        self.assertEqual(sorted(finished), [1, 2, 3])
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_retryable_failure_does_not_leak_a_slot(self) -> None:
        active = 0
        max_active = 0
        finished: list[int] = []
        failed = False

        async def fake(
            _data_provider: DataProvider,
            commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            nonlocal active, max_active, failed
            if commit.id == 1:
                failed = True
                raise RuntimeError("retryable failure")
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.1)
            finally:
                active -= 1
            finished.append(commit.id)

        _, task_queue = await self._drive(
            [make_commit(i) for i in (1, 2, 3, 4, 5)], 2, fake
        )
        self.assertTrue(failed)
        # The failing commit's slot was released: the remaining commits still
        # run two at a time (with a leaked slot they would be serialized).
        self.assertEqual(sorted(finished), [2, 3, 4, 5])
        self.assertEqual(max_active, 2)
        self.assertEqual(unfinished_tasks(task_queue), 0)

    async def test_non_retryable_failure_stops_the_worker(self) -> None:
        finished: list[int] = []
        fatal = False

        async def fake(
            _data_provider: DataProvider,
            commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            nonlocal fatal
            if commit.id == 1:
                fatal = True
                raise MemoryError("boom")
            await asyncio.sleep(0.05)
            finished.append(commit.id)

        provider, _ = await self._drive(
            [make_commit(1), make_commit(2), make_commit(3)], 2, fake
        )
        self.assertTrue(fatal)
        # The failing commit never completes; the commits already pulled and
        # spawned before the failure was observed (commit 2, and — depending
        # on scheduling — commit 3) are drained before the worker exits.
        self.assertNotIn(1, finished)
        self.assertIn(2, finished)
        self.assertTrue(all(cid in (2, 3) for cid in finished))
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_duplicate_commits_are_processed_once(self) -> None:
        """Copies of the same commit (the poller re-enqueues IN_QUEUE
        commits) are claimed by exactly one worker."""
        processed: list[int] = []

        async def fake(
            _data_provider: DataProvider,
            commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            processed.append(commit.id)

        provider = ClaimingProvider()
        cfg = make_cfg(2)
        task_queue: mp_queues.JoinableQueue[Commit | None] = mp.JoinableQueue()
        for commit in (make_commit(1), make_commit(1), make_commit(2)):
            task_queue.put(commit)
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake):
            await rcc.engine.process_commits(provider, task_queue, cfg)

        # The duplicate copy of commit 1 was claimed and skipped.
        self.assertEqual(sorted(processed), [1, 2])
        self.assertEqual(provider.claim_count, 3)
        self.assertEqual(provider.claimed, {1, 2})
        self.assertEqual(unfinished_tasks(task_queue), 0)

    async def test_retryable_failure_releases_the_claim(self) -> None:
        """A claimed commit that fails retryably goes back to IN_QUEUE."""

        async def fake(
            _data_provider: DataProvider,
            _commit: Commit,
            _cfg: rcc.config.Config | None = None,
        ) -> None:
            raise RuntimeError("retryable failure")

        provider = ClaimingProvider()
        cfg = make_cfg(2)
        task_queue: mp_queues.JoinableQueue[Commit | None] = mp.JoinableQueue()
        task_queue.put(make_commit(1))
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake):
            await rcc.engine.process_commits(provider, task_queue, cfg)

        self.assertEqual(provider.claim_count, 1)
        self.assertEqual(provider.release_count, 1)
        # The claim was given back: a later pull may take the commit again.
        self.assertEqual(provider.claimed, set())
        self.assertEqual(unfinished_tasks(task_queue), 0)


class TestProcessCommitIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_commits_processed_concurrently(self) -> None:
        """Run the real process_commit() pipeline with fakes (no docker)."""
        active = 0
        max_active = 0

        async def fake_run_tests(
            _data_provider: DataProvider,
            _storage_provider: object,
            _commit: Commit,
            _test_cases: list[object],
            _base_dir: str,
            _remote_dir: str,
        ) -> list[TestCaseResult]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.1)
            finally:
                active -= 1
            return []

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(2, exec_dir=tmpdir)
            provider = TrackingProvider()
            storage = FakeStorage(cfg)
            task_queue: mp_queues.JoinableQueue[Commit | None] = mp.JoinableQueue()
            commits = [make_commit(i) for i in range(3)]
            for commit in commits:
                task_queue.put(commit)
            task_queue.put(None)

            with (
                mock.patch.object(
                    rcc.provider.storage, "from_config", return_value=storage
                ),
                mock.patch.object(rcc.engine, "run_tests", fake_run_tests),
            ):
                await rcc.engine.process_commits(provider, task_queue, cfg)

        self.assertEqual(max_active, 2)
        # The multiprocessing queue pickles items, so the worker mutated
        # copies; observe the final statuses through the provider instead.
        for commit in commits:
            self.assertEqual(
                provider.commit_statuses[commit.id], Commit.STATUS_COMPLETED
            )
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)


class RecordingJoinableQueue(mp_queues.JoinableQueue[Commit | None]):
    """Records the maxsize used to construct the queue.

    ``multiprocessing.JoinableQueue`` is a factory function, so the concrete
    class from ``multiprocessing.queues`` is subclassed instead.
    """

    instances: ClassVar[list[RecordingJoinableQueue]] = []

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize, ctx=mp.get_context())
        RecordingJoinableQueue.instances.append(self)


class RecordingPutQueue(mp_queues.JoinableQueue[Commit | None]):
    """JoinableQueue that records the ids of everything put into it."""

    instances: ClassVar[list[RecordingPutQueue]] = []
    put_ids: list[int]

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize, ctx=mp.get_context())
        self.put_ids = []
        RecordingPutQueue.instances.append(self)

    @override
    def put(
        self,
        obj: Commit | None,
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        if obj is not None:
            self.put_ids.append(obj.id)
        super().put(obj, block, timeout)


class FakeProcess:
    """Runs the mp.Process target in a daemon thread (no real fork/spawn)."""

    _thread: threading.Thread

    def __init__(
        self, target: Callable[..., object], args: tuple[object, ...] = ()
    ) -> None:
        self._thread = threading.Thread(target=target, args=args, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self) -> None:
        self._thread.join()

    def terminate(self) -> None:
        pass


class _NullSingletonContext:
    def __init__(self, lock_fname: str, remove_lock_at_exit: bool = True) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: object,
    ) -> bool:
        return False


WORKER_COMMIT_SECONDS = 0.3


def fake_worker(
    _data_provider: DataProvider,
    task_queue: mp_queues.JoinableQueue[Commit | None],
    _cfg: rcc.config.Config,
) -> None:
    """Stand-in for ``rcc.engine.run_worker`` inside a FakeProcess thread.

    Mirrors the real worker's queue bookkeeping: one task_done() per pulled
    item, including the None stop hint.
    """
    while True:
        item = task_queue.get()
        if item is None:
            task_queue.task_done()
            return
        time.sleep(WORKER_COMMIT_SECONDS)
        task_queue.task_done()


class PollingProvider(rcc.provider.data.DataProvider):
    _commits: list[Commit]
    fetch_count: int
    open_count: int
    close_count: int

    def __init__(self, commits: list[Commit]) -> None:
        self._commits = commits
        self.fetch_count = 0
        self.open_count = 0
        self.close_count = 0

    @override
    async def open(self) -> None:
        self.open_count += 1

    @override
    async def close(self) -> None:
        self.close_count += 1

    @override
    async def fetch_commits_in_queue(self) -> list[Commit]:
        self.fetch_count += 1
        if self.fetch_count == 1:
            return list(self._commits)
        return []

    @override
    async def update_commit(self, commit: Commit) -> None:
        pass

    @override
    async def store_commit_test_results(
        self, commit: Commit, test_results: list[TestCaseResult]
    ) -> None:
        pass

    @override
    async def delete_commit_test_results(self, commit: Commit) -> None:
        pass

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        return []

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        return []


class RepeatingProvider(rcc.provider.data.DataProvider):
    """Returns the same commit on every poll cycle; counts the cycles."""

    _commit: Commit
    fetch_count: int

    def __init__(self, commit: Commit) -> None:
        self._commit = commit
        self.fetch_count = 0

    @override
    async def open(self) -> None:
        pass

    @override
    async def close(self) -> None:
        pass

    @override
    async def fetch_commits_in_queue(self) -> list[Commit]:
        self.fetch_count += 1
        return [self._commit]


class TestMainBackpressure(unittest.IsolatedAsyncioTestCase):
    def test_queue_maxsize_is_two_times_total_slots(self) -> None:
        cfg = rcc.config.Config({"num_workers": 3, "concurrency_per_worker": 4})
        self.assertEqual(rcc.task_queue_maxsize(cfg), 24)

    def test_queue_maxsize_falls_back_to_default_concurrency(self) -> None:
        cfg = rcc.config.Config({"num_workers": 2})
        expected = 2 * 2 * rcc.config.DEFAULT_CONCURRENCY_PER_WORKER
        self.assertEqual(rcc.task_queue_maxsize(cfg), expected)

    async def test_polling_loop_blocks_on_a_full_queue(self) -> None:
        provider = PollingProvider([make_commit(i) for i in range(5)])
        cfg = rcc.config.Config(
            {
                "provider": {"data": "postgres", "storage": "s3"},
                "num_workers": 1,
                "concurrency_per_worker": 1,  # -> bounded queue of size 2
                "min_sleep_time": 0.01,
                "max_sleep_time": 0.01,
                "lock_file": "compiler.lock",
                "log": None,
            }
        )
        RecordingJoinableQueue.instances = []

        patches = [
            mock.patch.object(rcc.config, "from_env", return_value=cfg),
            mock.patch.object(
                rcc, "parse_args", return_value=argparse.Namespace(config="env")
            ),
            mock.patch.object(
                rcc, "setup_logger", return_value=logging.getLogger("rcc.tests.main")
            ),
            mock.patch.object(rcc.util, "SingletonContext", _NullSingletonContext),
            mock.patch.object(rcc.provider.data, "from_config", return_value=provider),
            mock.patch.object(rcc.engine, "run_worker", fake_worker),
            mock.patch.object(mp, "Process", FakeProcess),
            mock.patch.object(mp, "JoinableQueue", RecordingJoinableQueue),
        ]
        for patch in patches:
            _ = patch.start()
        try:
            main_task = asyncio.create_task(rcc.main())
            await asyncio.sleep(0.5)

            # The bounded queue (maxsize 2) filled after two instant puts; the
            # third put blocks until the slow fake worker consumes one, so the
            # polling loop must not have fetched a second batch yet.
            self.assertEqual(provider.fetch_count, 1)
            (task_queue,) = RecordingJoinableQueue.instances
            self.assertEqual(cast(object, task_queue._maxsize), 2)

            # Simulate Ctrl-C (the first Ctrl-C cancels main()).
            _ = main_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main_task
        finally:
            for patch in reversed(patches):
                patch.stop()

        # The shutdown sequence ran: worker drained the queue (including the
        # None hint) and the provider pool was opened and closed once.
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)
        self.assertEqual(unfinished_tasks(task_queue), 0)

    async def _run_main_with_repeating_commit(
        self, provider: DataProvider, cfg: rcc.config.Config, runtime: float
    ) -> RecordingPutQueue:
        """Drive rcc.main() for ``runtime`` seconds, then cancel it."""
        RecordingPutQueue.instances = []
        patches = [
            mock.patch.object(rcc.config, "from_env", return_value=cfg),
            mock.patch.object(
                rcc, "parse_args", return_value=argparse.Namespace(config="env")
            ),
            mock.patch.object(
                rcc, "setup_logger", return_value=logging.getLogger("rcc.tests.main")
            ),
            mock.patch.object(rcc.util, "SingletonContext", _NullSingletonContext),
            mock.patch.object(rcc.provider.data, "from_config", return_value=provider),
            mock.patch.object(rcc.engine, "run_worker", fake_worker),
            mock.patch.object(mp, "Process", FakeProcess),
            mock.patch.object(mp, "JoinableQueue", RecordingPutQueue),
        ]
        for patch in patches:
            _ = patch.start()
        try:
            main_task = asyncio.create_task(rcc.main())
            await asyncio.sleep(runtime)
            _ = main_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main_task
        finally:
            for patch in reversed(patches):
                patch.stop()
        (task_queue,) = RecordingPutQueue.instances
        return task_queue

    async def test_poller_suppresses_recent_commits(self) -> None:
        """A commit that stays IN_QUEUE is put on the queue only once."""
        provider = RepeatingProvider(make_commit(1))
        cfg = rcc.config.Config(
            {
                "provider": {"data": "postgres", "storage": "s3"},
                "num_workers": 1,
                "concurrency_per_worker": 1,
                "min_sleep_time": 0.02,
                "max_sleep_time": 0.02,
                "lock_file": "compiler.lock",
                "log": None,
            }
        )

        task_queue = await self._run_main_with_repeating_commit(
            provider, cfg, runtime=0.6
        )

        # The poller fetched several times but only enqueued the commit once.
        self.assertGreaterEqual(provider.fetch_count, 2)
        self.assertEqual(task_queue.put_ids, [1])

    async def test_poller_reenqueues_after_suppression_expires(self) -> None:
        """A commit still IN_QUEUE after the window is enqueued again."""
        provider = RepeatingProvider(make_commit(1))
        cfg = rcc.config.Config(
            {
                "provider": {"data": "postgres", "storage": "s3"},
                "num_workers": 1,
                "concurrency_per_worker": 1,
                "min_sleep_time": 0.03,
                "max_sleep_time": 0.03,
                "lock_file": "compiler.lock",
                "log": None,
                "commit_enqueue_suppression": 0.1,
            }
        )

        task_queue = await self._run_main_with_repeating_commit(
            provider, cfg, runtime=0.8
        )

        # After the 0.1 s window expired the poller re-enqueued the commit on
        # the following cycles (a worker may have died before claiming it).
        self.assertGreaterEqual(task_queue.put_ids.count(1), 3)


class TestSelectNewCommits(unittest.TestCase):
    """Unit tests for the poller's duplicate-enqueue suppression helper."""

    def test_recent_commits_are_suppressed_and_tracking_is_pruned(self) -> None:
        tracker: dict[int, float] = {}
        c1, c2 = make_commit(1), make_commit(2)

        # First appearance: both are new.
        new = rcc.select_new_commits([c1, c2], tracker, 60)
        self.assertEqual([c.id for c in new], [1, 2])
        tracker[1] = time.monotonic()
        tracker[2] = time.monotonic()

        # Still IN_QUEUE within the window: suppressed.
        self.assertEqual(rcc.select_new_commits([c1, c2], tracker, 60), [])

        # A claimed commit leaves the fetch: its entry is pruned...
        self.assertEqual(rcc.select_new_commits([c1], tracker, 60), [])
        self.assertEqual(set(tracker), {1})

        # ...so a commit released back to IN_QUEUE is re-enqueued at once.
        new = rcc.select_new_commits([c2], tracker, 60)
        self.assertEqual([c.id for c in new], [2])
        tracker[2] = time.monotonic()

        # A commit still IN_QUEUE past the window is re-enqueued (its worker
        # may have died between pulling and claiming it).
        tracker[1] = time.monotonic() - 61
        new = rcc.select_new_commits([c1, c2], tracker, 60)
        self.assertEqual([c.id for c in new], [1])
        self.assertNotIn(1, tracker)
        self.assertIn(2, tracker)


if __name__ == "__main__":
    _ = unittest.main()
