import asyncio
import datetime
import logging
import os
import sys
import unittest
from typing import TextIO, cast, override

import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
from rcc.languages import Language
from rcc.model import Commit, TestCase, TestCaseResult

hello_c_src = """
#include <stdio.h>

int main() {
    printf("Hello, run.codes!\\n");
    return 0;
}
"""

hello_cpp_src = """
#include <iostream>

int main() {
    std::cout << "Hello, run.codes!\\n";
    return 0;
}
"""

hello_f90_src = """
program main
  implicit none

  write ( *, '(a)' ) 'Hello, run.codes!'

  stop
end
"""

hello_hs_src = """
main = putStrLn "Hello, run.codes!"
"""

hello_java_src = """
class Main {
    public static void main(String args[]) {
        System.out.println("Hello, run.codes!");
    }
}
"""

hello_m_src = """
printf("Hello, run.codes!\\n");
"""

hello_pas_src = """
program hello;
begin
    writeln('Hello, run.codes!');
end.
"""

hello_por_src = """
programa
{
    funcao inicio()
    {
        escreva("Hello, run.codes!")
    }
}
"""

hello_py2_src = """
print 'Hello, run.codes!'
"""

hello_py3_src = """
print('Hello, run.codes!')
"""

hello_r_src = """
cat("Hello, run.codes!\\n")
"""


def build_commit(
    commit_id: int, user_email: str, fname: str, language_name: str | None
) -> Commit:
    return Commit(
        commit_id,
        user_email,
        1,
        1,
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
        fname,
        1,
        1,
        1,
        fname,
        cast(Language, cast(object, language_name)),
    )


class MockStorageProvider(rcc.provider.storage.StorageProvider):
    def __init__(self, cfg: rcc.config.Config) -> None:
        pass

    @override
    def fetch_commit_file(self, commit: Commit, destination: str) -> None:
        if commit.fname is None:
            raise ValueError("Test commit has no filename")
        with open(destination, "w") as src_file:
            _, extension = os.path.splitext(commit.fname)
            extension = extension[1:]
            if extension == "c":
                src_code = hello_c_src
            elif extension == "cpp":
                src_code = hello_cpp_src
            elif extension == "f90":
                src_code = hello_f90_src
            elif extension == "hs":
                src_code = hello_hs_src
            elif extension == "java":
                src_code = hello_java_src
            elif extension == "m":
                src_code = hello_m_src
            elif extension == "pas":
                src_code = hello_pas_src
            elif extension == "por":
                src_code = hello_por_src
            elif extension == "py":
                language = cast(str, cast(object, commit.language))
                if language == "Python 2":
                    src_code = hello_py2_src
                elif language == "Python 3":
                    src_code = hello_py3_src
                else:
                    raise ValueError("Invalid commit")
            elif extension == "r":
                src_code = hello_r_src
            else:
                raise ValueError("Invalid commit")
            _ = src_file.write(src_code)

    @override
    def fetch_exercise_file(self, source: str, destination: str) -> None:
        pass

    @override
    def fetch_test_case_input_file(self, test_case: TestCase, destination: str) -> None:
        if test_case.id == 5432:
            with open(destination, "w") as in_file:
                _ = in_file.write("This input should be ignored.\n")

    @override
    def fetch_test_case_output_file(
        self, test_case: TestCase, destination: str
    ) -> None:
        if test_case.id == 5432:
            with open(destination, "w") as out_file:
                _ = out_file.write("Hello, run.codes!\n")

    @override
    def fetch_test_case_files(self, test_case: TestCase, destination: str) -> None:
        pass

    @override
    def store_commit_output(self, commit: Commit, commit_output_fname: str) -> None:
        pass


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
        self.num_calls_fetch_test_cases += 1
        return [
            TestCase(
                5432,
                commit.real_exercise_id,
                TestCase.IO_TYPE_TEXT,
                TestCase.IO_TYPE_TEXT,
                False,
                False,
                0,
                5,
                0,
                False,
                0,
                0,
                None,
            )
        ]


class TestEngineHello(unittest.TestCase):
    data_prov: MockDataProvider = MockDataProvider()
    storage_from_config: object = rcc.provider.storage.from_config
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(sys.stderr)

    @override
    def setUp(self) -> None:
        self.data_prov = MockDataProvider()
        self.storage_from_config = rcc.provider.storage.from_config
        rcc.provider.storage.from_config = MockStorageProvider
        cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
        if cfg is None:
            # Register a default configuration for tests
            cfg = rcc.config.from_dict(rcc.config.DEFAULT_CONFIG, {})
        cfg.update({"max_output_file_size": 1024 * 1024})
        self.handler = logging.StreamHandler(sys.stderr)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self.handler)

    @override
    def tearDown(self) -> None:
        rcc.provider.storage.from_config = self.storage_from_config
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.removeHandler(self.handler)

    def run_test_process_commit(self, commit: Commit) -> None:
        cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
        asyncio.run(rcc.engine.process_commit(self.data_prov, commit, cfg))
        self.assertEqual(commit.status, Commit.STATUS_COMPLETED)
        self.assertEqual(commit.score, 10)
        self.assertEqual(commit.corrects, 1)

    def test_process_commit_hello_c(self) -> None:
        commit = build_commit(1, "c", "hello.c", "C")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_cpp(self) -> None:
        commit = build_commit(2, "cpp", "hello.cpp", "C++")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_f90(self) -> None:
        commit = build_commit(3, "f90", "hello.f90", "Fortran")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_hs(self) -> None:
        commit = build_commit(4, "hs", "hello.hs", "Haskell")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_java(self) -> None:
        commit = build_commit(5, "java", "hello.java", "Java")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_m(self) -> None:
        commit = build_commit(6, "m", "hello.m", "Octave")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_pas(self) -> None:
        commit = build_commit(7, "pas", "hello.pas", "Pascal")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_por(self) -> None:
        commit = build_commit(8, "por", "hello.por", "Portugol")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_py2(self) -> None:
        commit = build_commit(9, "py2", "hello.py", "Python 2")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_py3(self) -> None:
        commit = build_commit(10, "py3", "hello.py", "Python 3")
        self.run_test_process_commit(commit)

    def test_process_commit_hello_r(self) -> None:
        commit = build_commit(11, "r", "hello.r", "R")
        self.run_test_process_commit(commit)
