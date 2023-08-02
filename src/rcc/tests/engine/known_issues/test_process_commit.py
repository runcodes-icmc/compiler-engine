import datetime
import logging
import multiprocessing as mp
import os
import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
import shutil
import sys
import unittest
import zipfile

from rcc.model import Commit, TestCase

# Metadata sample
# {
#    'id': 0,
#    'user_email': '',
#    'fname': '',
#    'language_name': '',
#    'expected_status': Commit.STATUS_,
#    'exercise': {
#        'id': 0,
#        'test_cases': [
#            {
#                'id': 0,
#                'out_type': TestCase.IO_TYPE_,
#                'files': ['fname', ...],
#            },
#            ...
#        ],
#    },
# }
commit_metadata = [
    {
        "id": 1,
        "user_email": "Python 3 input() issue",
        "fname": "rc_C01.py",
        "language_name": "Python 3",
        "expected_status": Commit.STATUS_COMPLETED,
        "exercise": {
            "id": 1,
            "test_cases": [{"id": 10, "out_type": TestCase.IO_TYPE_TEXT,},],
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
                {"id": i, "out_type": TestCase.IO_TYPE_TEXT,} for i in range(4)
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
                {"id": 10, "out_type": TestCase.IO_TYPE_TEXT, "files": ["3.in"],},
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
                {"id": i, "out_type": TestCase.IO_TYPE_TEXT,} for i in range(1, 11)
            ],
        },
    },
]


def build_commit(metadata):
    c = Commit(
        metadata["id"],
        metadata["user_email"],
        metadata["exercise"]["id"],
        metadata["exercise"]["id"],
        Commit.STATUS_IN_QUEUE,
        "",
        0,
        0,
        False,
        "",
        datetime.datetime.now(),
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
        metadata["language_name"],
    )
    c.test_cases = metadata["exercise"]["test_cases"]
    return c


class MockDataProvider(rcc.provider.data.DataProvider):
    def __init__(self):
        self.num_calls_update_commit = 0
        self.num_calls_store_commit_test_results = 0
        self.num_calls_delete_commit_test_results = 0
        self.num_calls_fetch_exercise_files = 0
        self.num_calls_fetch_test_cases = 0

    def fetch_commits_in_queue(self):
        pass

    def update_commit(self, commit):
        self.num_calls_update_commit += 1

    def store_commit_test_results(self, commit, test_results):
        self.num_calls_store_commit_test_results += 1

    def delete_commit_test_results(self, commit):
        self.num_calls_delete_commit_test_results += 1

    def fetch_exercise_files(self, commit):
        self.num_calls_fetch_exercise_files += 1
        return []

    def fetch_test_cases(self, commit):
        test_cases = []
        for test_case_metadata in commit.test_cases:
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
    def __init__(self, cfg):
        self.dirname = os.path.dirname(os.path.realpath(__file__))
        pass

    def fetch_commit_file(self, commit, destination):
        source = os.path.join(self.dirname, "commits", str(commit.id), commit.fname)
        shutil.copyfile(source, destination)

    def fetch_exercise_file(self, source, destination):
        pass

    def fetch_test_case_input_file(self, test_case, destination):
        source = os.path.join(
            self.dirname,
            "exercises",
            str(test_case.exercise_id),
            ".".join([str(test_case.id), "in"]),
        )
        shutil.copyfile(source, destination)

    def fetch_test_case_output_file(self, test_case, destination):
        source = os.path.join(
            self.dirname,
            "exercises",
            str(test_case.exercise_id),
            ".".join([str(test_case.id), "out"]),
        )
        shutil.copyfile(source, destination)

    def fetch_test_case_files(self, test_case, destination):
        for fname in test_case.files:
            source = os.path.join(
                self.dirname,
                "exercises",
                str(test_case.exercise_id),
                str(test_case.id),
                fname,
            )
            shutil.copyfile(source, os.path.join(destination, fname))

    def store_commit_output(self, commit, commit_output_fname):
        pass


class TestEngineKnownIssues(unittest.TestCase):
    def setUp(self):
        self.data_prov = MockDataProvider()
        self.S3Provider = rcc.provider.storage.S3
        rcc.provider.storage.S3 = MockStorageProvider
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.handler)

    def tearDown(self):
        rcc.provider.storage.S3 = self.S3Provider
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.removeHandler(self.handler)

    def test_process_commit_known_issues(self):
        for metadata in commit_metadata:
            commit = build_commit(metadata)
            with self.subTest(name=commit.user_email):
                rcc.engine.process_commit(self.data_prov, commit)
                self.assertEqual(commit.status, metadata["expected_status"])
