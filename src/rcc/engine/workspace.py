"""Workspace management: download inputs, prepare files, store outputs."""

import asyncio
import os
import shutil
import zipfile
from typing import TYPE_CHECKING, cast

from ..config import Config
from ..languages import deduce_language, is_compilable, language_from_extension
from ..model import Commit, TestCase
from .common import (
    DEFAULT_MKDIR_PERMISSIONS,
    raise_first_error,
    set_extension,
)

if TYPE_CHECKING:
    from ..provider.data import DataProvider
    from ..provider.storage import StorageProvider


async def download_commit_file(
    cfg: Config, storage_provider: StorageProvider, commit: Commit, base_dir: str
) -> None:
    """Download the commit source file into ``<base_dir>/<cfg.src_dir>``.

    Split out of :func:`copy_source_files` so the download can start
    concurrently with the prefetch database queries in ``process_commit``.
    """
    if commit.fname is None:
        raise ValueError("Commit has no filename; cannot download its source file")
    src_dir = os.path.join(base_dir, str(cfg.src_dir))
    os.makedirs(src_dir, DEFAULT_MKDIR_PERMISSIONS)
    destination = os.path.join(src_dir, os.path.basename(commit.fname))
    # boto3 download runs in a worker thread
    await asyncio.to_thread(storage_provider.fetch_commit_file, commit, destination)


async def copy_source_files(
    cfg: Config,
    data_provider: DataProvider,
    storage_provider: StorageProvider,
    commit: Commit,
    base_dir: str,
    semaphore: asyncio.Semaphore,
) -> None:
    """Copy the exercise's extra source files into ``<base_dir>/<cfg.src_dir>``.

    The commit file itself was already downloaded concurrently with the
    prefetch queries (see :func:`download_commit_file`); zip handling happens
    here. Exercise-file downloads are independent of each other and run
    concurrently, bounded by ``semaphore``.
    """
    fname = commit.fname
    if fname is None:
        raise ValueError("Commit has no filename; cannot copy its source files")
    src_dir = os.path.join(base_dir, str(cfg.src_dir))
    destination = os.path.join(src_dir, os.path.basename(fname))

    set_extension(commit)
    extension = commit.extension
    if extension == "zip":
        with zipfile.ZipFile(destination) as zip_file:
            extension = deduce_language(zip_file)
            commit.extension = extension
            commit.language = language_from_extension(extension)
            zip_file.extractall(src_dir)
    elif extension is not None:
        commit.language = language_from_extension(extension)
    commit.is_compilable = is_compilable(extension)

    # Copy files uploaded with exercise
    fnames = await data_provider.fetch_exercise_files(commit)

    async def copy_exercise_file(fname: str) -> None:
        source = os.path.join(str(commit.real_exercise_id), fname)
        file_destination = os.path.join(src_dir, os.path.basename(fname))
        async with semaphore:
            await asyncio.to_thread(
                storage_provider.fetch_exercise_file, source, file_destination
            )

    results = await asyncio.gather(
        *(copy_exercise_file(fname) for fname in fnames), return_exceptions=True
    )
    raise_first_error(results)


async def copy_test_case_files(
    storage_provider: StorageProvider,
    test_cases: list[TestCase],
    base_dir: str,
    semaphore: asyncio.Semaphore,
) -> None:
    """Download every test case's input and additional files concurrently.

    Each test case's downloads are independent of the others', so one task per
    test case runs in parallel, bounded by ``semaphore``. Directory creation
    stays per test case, right before that case's additional files are
    downloaded.
    """

    async def copy_one(test_case: TestCase) -> None:
        # Copy test case input file (boto3 call in a worker thread)
        dest = os.path.join(base_dir, f"{test_case.id}.in")
        async with semaphore:
            await asyncio.to_thread(
                storage_provider.fetch_test_case_input_file, test_case, dest
            )

        # Copy additional files uploaded to this test case
        test_case_dir = os.path.join(base_dir, f"test_{test_case.id}")
        os.makedirs(test_case_dir, DEFAULT_MKDIR_PERMISSIONS)
        async with semaphore:
            await asyncio.to_thread(
                storage_provider.fetch_test_case_files, test_case, test_case_dir
            )

    results = await asyncio.gather(
        *(copy_one(test_case) for test_case in test_cases), return_exceptions=True
    )
    raise_first_error(results)


def create_container_cfg_file(
    cfg: Config, commit: Commit, test_cases: list[TestCase], base_dir: str
) -> None:
    container_cfg: list[tuple[str, object, bool]] = [
        ("monitor_max_fs", cfg.monitor_max_file_size, False),
        ("monitor_max_ms", cfg.monitor_max_mem_size, False),
        ("compilation_timeout", cfg.compilation_timeout, False),
        ("src_file", commit.fname, True),
    ]
    container_cfg.extend(
        [(f"t_{test.id}", test.cpu_time, False) for test in test_cases]
    )
    with open(os.path.join(base_dir, str(cfg.container_cfg_file)), "w") as cfg_file:
        for cfg_item in container_cfg:
            if cfg_item[2]:  # value needs shell quoting
                print(f"{cfg_item[0]}='{cfg_item[1]}'", file=cfg_file)
            else:
                print(f"{cfg_item[0]}={cfg_item[1]}", file=cfg_file)


def prepare_output_file(cfg: Config, commit: Commit, base_dir: str) -> str:
    def should_truncate(fname: str) -> bool:
        return fname.endswith((".output", ".error"))

    def truncate(fname: str) -> None:
        with open(fname, "a") as f:
            size = f.seek(0, 2)
            if size > cast(int, cfg.max_output_file_size):
                _ = f.seek(0, 0)
                _ = f.truncate(cast(int, cfg.max_output_file_size))

    output_dir = os.path.join(base_dir, str(cfg.output_files_dir))
    output_fname = os.path.join(base_dir, f"{commit.id}.zip")
    with zipfile.ZipFile(output_fname, "w") as output_file:
        for dir_path, _, fnames in os.walk(output_dir):
            for fname in fnames:
                fs_fname = os.path.join(dir_path, fname)
                if should_truncate(fs_fname):
                    truncate(fs_fname)
                ar_dirname = os.path.dirname(fs_fname).replace(output_dir, ".")
                ar_fname = os.path.join(ar_dirname, fname)
                output_file.write(fs_fname, ar_fname)
    return output_fname


def cleanup_tests(base_dir: str) -> None:
    if os.path.isdir(base_dir):
        # Errors propagate: no error handler is passed, so rmtree() raises on
        # the first failure. The previous onexc handler re-raised the same
        # exception, which is equivalent but crashed on Python 3.12+ where
        # the handler receives the exception itself, not a 3-tuple.
        shutil.rmtree(base_dir)
