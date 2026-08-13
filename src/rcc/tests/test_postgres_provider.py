"""
Tests for the Postgres data provider (psycopg3 + shared async connection pool).

These tests do not require a database: the connection pool and connections
are faked at the boundary of `psycopg_pool.AsyncConnectionPool`, so they cover
pool lifecycle, transaction semantics (commit/rollback), pool exhaustion and
row mapping without external services.
"""

import base64
import datetime
import inspect
import unittest
from typing import Any, ClassVar, cast
from unittest import mock

import psycopg
import psycopg_pool

import rcc.config
from rcc.model import Commit, TestCase, TestCaseResult
from rcc.provider.data.postgres import Postgres


class FakeCursor:
    """Imitates ``psycopg.AsyncCursor``: execute + async iteration support."""

    def __init__(self, result_sets=None, error=None, rowcounts=None):
        # One list of rows per expected execute() call, in order.
        self.result_sets = list(result_sets or [])
        self.error = error
        # One rowcount value per expected execute() call, in order (default 0).
        self.rowcounts = list(rowcounts or [])
        self.rowcount = 0
        self.executed = []
        self._iter = iter([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params=None):
        if self.error is not None:
            raise self.error
        self.executed.append((query, params))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        rows = self.result_sets.pop(0) if self.result_sets else []
        self._iter = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeConnection:
    """Imitates ``psycopg.AsyncConnection`` transaction context semantics."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.autocommit = None

    def cursor(self):
        return self._cursor

    async def set_autocommit(self, value):
        # psycopg3 async connections expose autocommit through this awaitable
        # setter (the property itself is read-only).
        self.autocommit = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False


class FakePoolConnection:
    """Imitates the context manager returned by ``pool.connection()``."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        # psycopg_pool applies connection context semantics (commit on clean
        # exit, rollback on error) when returning the connection to the pool.
        await self._conn.__aexit__(exc_type, exc, tb)
        return False


class FakePool:
    """Stand-in for ``psycopg_pool.AsyncConnectionPool``."""

    def __init__(self, connection):
        self._connection = connection
        self.closed = False
        self.connections_granted = 0

    def connection(self):
        self.connections_granted += 1
        return FakePoolConnection(self._connection)

    async def close(self):
        self.closed = True


class ExhaustedPool:
    """A pool that can never provide a connection in time."""

    def connection(self):
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.0 sec")


class RecordingPool:
    """Records constructor arguments; used to inspect pool configuration."""

    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        RecordingPool.instances.append(self)

    async def open(self, wait=False, timeout=30.0):
        self.opened = True

    async def close(self, timeout=5.0):
        self.closed = True


def make_cfg(**db_overrides):
    db = {
        "host": "dbhost",
        "port": 5433,
        "name": "runcodes",
        "username": "user",
        "password": "pass",
    }
    db.update(db_overrides)
    return rcc.config.Config({"db": db})


def make_commit(**overrides):
    values = {
        "commit_id": 42,
        "user_email": "user@example.com",
        "exercise_id": 7,
        "real_exercise_id": 7,
        "status": Commit.STATUS_PROCESSING,
        "commit_hash": "abc123",
        "corrects": 2,
        "score": 10.0,
        "is_compiled": True,
        "compiled_message": "compiled ok",
        "commit_time": datetime.datetime(2026, 1, 2, 3, 4, 5),
        "compilation_started_time": datetime.datetime(2026, 1, 2, 3, 4, 6),
        "compilation_finished_time": datetime.datetime(2026, 1, 2, 3, 4, 7),
        "compiled_signal": 0,
        "compiled_error": "",
        "user_ip": "1.2.3.4",
        "aws_key": "commits/42/main.c",
        "offering_id": 1,
        "real_offering_id": 1,
        "course_id": 3,
        "fname": "main.c",
    }
    values.update(overrides)
    return Commit(**values)


def encode_b64(text):
    return base64.b64encode((text or "").encode("utf8")).decode("utf8")


class TestPostgresPool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RecordingPool.instances = []

    def tearDown(self):
        RecordingPool.instances = []

    async def test_open_creates_pool_with_expected_config(self):
        with mock.patch(
            "rcc.provider.data.postgres.AsyncConnectionPool", RecordingPool
        ):
            provider = Postgres(
                make_cfg(pool_min_size=2, pool_max_size=5, pool_timeout=10)
            )
            self.assertFalse(provider.is_open)
            await provider.open()

        self.assertTrue(provider.is_open)
        (pool,) = RecordingPool.instances
        self.assertTrue(pool.opened)
        self.assertEqual(pool.kwargs["min_size"], 2)
        self.assertEqual(pool.kwargs["max_size"], 5)
        self.assertEqual(pool.kwargs["timeout"], 10.0)
        self.assertFalse(pool.kwargs["open"])
        self.assertEqual(pool.kwargs["configure"], Postgres._configure_connection)
        conninfo = pool.kwargs["conninfo"]
        self.assertIn("host=dbhost", conninfo)
        self.assertIn("port=5433", conninfo)
        self.assertIn("dbname=runcodes", conninfo)
        self.assertIn("user=user", conninfo)

    async def test_open_is_idempotent(self):
        with mock.patch(
            "rcc.provider.data.postgres.AsyncConnectionPool", RecordingPool
        ):
            provider = Postgres(make_cfg())
            await provider.open()
            await provider.open()
        self.assertEqual(len(RecordingPool.instances), 1)

    async def test_close_closes_pool_and_allows_reopen(self):
        with mock.patch(
            "rcc.provider.data.postgres.AsyncConnectionPool", RecordingPool
        ):
            provider = Postgres(make_cfg())
            await provider.open()
            await provider.close()
            self.assertFalse(provider.is_open)
            (pool,) = RecordingPool.instances
            self.assertTrue(pool.closed)

            await provider.open()
            self.assertTrue(provider.is_open)

    async def test_use_before_open_raises(self):
        provider = Postgres(make_cfg())
        with self.assertRaises(RuntimeError):
            await provider.fetch_commits_in_queue()

    async def test_configure_callback_disables_autocommit(self):
        conn: Any = FakeConnection(FakeCursor())
        await Postgres._configure_connection(conn)
        # Must go through the async setter, not the read-only property.
        self.assertFalse(conn.autocommit)

    async def test_async_autocommit_api_contract(self):
        # Guard against assigning to `conn.autocommit` directly: on psycopg3
        # async connections the property setter raises AttributeError, which
        # would kill every pooled connection at the configure step and
        # produce the PoolTimeout storms seen in production logs. The
        # configure callback must use the awaitable `set_autocommit()`.
        self.assertTrue(
            inspect.iscoroutinefunction(psycopg.AsyncConnection.set_autocommit)
        )
        # Exercise the guard on a real AsyncConnection without touching the
        # network (__new__ skips __init__, so no server interaction).
        conn = psycopg.AsyncConnection.__new__(psycopg.AsyncConnection)
        with self.assertRaises(AttributeError):
            conn.autocommit = False

    async def test_pickling_drops_pool(self):
        import pickle

        provider = Postgres(make_cfg())
        provider._pool = cast(Any, object())
        clone = pickle.loads(pickle.dumps(provider))
        self.assertIsNone(clone._pool)
        # The configuration itself survives pickling.
        self.assertEqual(clone._pool_min_size, provider._pool_min_size)


class TestPostgresQueries(unittest.IsolatedAsyncioTestCase):
    def _provider_with(self, connection):
        provider = Postgres(make_cfg())
        provider._pool = cast(Any, FakePool(connection))
        return provider, connection

    async def test_update_commit_commits_on_success(self):
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        commit = make_commit()

        await provider.update_commit(commit)

        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        ((query, params),) = cursor.executed
        self.assertIn("UPDATE commits SET", query)
        expected = (
            commit.user_email,
            commit.exercise_id,
            commit.status,
            commit.commit_hash,
            commit.corrects,
            commit.score,
            commit.is_compiled,
            encode_b64(commit.compiled_message),
            commit.commit_time,
            commit.compilation_started_time,
            commit.compilation_finished_time,
            commit.compiled_signal,
            encode_b64(commit.compiled_error),
            commit.id,
        )
        self.assertEqual(params, expected)

    async def test_update_commit_rolls_back_on_error(self):
        cursor = FakeCursor(error=psycopg.OperationalError("syntax error"))
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)

        with self.assertRaises(psycopg.Error):
            await provider.update_commit(make_commit())

        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    async def test_store_commit_test_results_uses_single_transaction(self):
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        commit = make_commit()
        results = [
            TestCaseResult(commit.id, 1, 0.5, TestCaseResult.STATUS_CORRECT, "ok"),
            TestCaseResult(commit.id, 2, 0.7, TestCaseResult.STATUS_INCORRECT, "bad"),
        ]

        await provider.store_commit_test_results(commit, results)

        self.assertEqual(len(cursor.executed), 2)
        self.assertTrue(
            all(
                q.startswith("INSERT INTO commits_exercise_cases")
                for q, _ in cursor.executed
            )
        )
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

    async def test_store_commit_test_results_rolls_back_if_an_insert_fails(self):
        cursor = FakeCursor(error=psycopg.OperationalError("constraint violation"))
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        results = [TestCaseResult(42, 1, 0.5, TestCaseResult.STATUS_CORRECT, "ok")]

        with self.assertRaises(psycopg.Error):
            await provider.store_commit_test_results(make_commit(), results)

        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    async def test_pool_exhaustion_propagates_and_is_not_swallowed(self):
        provider = Postgres(make_cfg())
        provider._pool = cast(Any, ExhaustedPool())

        with self.assertRaises(psycopg_pool.PoolTimeout):
            await provider.update_commit(make_commit())

    async def test_fetch_commits_in_queue_maps_rows_and_language(self):
        row = [
            1,  # id
            "user@example.com",  # user_email
            5,  # exercise_id
            Commit.STATUS_IN_QUEUE,  # status
            "hash",  # hash
            0,  # corrects
            0.0,  # score
            False,  # compiled
            "",  # compiled_message
            datetime.datetime(2026, 1, 2),  # commit_time
            None,  # compilation_started
            None,  # compilation_finished
            None,  # compiled_signal
            "",  # compiled_error
            "1.2.3.4",  # ip
            "commits/1/main.c",  # aws_key
            2,  # offering_id
            False,  # ghost
            5,  # real_id (real_exercise_id)
            3,  # course_id
            "commits/1/main.c",  # real_offering_id (unused index filler)
        ]
        cursor = FakeCursor(result_sets=[[row]])
        provider, _ = self._provider_with(FakeConnection(cursor))

        commits = await provider.fetch_commits_in_queue()

        ((_query, params),) = cursor.executed
        self.assertEqual(params, (Commit.STATUS_IN_QUEUE,))
        self.assertEqual(len(commits), 1)
        commit = commits[0]
        self.assertEqual(commit.id, 1)
        self.assertEqual(commit.real_exercise_id, 5)
        self.assertEqual(commit.fname, "main.c")
        self.assertIsNotNone(commit.language)
        self.assertEqual(commit.language.name, "C")

    async def test_fetch_test_cases_reads_cases_and_files(self):
        case_row = [
            101,  # id
            5,  # exercise_id
            TestCase.IO_TYPE_TEXT,  # input_type
            TestCase.IO_TYPE_NUMERIC,  # output_type
            False,  # show_input
            False,  # show_expected_output
            0,  # maxmemsize
            5,  # cputime
            0,  # stacksize
            False,  # show_user_output
            0,  # file_size
            0.5,  # abs_error
            datetime.datetime(2026, 1, 2),  # last_update
        ]
        cursor = FakeCursor(
            result_sets=[
                [case_row],
                [(101, "in.txt"), (101, "data.bin")],
            ]
        )
        provider, _ = self._provider_with(FakeConnection(cursor))
        commit = make_commit()

        test_cases = await provider.fetch_test_cases(commit)

        self.assertEqual(len(test_cases), 1)
        test_case = test_cases[0]
        self.assertEqual(test_case.id, 101)
        self.assertEqual(test_case.output_type, TestCase.IO_TYPE_NUMERIC)
        self.assertEqual(test_case.files, ["in.txt", "data.bin"])
        self.assertEqual(len(cursor.executed), 2)
        self.assertEqual(cursor.executed[0][1], (commit.real_exercise_id,))
        # The per-case N+1 loop is replaced by one batched query over the ids.
        self.assertEqual(cursor.executed[1][1], ([101],))

    async def test_fetch_test_cases_batches_files_across_cases(self):
        def case_row(case_id):
            return [
                case_id,
                5,
                TestCase.IO_TYPE_TEXT,
                TestCase.IO_TYPE_TEXT,
                False,
                False,
                0,
                5,
                0,
                False,
                0,
                0.0,
                None,
            ]

        cursor = FakeCursor(
            result_sets=[
                [case_row(101), case_row(102)],
                # Interleaved rows: attribution must preserve per-case order.
                [(101, "a.in"), (102, "b.in"), (101, "data.bin")],
            ]
        )
        provider, _ = self._provider_with(FakeConnection(cursor))

        test_cases = await provider.fetch_test_cases(make_commit())

        self.assertEqual([tc.id for tc in test_cases], [101, 102])
        self.assertEqual(test_cases[0].files, ["a.in", "data.bin"])
        self.assertEqual(test_cases[1].files, ["b.in"])
        self.assertEqual(len(cursor.executed), 2)
        self.assertEqual(cursor.executed[1][1], ([101, 102],))

    async def test_fetch_test_cases_skips_files_query_without_cases(self):
        cursor = FakeCursor(result_sets=[[]])
        provider, _ = self._provider_with(FakeConnection(cursor))

        test_cases = await provider.fetch_test_cases(make_commit())

        self.assertEqual(test_cases, [])
        self.assertEqual(len(cursor.executed), 1)

    async def test_fetch_exercise_files_reads_rows(self):
        cursor = FakeCursor(result_sets=[[("Makefile",), ("util.c",)]])
        provider, _ = self._provider_with(FakeConnection(cursor))

        fnames = await provider.fetch_exercise_files(make_commit())

        self.assertEqual(fnames, ["Makefile", "util.c"])
        self.assertEqual(cursor.executed[0][1], (7,))

    async def test_claim_commit_flips_in_queue_to_processing(self):
        cursor = FakeCursor(rowcounts=[1])
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        commit = make_commit()

        claimed = await provider.claim_commit(commit)

        self.assertTrue(claimed)
        self.assertTrue(conn.committed)
        ((query, params),) = cursor.executed
        self.assertIn("UPDATE commits", query)
        self.assertIn("status = %s", query)
        self.assertIn("WHERE id = %s AND status = %s", query)
        # The DB column is `compilation_started` (no `_time` suffix): a wrong
        # column name would fail every claim against the real schema.
        self.assertIn("compilation_started = %s", query)
        self.assertNotIn("compilation_started_time", query)
        status, started, commit_id, expected_status = params
        self.assertEqual(status, Commit.STATUS_PROCESSING)
        self.assertIsInstance(started, datetime.datetime)
        self.assertEqual(commit_id, commit.id)
        self.assertEqual(expected_status, Commit.STATUS_IN_QUEUE)

    async def test_claim_commit_loses_when_already_taken(self):
        # Zero rows updated: the commit is no longer IN_QUEUE.
        cursor = FakeCursor(rowcounts=[0])
        provider, _ = self._provider_with(FakeConnection(cursor))

        claimed = await provider.claim_commit(make_commit())

        self.assertFalse(claimed)
        ((_query, params),) = cursor.executed
        self.assertEqual(params[0], Commit.STATUS_PROCESSING)
        self.assertEqual(params[2], 42)
        self.assertEqual(params[3], Commit.STATUS_IN_QUEUE)

    async def test_release_commit_restores_in_queue_state(self):
        cursor = FakeCursor(rowcounts=[1])
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        commit = make_commit()

        await provider.release_commit(commit)

        self.assertTrue(conn.committed)
        ((query, params),) = cursor.executed
        self.assertIn("UPDATE commits", query)
        self.assertIn("compilation_started = NULL", query)
        self.assertNotIn("compilation_started_time", query)
        self.assertEqual(
            params, (Commit.STATUS_IN_QUEUE, commit.id, Commit.STATUS_PROCESSING)
        )
        self.assertEqual(
            params, (Commit.STATUS_IN_QUEUE, commit.id, Commit.STATUS_PROCESSING)
        )


if __name__ == "__main__":
    unittest.main()
