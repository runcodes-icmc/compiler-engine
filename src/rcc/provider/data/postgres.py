from __future__ import unicode_literals

import base64
import datetime
from typing import cast, override

import psycopg
import psycopg.conninfo
from psycopg_pool import AsyncConnectionPool

from ...config import Config
from ...languages import language_from_extension
from ...model import Commit, TestCase, TestCaseResult
from .data_provider import DataProvider


class Postgres(DataProvider):
    """PostgreSQL data provider backed by a shared async connection pool.

    Instead of opening a fresh connection per call (the old psycopg2
    pattern), every method now draws a connection from a single
    `psycopg_pool.AsyncConnectionPool`, created lazily by :meth:`open` and
    released by :meth:`close`.

    One pool is created *per process*: one in the main polling process and
    one in every worker process. Pools hold live sockets and background
    tasks and cannot be shared across processes, so the pool is opened after
    the multiprocessing workers have been spawned and is deliberately
    stripped when this object is pickled (see :meth:`__getstate__`).
    """

    _conninfo: str
    _pool_min_size: int
    _pool_max_size: int
    _pool_timeout: float
    _pool: AsyncConnectionPool[psycopg.AsyncConnection] | None

    def __init__(self, cfg: Config) -> None:
        db = cast(dict[str, object], cfg.db)
        self._conninfo = psycopg.conninfo.make_conninfo(
            host=cast(str, db["host"]),
            port=cast(int, db["port"]),
            dbname=cast(str, db["name"]),
            user=cast(str, db["username"]),
            password=cast(str, db["password"]),
        )
        # Pool sizing: every process performs its DB work sequentially (the
        # main poller runs one query per poll cycle, each worker one commit
        # at a time), so one connection is enough for the steady state. The
        # maximum is a safety margin for transient overlap and is tunable
        # through configuration/environment variables.
        self._pool_min_size = int(str(db.get("pool_min_size", 1)))
        self._pool_max_size = int(str(db.get("pool_max_size", 10)))
        self._pool_timeout = float(str(db.get("pool_timeout", 30.0)))
        self._pool = None

    @override
    def __getstate__(self) -> dict[str, object]:
        # A pool holds live connections, threads and background tasks and
        # cannot be pickled or forked. Every process must open its own pool
        # by calling `open()` after the process has started.
        state: dict[str, object] = self.__dict__.copy()
        state["_pool"] = None
        return state

    @property
    def is_open(self) -> bool:
        """Whether the connection pool has been opened."""
        return self._pool is not None

    @override
    async def open(self) -> None:
        """Create and open the connection pool. Idempotent.

        The pool is opened with ``wait=False``: connections are established
        lazily by the pool's background tasks, so a database that is not up
        yet at startup does not crash the process — calls simply wait (up to
        ``pool_timeout`` seconds) for a connection and fail individually,
        preserving the self-healing behaviour of the previous per-call
        connect pattern.
        """
        if self._pool is not None:
            return
        pool: AsyncConnectionPool[psycopg.AsyncConnection] = AsyncConnectionPool(
            conninfo=self._conninfo,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
            timeout=self._pool_timeout,
            configure=self.configure_connection,
            open=False,
        )
        self._pool = pool
        try:
            await pool.open(wait=False)
        except Exception:
            self._pool = None
            raise

    @override
    async def close(self) -> None:
        """Close the connection pool, releasing every pooled connection."""
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @staticmethod
    async def configure_connection(conn: psycopg.AsyncConnection) -> None:
        # psycopg3 starts an implicit transaction on the first statement, and
        # `async with pool.connection()` commits it on clean exit and rolls
        # it back on error. Keep autocommit off so that this matches the
        # semantics of the old psycopg2 `with connection:` blocks: each
        # provider call is a single transaction.
        # NOTE: on async connections `autocommit` is a read-only property;
        # it must be toggled through the awaitable `set_autocommit()` method.
        await conn.set_autocommit(False)

    def _acquire(self) -> AsyncConnectionPool[psycopg.AsyncConnection]:
        if self._pool is None:
            raise RuntimeError(
                "Database connection pool is not open; call open() first"
            )
        return self._pool

    @staticmethod
    def commit_from_row(row: tuple[object, ...]) -> Commit:
        real_exercise_id = row[18] if row[17] else row[2]
        aws_key = cast(str, row[15])
        slash_index = aws_key.rfind("/")
        fname = aws_key[slash_index + 1 :]
        c = Commit(
            cast(int, row[0]),  # id
            cast(str, row[1]),  # user_email
            cast(int, row[2]),  # exercise_id
            cast(int, real_exercise_id),  # real_exercise_id
            cast(int, row[3]),  # status
            cast(str, row[4]),  # hash
            cast(int, row[5]),  # corrects
            cast(float, row[6]),  # score
            cast(bool, row[7]),  # compiled
            cast(str, row[8]),  # compiled_message
            cast(datetime.datetime, row[9]),  # commit_time
            cast(datetime.datetime | None, row[10]),  # compilation_started
            cast(datetime.datetime | None, row[11]),  # compilation_finished
            cast(str | int | None, row[12]),  # compiled_signal
            cast(str, row[13]),  # compiled_error
            cast(str, row[14]),  # ip
            aws_key,  # aws_key
            cast(int, row[16]),  # offering_id
            cast(int, row[20]),  # real_offering_id
            cast(int, row[19]),  # course_id
            fname,
        )
        return c

    @override
    async def fetch_commits_in_queue(self) -> list[Commit]:
        query = (
            "SELECT com.id"
            "     , com.user_email"
            "     , com.exercise_id"
            "     , com.status"
            "     , com.hash"
            "     , com.corrects"
            "     , com.score"
            "     , com.compiled"
            "     , com.compiled_message"
            "     , com.commit_time"
            "     , com.compilation_started"
            "     , com.compilation_finished"
            "     , com.compiled_signal"
            "     , com.compiled_error"
            "     , com.ip"
            "     , com.aws_key"
            "     , exe.offering_id"
            "     , exe.ghost"
            "     , exe.real_id AS real_exercise_id"
            "     , off.course_id"
            "     , CASE"
            "       WHEN exe.ghost=FALSE THEN exe.offering_id"
            "       ELSE (SELECT exe2.offering_id"
            "             FROM exercises AS exe2"
            "             WHERE exe2.id = exe.REAL_ID)"
            "       END AS real_offering_id"
            " FROM commits AS com,"
            "      exercises AS exe,"
            "      offerings AS off"
            " WHERE exe.offering_id = off.id"
            "   AND com.exercise_id = exe.id"
            "   AND status = %s"
            " ORDER BY com.commit_time ASC"
        )

        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(query, (Commit.STATUS_IN_QUEUE,))
            commits: list[Commit] = []
            async for row in cursor:
                commits.append(Postgres.commit_from_row(row))

        for commit in commits:
            if commit.fname is not None:
                commit.language = language_from_extension(commit.fname)
        return commits

    @override
    async def update_commit(self, commit: Commit) -> None:
        query = (
            "UPDATE commits SET"
            " user_email = %s,"
            " exercise_id = %s,"
            " status = %s,"
            " hash = %s,"
            " corrects = %s,"
            " score = %s,"
            " compiled = %s,"
            " compiled_message = %s,"
            " commit_time = %s,"
            " compilation_started = %s,"
            " compilation_finished = %s,"
            " compiled_signal = %s,"
            " compiled_error = %s"
            " WHERE id = %s"
        )

        compiled_message = (commit.compiled_message or "").encode("utf8")
        compiled_message = base64.b64encode(compiled_message).decode("utf8")

        compiled_error = (commit.compiled_error or "").encode("utf8")
        compiled_error = base64.b64encode(compiled_error).decode("utf8")

        data = (
            commit.user_email,
            commit.exercise_id,
            commit.status,
            commit.commit_hash,
            commit.corrects,
            commit.score,
            commit.is_compiled,
            compiled_message,  # encoded
            commit.commit_time,
            commit.compilation_started_time,
            commit.compilation_finished_time,
            commit.compiled_signal,
            compiled_error,  # encoded
            commit.id,
        )
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(query, data)

    @override
    async def store_commit_test_results(
        self, commit: Commit, test_results: list[TestCaseResult]
    ) -> None:
        query = (
            "INSERT INTO commits_exercise_cases(commit_id"
            "                                 , exercise_case_id"
            "                                 , cputime"
            "                                 , memused"
            "                                 , output"
            "                                 , output_type"
            "                                 , status"
            "                                 , status_message"
            "                                 , error)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        # All rows for a commit are written inside a single transaction: the
        # `async with pool.connection()` block commits on clean exit and rolls
        # everything back if any insert fails.
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            for test_case_result in test_results:
                data = (
                    commit.id,
                    test_case_result.test_case_id,
                    test_case_result.cpu_time,
                    test_case_result.mem_used,  # unused
                    test_case_result.output,  # unused
                    test_case_result.output_type,  # unused
                    test_case_result.status,
                    test_case_result.status_message,
                    test_case_result.error,
                )  # unused
                _ = await cursor.execute(query, data)

    @override
    async def delete_commit_test_results(self, commit: Commit) -> None:
        query = "DELETE FROM commits_exercise_cases WHERE commit_id = %s"
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(query, (commit.id,))

    @override
    async def claim_commit(self, commit: Commit) -> bool:
        """Atomically mark an ``STATUS_IN_QUEUE`` commit as PROCESSING.

        The poller can enqueue the same commit more than once (a commit
        stays IN_QUEUE until a worker takes it, e.g. while it waits in the
        bounded task queue), so several workers may pull copies of it. The
        conditional UPDATE makes the claim exclusive: exactly one worker
        flips the status and gets ``True``; every other copy's update
        matches zero rows and gets ``False``. ``compilation_started``
        is set here so a PROCESSING row is never observed without a start
        time (the worker later refreshes it with the same meaning).
        """
        query = (
            "UPDATE commits"
            " SET status = %s, compilation_started = %s"
            " WHERE id = %s AND status = %s"
        )
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(
                query,
                (
                    Commit.STATUS_PROCESSING,
                    datetime.datetime.now(),
                    commit.id,
                    Commit.STATUS_IN_QUEUE,
                ),
            )
            return cursor.rowcount == 1

    @override
    async def release_commit(self, commit: Commit) -> None:
        """Return a claimed commit to the queue (PROCESSING -> IN_QUEUE).

        Only called by the worker that holds the claim, after a retryable
        failure. The status guard keeps a stale release from clobbering a
        final status written in the meantime; the start time is cleared so
        the row is restored to the same state as a fresh queue entry.
        """
        query = (
            "UPDATE commits"
            " SET status = %s, compilation_started = NULL"
            " WHERE id = %s AND status = %s"
        )
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(
                query,
                (Commit.STATUS_IN_QUEUE, commit.id, Commit.STATUS_PROCESSING),
            )

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        query = "SELECT path FROM compilation_files WHERE exercise_id = %s"
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            _ = await cursor.execute(query, (commit.real_exercise_id,))
            return [cast(str, row[0]) async for row in cursor]

    @staticmethod
    def test_case_from_row(row: tuple[object, ...]) -> TestCase:
        t = TestCase(
            cast(int, row[0]),
            cast(int, row[1]),
            cast(int, row[2]),
            cast(int, row[3]),
            cast(bool, row[4]),
            cast(bool, row[5]),
            cast(int, row[6]),
            cast(int, row[7]),
            cast(int, row[8]),
            cast(bool, row[9]),
            cast(int, row[10]),
            cast(float | None, row[11]),
            row[12],
        )
        return t

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        query = (
            "SELECT id"
            "     , exercise_id"
            "     , input_type"
            "     , output_type"
            "     , show_input"
            "     , show_expected_output"
            "     , maxmemsize"
            "     , cputime"
            "     , stacksize"
            "     , show_user_output"
            "     , file_size"
            "     , abs_error"
            "     , last_update"
            " FROM exercise_cases"
            " WHERE exercise_id = %s"
            " ORDER BY id"
        )
        # One batched query for every test case's files instead of one query
        # per test case (the old N+1 pattern). psycopg3 adapts the list of
        # ids to a Postgres array for ``= ANY(%s)``. Rows are attributed to
        # their case by ``exercise_case_id``, preserving per-case file order:
        # the single scan returns rows in the same (per-case) order the old
        # per-case queries observed. File order is not semantically
        # significant anyway (files are addressed by name).
        files_query = (
            "SELECT exercise_case_id, path"
            " FROM exercise_case_files"
            " WHERE exercise_case_id = ANY(%s)"
        )
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            # Fetch test case metadata
            _ = await cursor.execute(query, (commit.real_exercise_id,))
            test_cases: list[TestCase] = []
            async for row in cursor:
                test_cases.append(Postgres.test_case_from_row(row))
            # Fetch the list of files of every test case in a single round
            # trip (skipped when the exercise has no test cases: an empty
            # array would be a pointless round trip).
            if test_cases:
                _ = await cursor.execute(
                    files_query, ([test_case.id for test_case in test_cases],)
                )
                files_by_case: dict[object, list[str]] = {}
                async for row in cursor:
                    files_by_case.setdefault(cast(object, row[0]), []).append(
                        cast(str, row[1])
                    )
                for test_case in test_cases:
                    test_case.files = files_by_case.get(test_case.id, [])
            return test_cases
