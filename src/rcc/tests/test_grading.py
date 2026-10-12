"""Unit tests for grading.process_test_results, in particular its handling
of a missing/malformed `monitor_out` file (compiler-monitor-oportunidades.md,
item B6): a sandbox-level failure should fail only the affected test case,
not raise and abort the whole batch.
"""

import datetime
import os
import tempfile
import unittest
from typing import override
from unittest.mock import MagicMock

from rcc.engine.grading import process_test_results
from rcc.model import Commit, TestCase, TestCaseResult


def build_test_case(test_case_id: int = 1) -> TestCase:
    return TestCase(
        test_case_id,
        1,
        TestCase.IO_TYPE_TEXT,
        TestCase.IO_TYPE_TEXT,
        False,
        False,
        0,
        5,
        0,
        False,
        0,
        None,
        None,
    )


def build_commit() -> Commit:
    return Commit(
        1,
        "user@example.com",
        1,
        1,
        Commit.STATUS_RUNNING,
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
        "hello.c",
        1,
        1,
        1,
        "hello.c",
        None,
    )


class TestProcessTestResultsRunInfoHandling(unittest.TestCase):
    tmpdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
    base_dir: str = ""
    storage: MagicMock = MagicMock()
    commit: Commit = build_commit()
    test_case: TestCase = build_test_case(1)

    @override
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = self.tmpdir.name
        self.storage = MagicMock()
        self.commit = build_commit()
        self.test_case = build_test_case(1)
        # process_test_results always stats the `.error` file.
        with open(os.path.join(self.base_dir, "1.error"), "w") as f:
            _ = f.write("")

    @override
    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_missing_monitor_out_does_not_raise(self) -> None:
        # No `1.monitor_out` written at all (monitor never ran / container
        # setup failed before it could).
        result = process_test_results(
            self.storage, self.commit, self.test_case, self.base_dir
        )
        self.assertIsInstance(result, TestCaseResult)
        self.assertEqual(result.status, TestCaseResult.STATUS_INCORRECT)
        self.storage.fetch_test_case_output_file.assert_not_called()

    def test_empty_monitor_out_does_not_raise(self) -> None:
        # `monitor_out` exists but is empty (e.g. the monitor binary itself
        # segfaulted before printing anything, per B4 in the audit doc).
        with open(os.path.join(self.base_dir, "1.monitor_out"), "w") as f:
            _ = f.write("")
        result = process_test_results(
            self.storage, self.commit, self.test_case, self.base_dir
        )
        self.assertIsInstance(result, TestCaseResult)
        self.assertEqual(result.status, TestCaseResult.STATUS_INCORRECT)
        self.storage.fetch_test_case_output_file.assert_not_called()

    def test_malformed_monitor_out_does_not_raise(self) -> None:
        # Present but missing the `time=` key entirely.
        with open(os.path.join(self.base_dir, "1.monitor_out"), "w") as f:
            _ = f.write("exit_status=0\nsignal=\n")
        result = process_test_results(
            self.storage, self.commit, self.test_case, self.base_dir
        )
        self.assertIsInstance(result, TestCaseResult)
        self.assertEqual(result.status, TestCaseResult.STATUS_INCORRECT)

    def test_well_formed_monitor_out_still_works(self) -> None:
        # Control case: a normal, well-formed monitor_out must still be
        # processed exactly as before (no change in behavior for the
        # common path).
        with open(os.path.join(self.base_dir, "1.monitor_out"), "w") as f:
            _ = f.write("exit_status=0\nsignal=\ntime=0.0123\n")
        with open(os.path.join(self.base_dir, "1.output"), "w") as f:
            _ = f.write("Hello, run.codes!\n")
        with open(os.path.join(self.base_dir, "1.out"), "w") as f:
            _ = f.write("Hello, run.codes!\n")

        def fake_fetch(_test_case: TestCase, destination: str) -> None:
            with open(destination, "w") as out:
                _ = out.write("Hello, run.codes!\n")

        self.storage.fetch_test_case_output_file.side_effect = fake_fetch

        result = process_test_results(
            self.storage, self.commit, self.test_case, self.base_dir
        )
        self.assertEqual(result.status, TestCaseResult.STATUS_CORRECT)
        self.assertEqual(result.cpu_time, "0.0123")
        self.assertEqual(result.error, "")


if __name__ == "__main__":
    _ = unittest.main()
