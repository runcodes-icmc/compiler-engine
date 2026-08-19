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
from collections.abc import Iterator
from typing import ClassVar, Self, cast, override
from unittest import mock

import psycopg
import psycopg_pool

import rcc.config
from rcc.languages import Language
from rcc.model import Commit, TestCase, TestCaseResult
from rcc.provider.data.postgres import Postgres


class FakeCursor:
    """Imitates ``psycopg.AsyncCursor``: execute + async iteration support."""

    result_sets: list[list[object]]
    error: Exception | None
    rowcounts: list[int]
    rowcount: int
    executed: list[tuple[str, object | None]]
    _iter: Iterator[object]

    def __init__(
        self,
        result_sets: list[list[object]] | None = None,
        error: Exception | None = None,
        rowcounts: list[int] | None = None,
    ) -> None:
        # One list of rows per expected execute() call, in order.
        self.result_sets = list(result_sets or [])
        self.error = error
        # One rowcount value per expected execute() call, in order (default 0).
        self.rowcounts = list(rowcounts or [])
        self.rowcount = 0
        self.executed = []
        self._iter = iter(())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return False

    async def execute(self, query: str, params: object | None = None) -> None:
        if self.error is not None:
            raise self.error
        self.executed.append((query, params))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        rows = self.result_sets.pop(0) if self.result_sets else []
        self._iter = iter(rows)

    def __aiter__(self) -> FakeCursor:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeConnection:
    """Imitates ``psycopg.AsyncConnection`` transaction context semantics."""

    _cursor: FakeCursor
    committed: bool
    rolled_back: bool
    autocommit: bool | None

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.autocommit = None

    def cursor(self) -> FakeCursor:
        return self._cursor

    async def set_autocommit(self, value: bool) -> None:
        # psycopg3 async connections expose autocommit through this awaitable
        # setter (the property itself is read-only).
        self.autocommit = value

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False


class FakePoolConnection:
    """Imitates the context manager returned by ``pool.connection()``."""

    _conn: FakeConnection

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        # psycopg_pool applies connection context semantics (commit on clean
        # exit, rollback on error) when returning the connection to the pool.
        _ = await self._conn.__aexit__(exc_type, exc, tb)
        return False


class FakePool:
    """Stand-in for ``psycopg_pool.AsyncConnectionPool``."""

    _connection: FakeConnection
    closed: bool
    connections_granted: int

    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.closed = False
        self.connections_granted = 0

    def connection(self) -> FakePoolConnection:
        self.connections_granted += 1
        return FakePoolConnection(self._connection)

    async def close(self) -> None:
        self.closed = True


class ExhaustedPool:
    """A pool that can never provide a connection in time."""

    def connection(self) -> FakePoolConnection:
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.0 sec")


class RecordingPool:
    """Records constructor arguments; used to inspect pool configuration."""

    instances: ClassVar[list[RecordingPool]] = []
    kwargs: dict[str, object]
    opened: bool
    closed: bool

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        RecordingPool.instances.append(self)

    async def open(self, **_kwargs: object) -> None:
        self.opened = True

    async def close(self, **_kwargs: object) -> None:
        self.closed = True


def make_cfg(
    concurrency: int | None = None, **db_overrides: object
) -> rcc.config.Config:
    db: dict[str, object] = {
        "host": "dbhost",
        "port": 5433,
        "name": "runcodes",
        "username": "user",
        "password": "pass",
    }
    db.update(db_overrides)
    cfg: dict[str, object] = {"db": db}
    if concurrency is not None:
        cfg["concurrency_per_worker"] = concurrency
    return rcc.config.Config(cfg)


