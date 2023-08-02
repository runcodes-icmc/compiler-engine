from __future__ import unicode_literals

from .data_provider import DataProvider
from ...model import Commit, TestCase

import base64
import contextlib
import os
import psycopg2
import rcc.util
from rcc.languages import language_from_extension


class Postgres(DataProvider):
    def __init__(self, cfg):
        self.host = cfg.db["host"]
        self.port = cfg.db["port"]
        self.db_name = cfg.db["name"]
        self.username = cfg.db["username"]
        self.password = cfg.db["password"]

    def _connect(self):
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.db_name,
            user=self.username,
            password=self.password,
        )

    @staticmethod
    def commit_from_row(row):
        real_exercise_id = row[18] if row[17] else row[2]
        slash_index = row[15].rfind("/")
        fname = row[15][slash_index + 1:]
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

    def fetch_commits_in_queue(self):
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

        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (Commit.STATUS_IN_QUEUE,))
                    commits = [Postgres.commit_from_row(row) for row in cursor]
                    for commit in commits:
                        commit.language = language_from_extension(commit.fname)
                    return commits

    def update_commit(self, commit):
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
        compiled_message = base64.b64encode(
            compiled_message).decode("utf8")

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
        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, data)

    def store_commit_test_results(self, commit, test_results):
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
        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
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
                        cursor.execute(query, data)

    def delete_commit_test_results(self, commit):
        query = "DELETE FROM commits_exercise_cases WHERE commit_id = %s"
        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (commit.id,))

    def fetch_exercise_files(self, commit):
        query = "SELECT path FROM compilation_files WHERE exercise_id = %s"
        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (commit.real_exercise_id,))
                    return [row[0] for row in cursor]

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

    def fetch_test_cases(self, commit):
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
        files_query = (
            "SELECT path" " FROM exercise_case_files" " WHERE exercise_case_id = %s"
        )
        with contextlib.closing(self._connect()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    # Fetch test case metadata
                    cursor.execute(query, (commit.real_exercise_id,))
                    test_cases = [Postgres.test_case_from_row(
                        row) for row in cursor]
                    # Fetch the list of files of each test case
                    for test_case in test_cases:
                        cursor.execute(files_query, (test_case.id,))
                        test_case.files = [row[0] for row in cursor]
                    return test_cases
