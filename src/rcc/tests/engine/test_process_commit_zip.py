from six import StringIO

import datetime
import logging
import multiprocessing as mp
import rcc.config
import rcc.engine
import rcc.provider.data
import rcc.provider.storage
import sys
import unittest
import zipfile

import tests.engine.test_process_commit_hello as hello

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


def build_commit(commit_id, user_email, fname, language_name):
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
        language_name,
    )


class MockStorageProvider(rcc.provider.storage.StorageProvider):
    def __init__(self, cfg):
        pass

    def fetch_commit_file(self, commit, destination):
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
            with StringIO() as src_file:
                zip_file.writestr(src_fname, src_code)
                zip_file.writestr("Makefile", make)

    def fetch_exercise_file(self, source, destination):
        pass

    def fetch_test_case_input_file(self, test_case, destination):
        if test_case.id == 5432:
            with open(destination, "w") as in_file:
                in_file.write("This input should be ignored.\n")

    def fetch_test_case_output_file(self, test_case, destination):
        if test_case.id == 5432:
            with open(destination, "w") as out_file:
                out_file.write("Hello, run.codes!\n")

    def fetch_test_case_files(self, test_case, destination):
        pass

    def store_commit_output(self, commit, commit_output_fname):
        pass


class TestEngineZip(unittest.TestCase):
    def setUp(self):
        self.data_prov = hello.MockDataProvider()
        self.S3Provider = rcc.provider.storage.S3
        rcc.provider.storage.S3 = MockStorageProvider
        self.handler = logging.StreamHandler(sys.stdout)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.addHandler(self.handler)

    def tearDown(self):
        rcc.provider.storage.S3 = self.S3Provider
        logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
        logger.removeHandler(self.handler)

    def run_test_process_commit(self, commit):
        rcc.engine.process_commit(self.data_prov, commit)
        self.assertEqual(commit.status, Commit.STATUS_COMPLETED)
        self.assertEqual(commit.score, 10)
        self.assertEqual(commit.corrects, 1)

    def test_process_commit_zip_c(self):
        commit = build_commit(1, "c", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_cpp(self):
        commit = build_commit(2, "cpp", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_f90(self):
        commit = build_commit(3, "f90", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_hs(self):
        commit = build_commit(4, "hs", "hello.zip", "Zip/Makefile")
        self.run_test_process_commit(commit)

    def test_process_commit_zip_java(self):
        commit = build_commit(5, "java", "hello.zip", "Zip/Makefile")
        rcc.engine.process_commit(self.data_prov, commit)
        self.run_test_process_commit(commit)

    # def test_process_commit_zip_m(self):
    #    commit = build_commit(6, 'm', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)

    # def test_process_commit_zip_pas(self):
    #    commit = build_commit(7, 'pas', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)

    # def test_process_commit_zip_por(self):
    #    commit = build_commit(8, 'por', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)

    # def test_process_commit_zip_py2(self):
    #    commit = build_commit(9, 'py2', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)

    # def test_process_commit_zip_py3(self):
    #    commit = build_commit(10, 'py3', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)

    # def test_process_commit_zip_r(self):
    #    commit = build_commit(11, 'r', 'hello.zip', 'Zip/Makefile')
    #    self.run_test_process_commit(commit)
