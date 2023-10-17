from __future__ import division, print_function, unicode_literals

import asyncio
import asyncio.subprocess
import configparser
import contextlib
import datetime
import docker
import filecmp
import itertools as it
import multiprocessing as mp
import logging
import os
import requests
import shutil
import zipfile

import rcc.config
import rcc.cmp
import rcc.provider
import rcc.provider.storage
import rcc.util

from rcc.model import Commit, TestCase, TestCaseResult

from .languages import language_from_extension

DEFAULT_MKDIR_PERMISSIONS = 0o777


def set_extension(commit):
    _, extension = os.path.splitext(commit.fname)
    extension = rcc.util.standardize_extension(extension[1:])
    commit.extension = extension


def copy_source_files(data_provider, storage_provider, commit, base_dir):
    cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
    src_dir = os.path.join(base_dir, cfg.src_dir)
    os.makedirs(src_dir, DEFAULT_MKDIR_PERMISSIONS)

    # Copy commit files
    destination = os.path.join(src_dir, os.path.basename(commit.fname))
    storage_provider.fetch_commit_file(commit, destination)

    # Add info about file extension and whether the submission is compilable
    set_extension(commit)
    if commit.extension == "zip":
        # Extra tasks if we have a zip (extract, deduce language of files)
        with zipfile.ZipFile(destination) as zip_file:
            commit.extension = rcc.util.deduce_language(zip_file)
            commit.language = rcc.util.language_from_extension(commit.extension)
            zip_file.extractall(src_dir)
    commit.is_compilable = rcc.util.is_compilable(commit.extension)

    # Copy files uploaded with exercise
    fnames = data_provider.fetch_exercise_files(commit)
    for fname in fnames:
        fname = os.path.join(str(commit.real_exercise_id), fname)
        destination = os.path.join(src_dir, os.path.basename(fname))
        storage_provider.fetch_exercise_file(fname, destination)


def copy_test_case_files(storage_provider, test_cases, base_dir):
    for test_case in test_cases:
        # Copy test case input file
        dest = os.path.join(base_dir, "{}.in".format(test_case.id))
        storage_provider.fetch_test_case_input_file(test_case, dest)

        # Copy additional files uploaded to this test case
        test_case_dir = os.path.join(base_dir, "test_{}".format(test_case.id))
        os.makedirs(test_case_dir, DEFAULT_MKDIR_PERMISSIONS)
        storage_provider.fetch_test_case_files(test_case, test_case_dir)


def create_container_cfg_file(commit, test_cases, base_dir):
    cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
    container_cfg = [
        ("monitor_max_fs", cfg.monitor_max_file_size, False),
        ("monitor_max_ms", cfg.monitor_max_mem_size, False),
        ("compilation_timeout", cfg.compilation_timeout, False),
        ("src_file", commit.fname, True),
    ]
    container_cfg.extend(
        [("t_{}".format(test.id), test.cpu_time, False) for test in test_cases]
    )
    with open(os.path.join(base_dir, cfg.container_cfg_file), "w") as cfg_file:
        for cfg_item in container_cfg:
            if cfg_item[2]:  # should quote?
                print("{c[0]}='{c[1]}'".format(c=cfg_item), file=cfg_file)
            else:
                print("{c[0]}={c[1]}".format(c=cfg_item), file=cfg_file)


def diff(user_fname, test_fname, output_type, abs_error):
    if output_type == TestCase.IO_TYPE_TEXT:
        if rcc.cmp.text_cmp(user_fname, test_fname):
            return TestCaseResult.STATUS_CORRECT
        elif rcc.cmp.text_cmp2(user_fname, test_fname):
            return TestCaseResult.STATUS_MALFORMED
        return TestCaseResult.STATUS_INCORRECT
    elif output_type == TestCase.IO_TYPE_NUMERIC:
        if rcc.cmp.number_cmp(user_fname, test_fname, abs_error):
            return TestCaseResult.STATUS_CORRECT
        return TestCaseResult.STATUS_INCORRECT
    elif output_type == TestCase.IO_TYPE_BINARY:
        if filecmp.cmp(user_fname, test_fname, shallow=False):
            return TestCaseResult.STATUS_CORRECT
        return TestCaseResult.STATUS_INCORRECT


