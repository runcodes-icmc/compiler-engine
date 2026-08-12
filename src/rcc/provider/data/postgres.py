from __future__ import unicode_literals

import base64

import psycopg
import psycopg.conninfo
from psycopg_pool import AsyncConnectionPool

from rcc.languages import language_from_extension

from ...model import Commit, TestCase
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

    def __init__(self, cfg):
        self._conninfo = psycopg.conninfo.make_conninfo(
            host=cfg.db["host"],
            port=cfg.db["port"],
            dbname=cfg.db["name"],
            user=cfg.db["username"],
            password=cfg.db["password"],
        )
        # Pool sizing: every process performs its DB work sequentially (the
        # main poller runs one query per poll cycle, each worker one commit
        # at a time), so one connection is enough for the steady state. The
        # maximum is a safety margin for transient overlap and is tunable
        # through configuration/environment variables.
        self._pool_min_size = int(cfg.db.get("pool_min_size", 1))
        self._pool_max_size = int(cfg.db.get("pool_max_size", 10))
        self._pool_timeout = float(cfg.db.get("pool_timeout", 30.0))
        self._pool: AsyncConnectionPool[psycopg.AsyncConnection] | None = None

    def __getstate__(self):
        # A pool holds live connections, threads and background tasks and
        # cannot be pickled or forked. Every process must open its own pool
        # by calling `open()` after the process has started.
        state = self.__dict__.copy()
        state["_pool"] = None
        return state

    @property
    def is_open(self):
        """Whether the connection pool has been opened."""
        return self._pool is not None

    async def open(self):
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
            configure=self._configure_connection,
            open=False,
        )
        self._pool = pool
        try:
            await pool.open(wait=False)
        except Exception:
            self._pool = None
            raise

    async def close(self):
        """Close the connection pool, releasing every pooled connection."""
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @staticmethod
    async def _configure_connection(conn: psycopg.AsyncConnection) -> None:
        # psycopg3 starts an implicit transaction on the first statement, and
        # `async with pool.connection()` commits it on clean exit and rolls
        # it back on error. Keep autocommit off so that this matches the
        # semantics of the old psycopg2 `with connection:` blocks: each
        # provider call is a single transaction.
        # NOTE: psycopg_pool 3.3+ requires this callback to be awaitable.
        conn.autocommit = False

    def _acquire(self) -> AsyncConnectionPool[psycopg.AsyncConnection]:
        if self._pool is None:
            raise RuntimeError(
                "Database connection pool is not open; call open() first"
            )
        return self._pool

    @staticmethod
    def commit_from_row(row):
        real_exercise_id = row[18] if row[17] else row[2]
        slash_index = row[15].rfind("/")
        fname = row[15][slash_index + 1 :]
        c = Commit(
            row[0],  # id
            row[1],  # user_email
            row[2],  # exercise_id
            real_exercise_id,  # real_exercise_id
            row[3],  # status
            row[4],  # hash
            row[5],  # corrects
            row[6],  # score
            row[7],  # compiled
            row[8],  # compiled_message
            row[9],  # commit_time
            row[10],  # compilation_started
            row[11],  # compilation_finished
            row[12],  # compiled_signal
            row[13],  # compiled_error
            row[14],  # ip
            row[15],  # aws_key
            row[16],  # offering_id
            row[20],  # real_offering_id
            row[19],  # course_id
            fname,
        )
        return c

    async def fetch_commits_in_queue(self):
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
            await cursor.execute(query, (Commit.STATUS_IN_QUEUE,))
            commits = []
            async for row in cursor:
                commits.append(Postgres.commit_from_row(row))

        for commit in commits:
            if commit.fname is not None:
                commit.language = language_from_extension(commit.fname)
        return commits

    async def update_commit(self, commit):
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
            await cursor.execute(query, data)

    async def store_commit_test_results(self, commit, test_results):
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
                await cursor.execute(query, data)

    async def delete_commit_test_results(self, commit):
        query = "DELETE FROM commits_exercise_cases WHERE commit_id = %s"
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            await cursor.execute(query, (commit.id,))

    async def fetch_exercise_files(self, commit):
        query = "SELECT path FROM compilation_files WHERE exercise_id = %s"
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            await cursor.execute(query, (commit.real_exercise_id,))
            return [row[0] async for row in cursor]

    @staticmethod
    def test_case_from_row(row):
        t = TestCase(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
        )
        return t

    async def fetch_test_cases(self, commit):
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
        files_query = "SELECT path FROM exercise_case_files WHERE exercise_case_id = %s"
        async with self._acquire().connection() as conn, conn.cursor() as cursor:
            # Fetch test case metadata
            await cursor.execute(query, (commit.real_exercise_id,))
            test_cases = []
            async for row in cursor:
                test_cases.append(Postgres.test_case_from_row(row))
            # Fetch the list of files of each test case
            for test_case in test_cases:
                await cursor.execute(files_query, (test_case.id,))
                test_case.files = [row[0] async for row in cursor]
            return test_cases
