from __future__ import annotations

import asyncio
import datetime
import logging
import os
import shutil
import sys
import unittest
from typing import NotRequired, TextIO, TypedDict, cast, override

import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
from rcc.languages import Language
from rcc.model import Commit, TestCase, TestCaseResult


class TestCaseMetadata(TypedDict):
    id: int
    out_type: int
    files: NotRequired[list[str]]


class ExerciseMetadata(TypedDict):
    id: int
    test_cases: list[TestCaseMetadata]


class CommitMetadata(TypedDict):
    id: int
    user_email: str
    fname: str
    language_name: str
    expected_status: int
    exercise: ExerciseMetadata


commit_metadata: list[CommitMetadata] = [
    {
        "id": 1,
        "user_email": "Python 3 input() issue",
        "fname": "rc_C01.py",
        "language_name": "Python 3",
        "expected_status": Commit.STATUS_COMPLETED,
        "exercise": {
            "id": 1,
            "test_cases": [
                {
                    "id": 10,
                    "out_type": TestCase.IO_TYPE_TEXT,
                },
            ],
        },
    },
    {
        "id": 2,
        "user_email": "Python 3 dies, no output",
        "fname": "rc_C08.py",
        "language_name": "Python 3",
        "expected_status": Commit.STATUS_COMPLETED,
        "exercise": {
            "id": 2,
            "test_cases": [
                {
                    "id": i,
                    "out_type": TestCase.IO_TYPE_TEXT,
                }
                for i in range(4)
            ],
        },
    },
    {
        "id": 3,
        "user_email": "Signal incomum",
        "fname": "02.c",
        "language_name": "C",
        "expected_status": Commit.STATUS_INCOMPLETE,
        "exercise": {
            "id": 3,
            "test_cases": [
                {
                    "id": 10,
                    "out_type": TestCase.IO_TYPE_TEXT,
                    "files": ["3.in"],
                },
            ],
        },
    },
    {
        "id": 4,
        "user_email": "Correção vazia",
        "fname": "1.c",
        "language_name": "C",
        "expected_status": Commit.STATUS_COMPLETED,
        "exercise": {
            "id": 4,
            "test_cases": [
                {
                    "id": i,
                    "out_type": TestCase.IO_TYPE_TEXT,
                }
                for i in range(1, 11)
            ],
        },
    },
]


def build_commit(metadata: CommitMetadata) -> Commit:
    exercise = metadata["exercise"]
    c = Commit(
        metadata["id"],
        metadata["user_email"],
        exercise["id"],
        exercise["id"],
        Commit.STATUS_IN_QUEUE,
        "",
        0,
        0,
        False,
        "",
        datetime.datetime.now(tz=datetime.UTC),
        None,
        None,
        None,
        "",
        None,
        metadata["fname"],
        1,
        1,
        1,
        metadata["fname"],
        cast(
            Language, cast(object, metadata["language_name"])
        ),  # placeholder; set_extension fixes it
    )
    c.test_cases = exercise["test_cases"]
    return c


class MockDataProvider(rcc.provider.data.DataProvider):
    num_calls_update_commit: int
    num_calls_store_commit_test_results: int
    num_calls_delete_commit_test_results: int
    num_calls_fetch_exercise_files: int
    num_calls_fetch_test_cases: int

    def __init__(self) -> None:
        self.num_calls_update_commit = 0
        self.num_calls_store_commit_test_results = 0
        self.num_calls_delete_commit_test_results = 0
        self.num_calls_fetch_exercise_files = 0
        self.num_calls_fetch_test_cases = 0

    @override
    async def fetch_commits_in_queue(self) -> list[Commit]:
        return []

    @override
    async def update_commit(self, commit: Commit) -> None:
        self.num_calls_update_commit += 1

    @override
    async def store_commit_test_results(
        self, commit: Commit, test_results: list[TestCaseResult]
    ) -> None:
        self.num_calls_store_commit_test_results += 1

    @override
    async def delete_commit_test_results(self, commit: Commit) -> None:
        self.num_calls_delete_commit_test_results += 1

    @override
    async def fetch_exercise_files(self, commit: Commit) -> list[str]:
        self.num_calls_fetch_exercise_files += 1
        return []

    @override
    async def fetch_test_cases(self, commit: Commit) -> list[TestCase]:
        test_cases: list[TestCase] = []
        for test_case_metadata in cast(list[TestCaseMetadata], commit.test_cases):
            test_case = TestCase(
                test_case_metadata["id"],
                commit.real_exercise_id,
                TestCase.IO_TYPE_TEXT,
                test_case_metadata["out_type"],
                False,
                False,
                0,
                5,
                0,
                False,
                0,
                0,
                None,
                test_case_metadata.get("files", []),
            )
            test_cases.append(test_case)
        return test_cases


class MockStorageProvider(rcc.provider.storage.StorageProvider):
    dirname: str

    def __init__(self, cfg: rcc.config.Config) -> None:
        self.dirname = os.path.dirname(os.path.realpath(__file__))

    @override
    def fetch_commit_file(self, commit: Commit, destination: str) -> None:
        if commit.fname is None:
            raise ValueError("Test commit has no filename")
        source = os.path.join(self.dirname, "commits", str(commit.id), commit.fname)
        _ = shutil.copyfile(source, destination)

    @override
    def fetch_exercise_file(self, source: str, destination: str) -> None:
        pass

    @override
    def fetch_test_case_input_file(self, test_case: TestCase, destination: str) -> None:
        source = os.path.join(
            self.dirname,
            "exercises",
            str(test_case.exercise_id),
            ".".join([str(test_case.id), "in"]),
        )
        _ = shutil.copyfile(source, destination)

    @override
    def fetch_test_case_output_file(
        self, test_case: TestCase, destination: str
    ) -> None:
        source = os.path.join(
            self.dirname,
            "exercises",
            str(test_case.exercise_id),
            ".".join([str(test_case.id), "out"]),
        )
        _ = shutil.copyfile(source, destination)

    @override
    def fetch_test_case_files(self, test_case: TestCase, destination: str) -> None:
        for fname in test_case.files:
            source = os.path.join(
                self.dirname,
                "exercises",
                str(test_case.exercise_id),
                str(test_case.id),
                fname,
            )
            _ = shutil.copyfile(source, os.path.join(destination, fname))

    @override
    def store_commit_output(self, commit: Commit, commit_output_fname: str) -> None:
        pass


class TestEngineKnownIssues(unittest.TestCase):
    data_prov: MockDataProvider = MockDataProvider()
    storage_provider_class: object = rcc.provider.storage.S3
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(sys.stdout)

    @override
    def setUp(self) -> None:
        self.data_prov = MockDataProvider()
        self.storage_provider_class = rcc.provider.storage.S3
        rcc.provider.storage.S3 = MockStorageProvider
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.handler)

    @override
    def tearDown(self) -> None:
        rcc.provider.storage.S3 = self.storage_provider_class
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.removeHandler(self.handler)

    def test_process_commit_known_issues(self) -> None:
        for metadata in commit_metadata:
            commit = build_commit(metadata)
            with self.subTest(name=commit.user_email):
                cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
                asyncio.run(rcc.engine.process_commit(self.data_prov, commit, cfg))
                self.assertEqual(commit.status, metadata["expected_status"])