def process_test_results(storage_provider, commit, test_case, base_dir):
    logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
    user_out_fname = os.path.join(base_dir, "{}.output".format(test_case.id))
    run_info_fname = os.path.join(base_dir, "{}.monitor_out".format(test_case.id))
    run_info = configparser.ConfigParser(allow_no_value=True)
    with open(run_info_fname) as run_info_file:
        run_info.read_file(it.chain(("[info]",), run_info_file))
    if len(run_info["info"]["signal"]) != 0:
        test_status = TestCaseResult.STATUS_INCORRECT
    else:
        test_out_fname = os.path.join(base_dir, "{}.out".format(test_case.id))
        storage_provider.fetch_test_case_output_file(test_case, test_out_fname)
        if (
            test_case.output_type == TestCase.IO_TYPE_NUMERIC
            and test_case.abs_error is None
        ):
            logger.debug(
                "[{c.id}] ({t.id}) Error margin is not set".format(
                    c=commit, t=test_case
                )
            )
            test_case.abs_error = 0.0
        test_status = diff(
            user_out_fname, test_out_fname, test_case.output_type, test_case.abs_error
        )
    return TestCaseResult(
        commit.id,
        test_case.id,
        run_info["info"]["time"],
        test_status,
        run_info["info"]["signal"],
    )


async def expect_message(outputs, expected, timeout):
    async def next_message():
        message = next(outputs)
        message = message.decode("utf8").strip()
        return message

    message = await asyncio.wait_for(next_message(), timeout)
    if message != expected:
        raise RuntimeError("Expected `{}`, got `{}`".format(expected, message))


async def run(data_provider, commit, test_cases, base_dir, remote_dir):
    logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
    cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)

    client = docker.from_env()
    volumes = {
        remote_dir: {"bind": "/root", "mode": "rw"},
    }
    container = client.containers.run(
        commit.language.image, detach=True, remove=False, volumes=volumes
    )

    container_stdout = container.logs(stream=True)

    if commit.is_compilable:
        try:
            # Compilation start
            await expect_message(
                container_stdout, "compilation.start", cfg.compilation_timeout
            )

            commit.status = Commit.STATUS_COMPILING
            data_provider.update_commit(commit)

            # Compilation done
            await expect_message(
                container_stdout, "compilation.done", cfg.compilation_timeout
            )

            err_fname = os.path.join(base_dir, cfg.compilation_error_file)
            # 'replace' is used because the compiler output may include text from
            # the user-submitted file, which we have no control of
            with open(err_fname, errors="replace") as err_file:
                compiled_error = "".join(err_file.readlines()).strip()
            if compiled_error != "":
                commit.status = Commit.STATUS_ERROR
                commit.compiled_error = compiled_error
                commit.compiled_signal = 1
                commit.is_compiled = False
            else:
                commit.status = Commit.STATUS_COMPILED
                commit.is_compiled = True
        except asyncio.TimeoutError:
            logger.warning("Compilation timed out", exc_info=True)
            raise RuntimeError("Compilation timed out")
    else:
        # NOTE: does not make much sense, but seems to be needed
        commit.is_compiled = True

    commit.compilation_finished_time = datetime.datetime.now()
    data_provider.update_commit(commit)

    if commit.status != Commit.STATUS_ERROR:
        try:
            # Test cases execution start
            base_timeout = cfg.base_exec_timeout * (1 + len(test_cases))
            timeout = base_timeout + sum(c.cpu_time for c in test_cases)
            await expect_message(container_stdout, "run.start", timeout)

            commit.status = Commit.STATUS_RUNNING
            data_provider.update_commit(commit)

            # Test cases execution done
            await expect_message(container_stdout, "run.done", timeout)
        except asyncio.TimeoutError:
            logger.warning("Execution timed out", exc_info=True)
            raise RuntimeError("Execution timed out")
    try:
        container.wait(timeout=cfg.base_exec_timeout)
    except requests.exceptions.ReadTimeout:
        logger.error("Container wait timed out", exc_info=True)
        container.kill()
    finally:
        # Ensure container is removed
        try:
            container.remove(force=True)
        except Exception:
            logger.error("Container removal failed", exc_info=True)


def run_tests(
    data_provider, storage_provider, commit, test_cases, base_dir, remote_dir
):
    loop = asyncio.get_event_loop()
    run_task = run(data_provider, commit, test_cases, base_dir, remote_dir)
    loop.run_until_complete(run_task)
    # loop.close()

    if commit.status == Commit.STATUS_ERROR:
        return []
    return [
        process_test_results(storage_provider, commit, test_case, base_dir)
        for test_case in test_cases
    ]


def prepare_output_file(commit, base_dir):
    def should_truncate(fname):
        return fname.endswith((".output", ".error"))

    def truncate(fname):
        with open(fname, "a") as f:
            size = f.seek(0, 2)
            if size > cfg.max_output_file_size:
                f.seek(0, 0)
                f.truncate(cfg.max_output_file_size)

    cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
    output_dir = os.path.join(base_dir, cfg.output_files_dir)
    output_fname = os.path.join(base_dir, "{}.zip".format(commit.id))
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


