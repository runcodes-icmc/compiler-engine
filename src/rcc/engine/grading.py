"""Grading: compare outputs, collect results, compute scores."""

import configparser
import filecmp
import itertools as it
import logging
import os
from typing import TYPE_CHECKING

from ..cmp import number_cmp, text_cmp, text_cmp2
from ..config import DEFAULT_LOGGER
from ..model import Commit, TestCase, TestCaseResult
from ..util import count_if

if TYPE_CHECKING:
    from ..provider.storage import StorageProvider


def diff(
    user_fname: str, test_fname: str, output_type: int, abs_error: float | None
) -> int:
    if output_type == TestCase.IO_TYPE_TEXT:
        if text_cmp(user_fname, test_fname):
            return TestCaseResult.STATUS_CORRECT
        elif text_cmp2(user_fname, test_fname):
            return TestCaseResult.STATUS_MALFORMED
        return TestCaseResult.STATUS_INCORRECT
    elif output_type == TestCase.IO_TYPE_NUMERIC:
        if number_cmp(user_fname, test_fname, abs_error or 0.0):
            return TestCaseResult.STATUS_CORRECT
        return TestCaseResult.STATUS_INCORRECT
    elif output_type == TestCase.IO_TYPE_BINARY:
        if filecmp.cmp(user_fname, test_fname, shallow=False):
            return TestCaseResult.STATUS_CORRECT
        return TestCaseResult.STATUS_INCORRECT
    raise ValueError(f"Unknown test case output type: {output_type}")


def process_test_results(
    storage_provider: StorageProvider,
    commit: Commit,
    test_case: TestCase,
    base_dir: str,
) -> TestCaseResult:
    logger = logging.getLogger(DEFAULT_LOGGER)
    user_out_fname = os.path.join(base_dir, f"{test_case.id}.output")
    user_err_fname = os.path.join(base_dir, f"{test_case.id}.error")
    run_info_fname = os.path.join(base_dir, f"{test_case.id}.monitor_out")
    run_info = configparser.ConfigParser(allow_no_value=True)
    with open(run_info_fname) as run_info_file:
        run_info.read_file(it.chain(("[info]",), run_info_file))
    user_err_stat = os.stat(user_err_fname)
    if len(run_info["info"]["signal"]) != 0 or user_err_stat.st_size != 0:
        test_status = TestCaseResult.STATUS_INCORRECT
    else:
        test_out_fname = os.path.join(base_dir, f"{test_case.id}.out")
        storage_provider.fetch_test_case_output_file(test_case, test_out_fname)
        if (
            test_case.output_type == TestCase.IO_TYPE_NUMERIC
            and test_case.abs_error is None
        ):
            logger.debug(f"[{commit.id}] ({test_case.id}) Error margin is not set")
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


def process_test_results_batch(
    storage_provider: StorageProvider,
    commit: Commit,
    test_cases: list[TestCase],
    base_dir: str,
) -> list[TestCaseResult]:
    """Run :func:`process_test_results` for every test case (sync helper).

    Called through ``asyncio.to_thread``: the per-case S3 downloads inside
    :func:`process_test_results` are blocking calls.
    """
    return [
        process_test_results(storage_provider, commit, test_case, base_dir)
        for test_case in test_cases
    ]


def compute_score(
    commit: Commit, test_cases: list[TestCase], test_results: list[TestCaseResult]
) -> None:
    if commit.status == Commit.STATUS_ERROR:
        commit.corrects = 0
        commit.score = 0
        return

    def is_correct(test_result: TestCaseResult) -> bool:
        return test_result.status == TestCaseResult.STATUS_CORRECT

    commit.corrects = count_if(is_correct, test_results)
    # Score starts at 10 and is reduced proportionally to the number of
    # incorrect test cases
    commit.score = 10.0
    if len(test_cases) > 0:
        commit.score *= commit.corrects / len(test_cases)
    if commit.corrects == len(test_cases):
        commit.status = Commit.STATUS_COMPLETED
    else:
        commit.status = Commit.STATUS_INCOMPLETE
