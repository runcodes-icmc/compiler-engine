"""
Unit tests for the parallel commit pipeline (no docker daemon required).

Covers:
- the container log reader thread adapter (ordering, sentinel, timeouts),
- the per-worker semaphore concurrency (M slots, no slot leaks, stop hints),
- the bounded task queue backpressure in ``rcc.main()``.
"""

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
from unittest import mock

import rcc
import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
import rcc.util
from rcc.model import Commit


def make_commit(commit_id):
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


def make_cfg(concurrency, exec_dir=None):
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


class FakeStorage(object):
    """Synchronous no-op storage provider (its methods run in worker threads)."""

    def __init__(self, cfg):
        pass

    def fetch_commit_file(self, commit, destination):
        pass

    def fetch_exercise_file(self, source, destination):
        pass

    def fetch_test_case_input_file(self, test_case, destination):
        pass

    def fetch_test_case_output_file(self, test_case, destination):
        pass

    def fetch_test_case_files(self, test_case, destination):
        pass

    def store_commit_output(self, commit, commit_output_fname):
        pass


class TrackingProvider(rcc.provider.data.DataProvider):
    """Counts pool lifecycle events and records commit statuses; no real DB."""

    def __init__(self):
        self.open_count = 0
        self.close_count = 0
        self.commit_statuses = {}

    async def open(self):
        self.open_count += 1

    async def close(self):
        self.close_count += 1

    async def fetch_commits_in_queue(self):
        return []

    async def update_commit(self, commit):
        # The last status written for a commit is its final status.
        self.commit_statuses[commit.id] = commit.status

    async def store_commit_test_results(self, commit, test_results):
        pass

    async def delete_commit_test_results(self, commit):
        pass

    async def fetch_exercise_files(self, commit):
        return []

    async def fetch_test_cases(self, commit):
        return []


class ClaimingProvider(TrackingProvider):
    """Mimics the Postgres claim semantics: first claim per commit wins."""

    def __init__(self):
        super().__init__()
        self._claimed = set()
        self.claim_count = 0
        self.release_count = 0

    async def claim_commit(self, commit):
        self.claim_count += 1
        if commit.id in self._claimed:
            return False
        self._claimed.add(commit.id)
        return True

    async def release_commit(self, commit):
        self.release_count += 1
        self._claimed.discard(commit.id)


def unfinished_tasks(q):
    """Number of items put on a JoinableQueue but not yet task_done()'d.

    ``multiprocessing.queues.JoinableQueue`` keeps the counter in a private
    ``Semaphore`` and does not expose it as an attribute (unlike
    ``queue.Queue``).
    """
    return q._unfinished_tasks._semlock._get_value()


class TestContainerLogReader(unittest.IsolatedAsyncioTestCase):
    def _reader(self, generator):
        reader = rcc.engine.ContainerLogReader(generator, asyncio.get_running_loop())
        reader.start()
        return reader

    async def test_lines_are_delivered_in_order(self):
        def gen():
            yield b"compilation.start\n"
            yield b"  hello\n"
            yield b"compilation.done"

        reader = self._reader(gen())
        self.assertEqual(await reader.get(), "compilation.start")
        self.assertEqual(await reader.get(), "hello")
        self.assertEqual(await reader.get(), "compilation.done")
        self.assertIs(await reader.get(), rcc.engine.ContainerLogReader.END)
        reader.stop()

    async def test_sentinel_pushed_when_generator_is_empty(self):
        reader = self._reader(iter(()))
        self.assertIs(
            await asyncio.wait_for(reader.get(), 1.0), rcc.engine.ContainerLogReader.END
        )
        reader.stop()

    async def test_generator_error_is_treated_as_end_of_stream(self):
        def gen():
            yield b"first"
            raise ConnectionError("socket closed")

        reader = self._reader(gen())
        self.assertEqual(await reader.get(), "first")
        self.assertIs(
            await asyncio.wait_for(reader.get(), 1.0), rcc.engine.ContainerLogReader.END
        )
        reader.stop()

    async def test_get_times_out_when_generator_stalls(self):
        def gen():
            time.sleep(30)
            yield b"late"

        reader = self._reader(gen())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.get(), 0.05)

    async def test_expect_message_matches(self):
        reader = self._reader(iter([b"compilation.start"]))
        await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()

    async def test_expect_message_wrong_line_raises(self):
        reader = self._reader(iter([b"something.else"]))
        with self.assertRaises(RuntimeError):
            await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()

    async def test_expect_message_timeout_raises(self):
        def gen():
            time.sleep(30)
            yield b"late"

        reader = self._reader(gen())
        with self.assertRaises(asyncio.TimeoutError):
            await rcc.engine.expect_message(reader, "compilation.start", 0.05)

    async def test_expect_message_stream_end_raises(self):
        reader = self._reader(iter(()))
        with self.assertRaises(RuntimeError):
            await rcc.engine.expect_message(reader, "compilation.start", 1.0)
        reader.stop()