def compute_score(commit, test_cases, test_results):
    if commit.status == Commit.STATUS_ERROR:
        commit.corrects = 0
        commit.score = 0
        return

    def is_correct(test_result):
        return test_result.status == TestCaseResult.STATUS_CORRECT

    commit.corrects = rcc.util.count_if(is_correct, test_results)
    # Score starts at 10 and is reduced proportionally to the number of
    # incorrect test cases
    commit.score = 10.0
    if len(test_cases) > 0:
        commit.score *= commit.corrects / len(test_cases)
    if commit.corrects == len(test_cases):
        commit.status = Commit.STATUS_COMPLETED
    else:
        commit.status = Commit.STATUS_INCOMPLETE


def cleanup_tests(base_dir):
    if os.path.isdir(base_dir):

        def rmtree_handler(func, path, exc_info):
            exc_type, exc_value, _ = exc_info
            raise exc_type(exc_value)

        shutil.rmtree(base_dir, onerror=rmtree_handler)


def process_commit(data_provider, commit):
    cfg = rcc.config.get_config(rcc.config.DEFAULT_CONFIG)
    logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
    logger.debug(
        "[{c.id}]"
        " user_email={c.user_email}"
        ", exercise_id={c.exercise_id}"
        ", commit_time={c.commit_time}".format(c=commit)
    )

    try:
        storage_provider = rcc.provider.storage.from_config(cfg)
    except:
        logger.error("[{c.id}] Storage provider error".format(c=commit), exc_info=True)
        commit.status = Commit.STATUS_INTERNAL_ERROR
        data_provider.update_commit(commit)
        return

    try:
        test_cases = data_provider.fetch_test_cases(commit)
    except:
        logger.error(
            "[{c.id}] Failed to fetch test cases".format(c=commit), exc_info=True
        )
        commit.status = Commit.STATUS_INTERNAL_ERROR
        data_provider.update_commit(commit)
        if cfg.cleanup_on_error:
            cleanup_tests(base_dir)
        return

    # Delete results already produced by this commit, if any
    data_provider.delete_commit_test_results(commit)
    commit.reset()
    commit.status = Commit.STATUS_PROCESSING
    commit.compilation_started_time = datetime.datetime.now()
    data_provider.update_commit(commit)

    logger.debug("[{c.id}] Preparing to run tests".format(c=commit))
    try:
        base_dir = os.path.join(cfg.exec_dir, "commit_{}".format(commit.id))
        remote_dir = os.path.join(cfg.exec_dir_remote, "commit_{}".format(commit.id))
        cleanup_tests(base_dir)
        os.makedirs(base_dir, DEFAULT_MKDIR_PERMISSIONS)
        create_container_cfg_file(commit, test_cases, base_dir)
        copy_source_files(data_provider, storage_provider, commit, base_dir)
        copy_test_case_files(storage_provider, test_cases, base_dir)
    except:
        logger.error("[{c.id}] Failed to prepare runs".format(c=commit), exc_info=True)
        commit.status = Commit.STATUS_INTERNAL_ERROR
        data_provider.update_commit(commit)
        if cfg.cleanup_on_error:
            cleanup_tests(base_dir)
        return

    logger.debug("[{c.id}] Running tests".format(c=commit))
    try:
        test_results = run_tests(
            data_provider, storage_provider, commit, test_cases, base_dir, remote_dir
        )
    except:
        logger.error("[{c.id}] Failed to run tests".format(c=commit), exc_info=True)
        commit.status = Commit.STATUS_INTERNAL_ERROR
        data_provider.update_commit(commit)
        if cfg.cleanup_on_error:
            cleanup_tests(base_dir)
        return
    logger.debug("[{c.id}] Done testing".format(c=commit))

    logger.debug("[{c.id}] Storing results".format(c=commit))
    try:
        compute_score(commit, test_cases, test_results)
        data_provider.update_commit(commit)
        data_provider.store_commit_test_results(commit, test_results)
        if len(test_results) > 0:
            output_fname = prepare_output_file(commit, base_dir)
            storage_provider.store_commit_output(commit, output_fname)
        cleanup_tests(base_dir)
    except:
        logger.error(
            "[{c.id}] Could not save results, commit data might be"
            " inconsistent".format(c=commit),
            exc_info=True,
        )
        commit.status = Commit.STATUS_INTERNAL_ERROR
        data_provider.update_commit(commit)
        if cfg.cleanup_on_error:
            cleanup_tests(base_dir)
        return
    logger.debug("[{c.id}] Commit processing done".format(c=commit))


def process_commits(data_provider, commit_queue):
    logger = logging.getLogger(rcc.config.DEFAULT_LOGGER)
    logger.debug("Worker started")
    with rcc.util.UninterruptibleContext():
        try:
            while True:
                commit = commit_queue.get()
                if commit is None:
                    # Hint to stop processing, mark empty task as done and bail
                    commit_queue.task_done()
                    break
                process_commit(data_provider, commit)
                commit_queue.task_done()
        except:
            logger.error("Uncaught exception", exc_info=True)
    logger.debug("Worker stopped")
