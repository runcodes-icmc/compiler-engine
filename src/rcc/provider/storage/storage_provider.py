from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...model import Commit, TestCase


class StorageProvider:
    def fetch_commit_file(self, _commit: Commit, _destination: str) -> None:
        raise NotImplementedError()

    def fetch_exercise_file(self, _source: str, _destination: str) -> None:
        raise NotImplementedError()

    def fetch_test_case_input_file(
        self, _test_case: TestCase, _destination: str
    ) -> None:
        raise NotImplementedError()

    def fetch_test_case_output_file(
        self, _test_case: TestCase, _destination: str
    ) -> None:
        raise NotImplementedError()

    def fetch_test_case_files(self, _test_case: TestCase, _destination: str) -> None:
        raise NotImplementedError()

    def store_commit_output(self, _commit: Commit, _commit_output_fname: str) -> None:
        raise NotImplementedError()