class TestWorkerConcurrency(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, commits, concurrency, fake_process_commit):
        provider = TrackingProvider()
        cfg = make_cfg(concurrency)
        task_queue = mp.JoinableQueue()
        for commit in commits:
            task_queue.put(commit)
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake_process_commit):
            await rcc.engine.process_commits(provider, task_queue, cfg)
        return provider, task_queue

    async def test_at_most_M_commits_in_flight(self):
        state = {"active": 0, "max_active": 0, "finished": []}

        async def fake(data_provider, commit, cfg=None):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                await asyncio.sleep(0.05)
            finally:
                state["active"] -= 1
            state["finished"].append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(i) for i in range(5)], 2, fake
        )
        self.assertEqual(state["max_active"], 2)
        self.assertEqual(sorted(state["finished"]), [0, 1, 2, 3, 4])
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_stop_hint_drains_in_flight_commits(self):
        state = {"finished": []}

        async def fake(data_provider, commit, cfg=None):
            await asyncio.sleep(0.1)
            state["finished"].append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(1), make_commit(2), make_commit(3)], 2, fake
        )
        self.assertEqual(sorted(state["finished"]), [1, 2, 3])
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_retryable_failure_does_not_leak_a_slot(self):
        state = {"active": 0, "max_active": 0, "finished": [], "failed": False}

        async def fake(data_provider, commit, cfg=None):
            if commit.id == 1:
                state["failed"] = True
                raise RuntimeError("retryable failure")
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                await asyncio.sleep(0.1)
            finally:
                state["active"] -= 1
            state["finished"].append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(i) for i in (1, 2, 3, 4, 5)], 2, fake
        )
        self.assertTrue(state["failed"])
        # The failing commit's slot was released: the remaining commits still
        # run two at a time (with a leaked slot they would be serialized).
        self.assertEqual(sorted(state["finished"]), [2, 3, 4, 5])
        self.assertEqual(state["max_active"], 2)
        self.assertEqual(unfinished_tasks(task_queue), 0)

    async def test_non_retryable_failure_stops_the_worker(self):
        state = {"finished": [], "fatal": False}

        async def fake(data_provider, commit, cfg=None):
            if commit.id == 1:
                state["fatal"] = True
                raise MemoryError("boom")
            await asyncio.sleep(0.05)
            state["finished"].append(commit.id)

        provider, task_queue = await self._drive(
            [make_commit(1), make_commit(2), make_commit(3)], 2, fake
        )
        self.assertTrue(state["fatal"])
        # The failing commit never completes; the commits already pulled and
        # spawned before the failure was observed (commit 2, and — depending
        # on scheduling — commit 3) are drained before the worker exits.
        self.assertNotIn(1, state["finished"])
        self.assertIn(2, state["finished"])
        self.assertTrue(all(cid in (2, 3) for cid in state["finished"]))
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)

    async def test_duplicate_commits_are_processed_once(self):
        """Copies of the same commit (the poller re-enqueues IN_QUEUE
        commits) are claimed by exactly one worker."""
        state = {"processed": []}

        async def fake(data_provider, commit, cfg=None):
            state["processed"].append(commit.id)

        provider = ClaimingProvider()
        cfg = make_cfg(2)
        task_queue = mp.JoinableQueue()
        for commit in (make_commit(1), make_commit(1), make_commit(2)):
            task_queue.put(commit)
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake):
            await rcc.engine.process_commits(provider, task_queue, cfg)

        # The duplicate copy of commit 1 was claimed and skipped.
        self.assertEqual(sorted(state["processed"]), [1, 2])
        self.assertEqual(provider.claim_count, 3)
        self.assertEqual(provider._claimed, {1, 2})
        self.assertEqual(unfinished_tasks(task_queue), 0)

    async def test_retryable_failure_releases_the_claim(self):
        """A claimed commit that fails retryably goes back to IN_QUEUE."""

        async def fake(data_provider, commit, cfg=None):
            raise RuntimeError("retryable failure")

        provider = ClaimingProvider()
        cfg = make_cfg(2)
        task_queue = mp.JoinableQueue()
        task_queue.put(make_commit(1))
        task_queue.put(None)
        with mock.patch.object(rcc.engine, "process_commit", fake):
            await rcc.engine.process_commits(provider, task_queue, cfg)

        self.assertEqual(provider.claim_count, 1)
        self.assertEqual(provider.release_count, 1)
        # The claim was given back: a later pull may take the commit again.
        self.assertEqual(provider._claimed, set())
        self.assertEqual(unfinished_tasks(task_queue), 0)


class TestProcessCommitIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_commits_processed_concurrently(self):
        """Run the real process_commit() pipeline with fakes (no docker)."""
        state = {"active": 0, "max_active": 0}

        async def fake_run_tests(
            data_provider, storage_provider, commit, test_cases, base_dir, remote_dir
        ):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                await asyncio.sleep(0.1)
            finally:
                state["active"] -= 1
            return []

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_cfg(2, exec_dir=tmpdir)
            provider = TrackingProvider()
            storage = FakeStorage(cfg)
            task_queue = mp.JoinableQueue()
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

        self.assertEqual(state["max_active"], 2)
        # The multiprocessing queue pickles items, so the worker mutated
        # copies; observe the final statuses through the provider instead.
        for commit in commits:
            self.assertEqual(
                provider.commit_statuses[commit.id], Commit.STATUS_COMPLETED
            )
        self.assertEqual(unfinished_tasks(task_queue), 0)
        self.assertEqual(provider.open_count, 1)
        self.assertEqual(provider.close_count, 1)


class RecordingJoinableQueue(mp_queues.JoinableQueue):
    """Records the maxsize used to construct the queue.

    ``multiprocessing.JoinableQueue`` is a factory function, so the concrete
    class from ``multiprocessing.queues`` is subclassed instead.
    """

    instances = []

    def __init__(self, maxsize=0):
        super().__init__(maxsize, ctx=mp.get_context())
        RecordingJoinableQueue.instances.append(self)


class RecordingPutQueue(mp_queues.JoinableQueue):
    """JoinableQueue that records the ids of everything put into it."""

    instances = []

    def __init__(self, maxsize=0):
        super().__init__(maxsize, ctx=mp.get_context())
        self.put_ids = []
        RecordingPutQueue.instances.append(self)

    def put(self, item, *args, **kwargs):
        if item is not None:
            self.put_ids.append(item.id)
        super().put(item, *args, **kwargs)


class FakeProcess(object):
    """Runs the mp.Process target in a daemon thread (no real fork/spawn)."""

    def __init__(self, target, args=()):
        self._thread = threading.Thread(target=target, args=args, daemon=True)

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self):
        self._thread.join()

    def terminate(self):
        pass


