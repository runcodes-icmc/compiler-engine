from __future__ import annotations

import asyncio
import datetime
import logging
import sys
import unittest
import zipfile
from typing import TextIO, cast, override

import tests.engine.test_process_commit_hello as hello

import rcc.config
import rcc.engine
import rcc.provider.storage
from rcc.languages import Language
from rcc.model import Commit, TestCase

make_c = """
all:
	gcc -o hello hello.c
run:
	./hello
"""

make_cpp = """
all:
	g++ -o hello hello.cpp
run:
	./hello
"""

make_f90 = """
all:
	gfortran -o hello hello.f90
run:
	./hello
"""

make_hs = """
all:
	ghc -o hello hello.hs
run:
	./hello
"""

make_java = """
all:
	javac hello.java
run:
	java Main
"""

# Unsupported
make_m = """
all:
	true
run:
	octave hello.m
"""

make_pas = """
all:
	fpc -ohello hello.pas
run:
	./hello
"""

# Unsupported
make_por = """
all:
	true
run:
	true
"""

make_py2 = """
all:
	true
run:
	python2 hello.py
"""

make_py3 = """
all:
	true
run:
	python3 hello.py
"""

# Unsupported
make_r = """
all:
	true
run:
	true
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
        datetime.datetime.now(),
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
        sources = [
            hello.hello_c_src,
            hello.hello_cpp_src,
            hello.hello_f90_src,
            hello.hello_hs_src,
            hello.hello_java_src,
            hello.hello_m_src,
            hello.hello_pas_src,
            hello.hello_por_src,
            hello.hello_py2_src,
            hello.hello_py3_src,
            hello.hello_r_src,
        ]
        makes = [
            make_c,
            make_cpp,
            make_f90,
            make_hs,
            make_java,
            make_m,
            make_pas,
            make_por,
            make_py2,
            make_py3,
            make_r,
        ]
        names = ["c", "cpp", "f90", "hs", "java", "m", "pas", "por", "py", "py", "r"]
        with zipfile.ZipFile(destination, "w") as zip_file:
            src_fname = ".".join(("hello", names[commit.id - 1]))
            src_code = sources[commit.id - 1]
            make = makes[commit.id - 1]
            zip_file.writestr(src_fname, src_code)
            zip_file.writestr("Makefile", make)

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


class TestEngineZip(unittest.TestCase):
    data_prov: hello.MockDataProvider = hello.MockDataProvider()
    storage_provider_class: object = rcc.provider.storage.S3
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(sys.stdout)

    @override
    def setUp(self) -> None:
        self.data_prov = hello.MockDataProvider()
        self.storage_provider_class = rcc.provider.storage.S3
        setattr(rcc.provider.storage, "S3", MockStorageProvider)
        # Ensure configuration is registered for tests
        cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
        if cfg is None:
            cfg = rcc.config.from_dict(rcc.config.DEFAULT_CONFIG, {})
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.addHandler(self.handler)

    @override
    def tearDown(self) -> None:
        setattr(rcc.provider.storage, "S3", self.storage_provider_class)
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.removeHandler(self.handler)

    def run_test_process_commit(self, commit: Commit) -> None:
        cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
        asyncio.run(rcc.engine.process_commit(self.data_prov, commit, cfg))
        self.assertEqual(commit.status, Commit.STATUS_COMPLETED)
        self.assertEqual(commit.score, 10)
        self.assertEqual(commit.corrects, 1)

    def test_process_commit_zip_c(self) -> None:
        commit = build_commit(1, "c", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_cpp(self) -> None:
        commit = build_commit(2, "cpp", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_f90(self) -> None:
        commit = build_commit(3, "f90", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_hs(self) -> None:
        commit = build_commit(4, "hs", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_java(self) -> None:
        commit = build_commit(5, "java", "hello.zip", "Zip/Makefile")
        cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
        asyncio.run(rcc.engine.process_commit(self.data_prov, commit, cfg))
        self.run_test_process_commit(commit)