def make_commit(**overrides: object) -> Commit:
    values: dict[str, object] = {
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
        "commit_time": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "compilation_started_time": datetime.datetime(
            2026, 1, 2, 3, 4, 6, tzinfo=datetime.UTC
        ),
        "compilation_finished_time": datetime.datetime(
            2026, 1, 2, 3, 4, 7, tzinfo=datetime.UTC
        ),
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
    return Commit(
        cast(int, values["commit_id"]),
        cast(str, values["user_email"]),
        cast(int, values["exercise_id"]),
        cast(int, values["real_exercise_id"]),
        cast(int, values["status"]),
        cast(str, values["commit_hash"]),
        cast(int, values["corrects"]),
        cast(float, values["score"]),
        cast(bool, values["is_compiled"]),
        cast(str, values["compiled_message"]),
        cast(datetime.datetime, values["commit_time"]),
        cast(datetime.datetime | None, values["compilation_started_time"]),
        cast(datetime.datetime | None, values["compilation_finished_time"]),
        cast(str | int | None, values["compiled_signal"]),
        cast(str, values["compiled_error"]),
        cast(str | None, values["user_ip"]),
        cast(str, values["aws_key"]),
        cast(int, values["offering_id"]),
        cast(int, values["real_offering_id"]),
        cast(int, values["course_id"]),
        cast(str | None, values["fname"]),
    )


def encode_b64(text: str | None) -> str:
    return base64.b64encode((text or "").encode("utf8")).decode("utf8")


class TestPostgresPool(unittest.IsolatedAsyncioTestCase):
    @override
    def setUp(self) -> None:
        RecordingPool.instances = []

    @override
    def tearDown(self) -> None:
        RecordingPool.instances = []

    async def test_open_creates_pool_with_expected_config(self) -> None:
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
        self.assertEqual(pool.kwargs["configure"], Postgres.configure_connection)
        conninfo = cast(str, pool.kwargs["conninfo"])
        self.assertIn("host=dbhost", conninfo)
        self.assertIn("port=5433", conninfo)
        self.assertIn("dbname=runcodes", conninfo)
        self.assertIn("user=user", conninfo)

    def test_pool_max_size_derived_from_concurrency(self) -> None:
        provider = Postgres(make_cfg(concurrency=4))
        self.assertEqual(provider.pool_max_size, 6)

    def test_pool_max_size_derived_value_clamped_to_min_size(self) -> None:
        provider = Postgres(make_cfg(concurrency=1, pool_min_size=5))
        self.assertEqual(provider.pool_max_size, 5)

    def test_pool_max_size_explicit_value_wins(self) -> None:
        provider = Postgres(make_cfg(concurrency=4, pool_max_size=100))
        self.assertEqual(provider.pool_max_size, 100)

    def test_pool_max_size_derivation_uses_default_concurrency(self) -> None:
        provider = Postgres(make_cfg())
        self.assertEqual(
            provider.pool_max_size,
            rcc.config.DEFAULT_CONCURRENCY_PER_WORKER + 2,
        )

    async def test_open_uses_derived_max_size_when_not_configured(self) -> None:
        with mock.patch(
            "rcc.provider.data.postgres.AsyncConnectionPool", RecordingPool
        ):
            provider = Postgres(make_cfg(concurrency=3))
            await provider.open()
        (pool,) = RecordingPool.instances
        self.assertEqual(pool.kwargs["max_size"], 5)

    async def test_open_is_idempotent(self) -> None:
        with mock.patch(
            "rcc.provider.data.postgres.AsyncConnectionPool", RecordingPool
        ):
            provider = Postgres(make_cfg())
            await provider.open()
            await provider.open()
        self.assertEqual(len(RecordingPool.instances), 1)

    async def test_close_closes_pool_and_allows_reopen(self) -> None:
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

    async def test_use_before_open_raises(self) -> None:
        provider = Postgres(make_cfg())
        with self.assertRaises(RuntimeError):
            _ = await provider.fetch_commits_in_queue()

    async def test_configure_callback_disables_autocommit(self) -> None:
        conn = FakeConnection(FakeCursor())
        await Postgres.configure_connection(
            cast(psycopg.AsyncConnection, cast(object, conn))
        )
        # Must go through the async setter, not the read-only property.
        self.assertFalse(conn.autocommit)

    async def test_async_autocommit_api_contract(self) -> None:
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

    async def test_pickling_drops_pool(self) -> None:
        import pickle

        provider = Postgres(make_cfg())
        provider._pool = cast(  # pyright: ignore[reportPrivateUsage]
            psycopg_pool.AsyncConnectionPool[psycopg.AsyncConnection], object()
        )
        clone = cast(Postgres, pickle.loads(pickle.dumps(provider)))
        self.assertIsNone(clone._pool)  # pyright: ignore[reportPrivateUsage]
        # The configuration itself survives pickling.
        self.assertEqual(clone.pool_min_size, provider.pool_min_size)


class TestPostgresQueries(unittest.IsolatedAsyncioTestCase):
    def _provider_with(
        self, connection: FakeConnection
    ) -> tuple[Postgres, FakeConnection]:
        provider = Postgres(make_cfg())
        provider._pool = cast(  # pyright: ignore[reportPrivateUsage]
            psycopg_pool.AsyncConnectionPool[psycopg.AsyncConnection],
            cast(object, FakePool(connection)),
        )
        return provider, connection

    async def test_update_commit_commits_on_success(self) -> None:
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

    async def test_update_commit_rolls_back_on_error(self) -> None:
        cursor = FakeCursor(error=psycopg.OperationalError("syntax error"))
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)

        with self.assertRaises(psycopg.Error):
            await provider.update_commit(make_commit())

        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    async def test_store_commit_test_results_uses_single_transaction(self) -> None:
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

    async def test_store_commit_test_results_rolls_back_if_an_insert_fails(
        self,
    ) -> None:
        cursor = FakeCursor(error=psycopg.OperationalError("constraint violation"))
        conn = FakeConnection(cursor)
        provider, _ = self._provider_with(conn)
        results = [TestCaseResult(42, 1, 0.5, TestCaseResult.STATUS_CORRECT, "ok")]

        with self.assertRaises(psycopg.Error):
            await provider.store_commit_test_results(make_commit(), results)

        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    async def test_pool_exhaustion_propagates_and_is_not_swallowed(self) -> None:
        provider = Postgres(make_cfg())
        provider._pool = cast(  # pyright: ignore[reportPrivateUsage]
            psycopg_pool.AsyncConnectionPool[psycopg.AsyncConnection],
            cast(object, ExhaustedPool()),
        )

        with self.assertRaises(psycopg_pool.PoolTimeout):
            await provider.update_commit(make_commit())

    async def test_fetch_commits_in_queue_maps_rows_and_language(self) -> None:
        row: list[object] = [
            1,  # id
            "user@example.com",  # user_email
            5,  # exercise_id
            Commit.STATUS_IN_QUEUE,  # status
            "hash",  # hash
            0,  # corrects
            0.0,  # score
            False,  # compiled
            "",  # compiled_message
            datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),  # commit_time
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
            "commits/1/main.c",  # real_offering_id
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
        self.assertEqual(cast(Language, commit.language).name, "C")

    async def test_fetch_test_cases_reads_cases_and_files(self) -> None:
        case_row: list[object] = [
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
            datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),  # last_update
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

    async def test_fetch_test_cases_batches_files_across_cases(self) -> None:
        def case_row(case_id: int) -> list[object]:
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

    async def test_fetch_test_cases_skips_files_query_without_cases(self) -> None:
        cursor = FakeCursor(result_sets=[[]])
        provider, _ = self._provider_with(FakeConnection(cursor))

        test_cases = await provider.fetch_test_cases(make_commit())

        self.assertEqual(test_cases, [])
        self.assertEqual(len(cursor.executed), 1)

    async def test_fetch_exercise_files_reads_rows(self) -> None:
        cursor = FakeCursor(result_sets=[[("Makefile",), ("util.c",)]])
        provider, _ = self._provider_with(FakeConnection(cursor))

        fnames = await provider.fetch_exercise_files(make_commit())

        self.assertEqual(fnames, ["Makefile", "util.c"])
        self.assertEqual(cursor.executed[0][1], (7,))

    async def test_claim_commit_flips_in_queue_to_processing(self) -> None:
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
        status, started, commit_id, expected_status = cast(tuple[object, ...], params)
        self.assertEqual(status, Commit.STATUS_PROCESSING)
        self.assertIsInstance(started, datetime.datetime)
        self.assertEqual(commit_id, commit.id)
        self.assertEqual(expected_status, Commit.STATUS_IN_QUEUE)

    async def test_claim_commit_loses_when_already_taken(self) -> None:
        # Zero rows updated: the commit is no longer IN_QUEUE.
        cursor = FakeCursor(rowcounts=[0])
        provider, _ = self._provider_with(FakeConnection(cursor))

        claimed = await provider.claim_commit(make_commit())

        self.assertFalse(claimed)
        ((_query, params),) = cursor.executed
        params_tuple = cast(tuple[object, ...], params)
        self.assertEqual(params_tuple[0], Commit.STATUS_PROCESSING)
        self.assertEqual(params_tuple[2], 42)
        self.assertEqual(params_tuple[3], Commit.STATUS_IN_QUEUE)

    async def test_release_commit_restores_in_queue_state(self) -> None:
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