class _NullSingletonContext(object):
    def __init__(self, lock_fname, remove_lock_at_exit=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        return False


WORKER_COMMIT_SECONDS = 0.3


def fake_worker(data_provider, task_queue, cfg):
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
    def __init__(self, commits):
        self._commits = commits
        self.fetch_count = 0
        self.open_count = 0
        self.close_count = 0

    async def open(self):
        self.open_count += 1

    async def close(self):
        self.close_count += 1

    async def fetch_commits_in_queue(self):
        self.fetch_count += 1
        if self.fetch_count == 1:
            return list(self._commits)
        return []

    async def update_commit(self, commit):
        pass

    async def store_commit_test_results(self, commit, test_results):
        pass

    async def delete_commit_test_results(self, commit):
        pass

    async def fetch_exercise_files(self, commit):
        return []

    async def fetch_test_cases(self, commit):
        return []


class RepeatingProvider(rcc.provider.data.DataProvider):
    """Returns the same commit on every poll cycle; counts the cycles."""

    def __init__(self, commit):
        self._commit = commit
        self.fetch_count = 0

    async def open(self):
        pass

    async def close(self):
        pass

    async def fetch_commits_in_queue(self):
        self.fetch_count += 1
        return [self._commit]


class TestMainBackpressure(unittest.IsolatedAsyncioTestCase):
    def test_queue_maxsize_is_two_times_total_slots(self):
        cfg = rcc.config.Config({"num_workers": 3, "concurrency_per_worker": 4})
        self.assertEqual(rcc._task_queue_maxsize(cfg), 24)

    def test_queue_maxsize_falls_back_to_default_concurrency(self):
        cfg = rcc.config.Config({"num_workers": 2})
        expected = 2 * 2 * rcc.config.DEFAULT_CONCURRENCY_PER_WORKER
        self.assertEqual(rcc._task_queue_maxsize(cfg), expected)

    async def test_polling_loop_blocks_on_a_full_queue(self):
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
            patch.start()
        try:
            main_task = asyncio.create_task(rcc.main())
            await asyncio.sleep(0.5)

            # The bounded queue (maxsize 2) filled after two instant puts; the
            # third put blocks until the slow fake worker consumes one, so the
            # polling loop must not have fetched a second batch yet.
            self.assertEqual(provider.fetch_count, 1)
            (task_queue,) = RecordingJoinableQueue.instances
            self.assertEqual(task_queue._maxsize, 2)

            # Simulate Ctrl-C (the first Ctrl-C cancels main()).
            main_task.cancel()
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

    async def _run_main_with_repeating_commit(self, provider, cfg, runtime):
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
            patch.start()
        try:
            main_task = asyncio.create_task(rcc.main())
            await asyncio.sleep(runtime)
            main_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main_task
        finally:
            for patch in reversed(patches):
                patch.stop()
        (task_queue,) = RecordingPutQueue.instances
        return task_queue

    async def test_poller_suppresses_recent_commits(self):
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

    async def test_poller_reenqueues_after_suppression_expires(self):
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

    def test_recent_commits_are_suppressed_and_tracking_is_pruned(self):
        tracker = {}
        c1, c2 = make_commit(1), make_commit(2)

        # First appearance: both are new.
        new = rcc._select_new_commits([c1, c2], tracker, 60)
        self.assertEqual([c.id for c in new], [1, 2])
        tracker[1] = time.monotonic()
        tracker[2] = time.monotonic()

        # Still IN_QUEUE within the window: suppressed.
        self.assertEqual(rcc._select_new_commits([c1, c2], tracker, 60), [])

        # A claimed commit leaves the fetch: its entry is pruned...
        self.assertEqual(rcc._select_new_commits([c1], tracker, 60), [])
        self.assertEqual(set(tracker), {1})

        # ...so a commit released back to IN_QUEUE is re-enqueued at once.
        new = rcc._select_new_commits([c2], tracker, 60)
        self.assertEqual([c.id for c in new], [2])
        tracker[2] = time.monotonic()

        # A commit still IN_QUEUE past the window is re-enqueued (its worker
        # may have died between pulling and claiming it).
        tracker[1] = time.monotonic() - 61
        new = rcc._select_new_commits([c1, c2], tracker, 60)
        self.assertEqual([c.id for c in new], [1])
        self.assertNotIn(1, tracker)
        self.assertIn(2, tracker)


if __name__ == "__main__":
    unittest.main()
