"""The per-commit pipeline: prepare, compile/run, grade, store."""

import asyncio
import datetime
import logging
import os
from typing import TYPE_CHECKING, cast

from ..config import DEFAULT_LOGGER, get_concurrency
from ..model import Commit, TestCase, TestCaseResult
from ..provider import storage
from .common import DEFAULT_MKDIR_PERMISSIONS, await_task_result, mark_task_done
from .container import run_tests
from .grading import compute_score
from .workspace import (
    cleanup_tests,
    copy_source_files,
    copy_test_case_files,
    create_container_cfg_file,
    download_commit_file,
    prepare_output_file,
)

if TYPE_CHECKING:
    from ..config import Config
    from ..provider.data import DataProvider
    from ..provider.storage import StorageProvider

# Upper bound for the number of concurrent S3 downloads in the prefetch phase.
# The actual bound mirrors the configured in-flight concurrency, capped here so
# a single commit with many exercise/test-case files cannot open an unbounded
# number of connections.
PREFETCH_MAX_CONCURRENT_DOWNLOADS = 8


async def process_commit(
    data_provider: DataProvider, commit: Commit, cfg: Config
) -> None:
    """Process a single commit: compile it, run its test cases, store results.

    Fully async: every interaction with the data provider is awaited. Runs on
    the caller's event loop (a consumer's main loop or a test runner).

    The work is split into phases, one helper per phase; a helper returns
    ``False`` (or ``None``) when it failed the commit (logged and marked
    INTERNAL_ERROR), which ends processing early.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)
    logger.debug(
        f"[{commit.id}] user_email={commit.user_email}, exercise_id={commit.exercise_id}, commit_time={commit.commit_time}"
    )

    try:
        storage_provider = storage.from_config(cfg)
    except Exception:
        logger.exception(f"[{commit.id}] Storage provider error")
        commit.status = Commit.STATUS_INTERNAL_ERROR
        await data_provider.update_commit(commit)
        return

    base_dir = os.path.join(cast(str, cfg.exec_dir), f"commit_{commit.id}")
    remote_dir = os.path.join(cast(str, cfg.exec_dir_remote), f"commit_{commit.id}")

    if not await _prepare_work_dir(data_provider, commit, cfg, base_dir):
        return

    prefetched = await _prefetch_commit_data(
        data_provider, storage_provider, commit, cfg, base_dir
    )
    if prefetched is None:
        return
    test_cases, download_task = prefetched

    if not await _prepare_run_files(
        data_provider,
        storage_provider,
        commit,
        test_cases,
        cfg,
        base_dir,
        download_task,
    ):
        return

    logger.debug(f"[{commit.id}] Running tests")
    try:
        test_results = await run_tests(
            cfg,
            data_provider,
            storage_provider,
            commit,
            test_cases,
            base_dir,
            remote_dir,
        )
    except Exception:  # noqa: BLE001
        await _fail_commit(data_provider, commit, cfg, base_dir, "Failed to run tests")
        return
    logger.debug(f"[{commit.id}] Done testing")

    logger.debug(f"[{commit.id}] Storing results")
    if not await _store_results(
        data_provider,
        storage_provider,
        commit,
        test_cases,
        test_results,
        cfg,
        base_dir,
    ):
        return
    logger.debug(f"[{commit.id}] Commit processing done")


async def _fail_commit(
    data_provider: DataProvider,
    commit: Commit,
    cfg: Config,
    base_dir: str,
    message: str,
) -> None:
    """Log ``message``, mark the commit INTERNAL_ERROR and drop its work dir.

    The directory is only removed when ``cleanup_on_error`` is configured, so
    a failure can be inspected afterwards by default.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)
    logger.exception(f"[{commit.id}] {message}")
    commit.status = Commit.STATUS_INTERNAL_ERROR
    await data_provider.update_commit(commit)
    if bool(cfg.cleanup_on_error):
        cleanup_tests(base_dir)


async def _prepare_work_dir(
    data_provider: DataProvider,
    commit: Commit,
    cfg: Config,
    base_dir: str,
) -> bool:
    """Recreate the work directory; ``False`` when the commit was failed.

    The cleanup runs BEFORE any prefetch download starts, so it can never
    delete a file the prefetch just wrote.
    """
    try:
        cleanup_tests(base_dir)
        os.makedirs(base_dir, DEFAULT_MKDIR_PERMISSIONS)
    except Exception:  # noqa: BLE001
        commit.reset()
        await _fail_commit(
            data_provider, commit, cfg, base_dir, "Failed to prepare runs"
        )
        return False
    return True


async def _prefetch_commit_data(
    data_provider: DataProvider,
    storage_provider: StorageProvider,
    commit: Commit,
    cfg: Config,
    base_dir: str,
) -> tuple[list[TestCase], asyncio.Task[None]] | None:
    """Prefetch everything the container phase needs, overlapping the IO.

    Three independent operations start together: fetching the test cases (DB
    read), deleting stale results (DB write) and downloading the commit source
    file (S3, in a worker thread).

    Ordering guarantees:
      * the DB pair is awaited first and commit.reset()/STATUS_PROCESSING is
        written as soon as it completes: the poller re-enqueues every commit
        it still sees as STATUS_IN_QUEUE, so a slow download would otherwise
        leave the commit queued for the whole download time and several
        consumers would process copies of it (colliding on the same base_dir);
      * delete_commit_test_results still finishes before the STATUS_PROCESSING
        update: a crash between the two must not leave a commit marked
        PROCESSING with stale results;
      * a download failure is handed back through the task for the caller's
        "prepare runs" error path.

    Returns the fetched test cases and the in-flight download task, or
    ``None`` when the commit was marked INTERNAL_ERROR and must not be
    processed further.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)

    download_task = asyncio.create_task(
        download_commit_file(cfg, storage_provider, commit, base_dir)
    )
    # Every path below awaits (or cancels) this task; the done callback only
    # guarantees the outcome is retrieved on the paths that never do (e.g.
    # this coroutine cancelled mid-prefetch), avoiding 'exception was never
    # retrieved' warnings.
    download_task.add_done_callback(mark_task_done)

    test_cases, delete_error = await asyncio.gather(
        data_provider.fetch_test_cases(commit),
        data_provider.delete_commit_test_results(commit),
        return_exceptions=True,
    )

    if isinstance(test_cases, BaseException):
        if isinstance(test_cases, Exception):
            # The delete and the download ran concurrently with the failed
            # fetch; wait for the download (so the cleanup below cannot race
            # its worker thread) and log any failure, so nothing is silently
            # swallowed.
            download_error = await await_task_result(download_task)
            for step_error in (delete_error, download_error):
                if isinstance(step_error, Exception):
                    logger.error(
                        f"[{commit.id}] Concurrent prefetch step failed",
                        exc_info=step_error,
                    )
            logger.error(
                f"[{commit.id}] Failed to fetch test cases",
                exc_info=test_cases,
            )
            commit.status = Commit.STATUS_INTERNAL_ERROR
            await data_provider.update_commit(commit)
            if bool(cfg.cleanup_on_error):
                cleanup_tests(base_dir)
            return None
        # A non-Exception BaseException (e.g. CancelledError) must never be
        # treated as a provider failure: propagate it unchanged.
        _ = download_task.cancel()
        raise test_cases

    if delete_error is not None:
        # Wait for the in-flight download so a later retry's cleanup cannot
        # race its worker thread, then propagate exactly like the sequential
        # version (the commit stays in the queue and is retried).
        _ = await await_task_result(download_task)
        raise delete_error

    commit.reset()
    commit.status = Commit.STATUS_PROCESSING
    commit.compilation_started_time = datetime.datetime.now(tz=datetime.UTC)
    await data_provider.update_commit(commit)

    return test_cases, download_task


async def _prepare_run_files(
    data_provider: DataProvider,
    storage_provider: StorageProvider,
    commit: Commit,
    test_cases: list[TestCase],
    cfg: Config,
    base_dir: str,
    download_task: asyncio.Task[None],
) -> bool:
    """Finish the prefetched download and fetch the remaining input files.

    The download failure surfaces here so it goes through the same
    "Failed to prepare runs" error path as the other preparation steps.
    Returns ``False`` when the commit was failed.
    """
    # Bound for the S3 downloads: mirror the in-flight commit concurrency so
    # a consumer never opens more simultaneous downloads than it has in-flight
    # commits, capped at a sane small maximum (and never zero, which would
    # deadlock every download).
    download_semaphore = asyncio.Semaphore(
        max(1, min(get_concurrency(cfg), PREFETCH_MAX_CONCURRENT_DOWNLOADS))
    )
    try:
        commit_file_error = await await_task_result(download_task)
        if commit_file_error is not None:
            raise commit_file_error
        create_container_cfg_file(cfg, commit, test_cases, base_dir)
        await copy_source_files(
            cfg, data_provider, storage_provider, commit, base_dir, download_semaphore
        )
        await copy_test_case_files(
            storage_provider, test_cases, base_dir, download_semaphore
        )
    except Exception:  # noqa: BLE001
        await _fail_commit(
            data_provider, commit, cfg, base_dir, "Failed to prepare runs"
        )
        return False
    return True


async def _store_results(
    data_provider: DataProvider,
    storage_provider: StorageProvider,
    commit: Commit,
    test_cases: list[TestCase],
    test_results: list[TestCaseResult],
    cfg: Config,
    base_dir: str,
) -> bool:
    """Score the commit, persist its results and upload the output zip.

    Returns ``False`` when the commit was failed.
    """
    try:
        compute_score(commit, test_cases, test_results)
        await data_provider.update_commit(commit)
        await data_provider.store_commit_test_results(commit, test_results)
        if len(test_results) > 0:
            output_fname = prepare_output_file(cfg, commit, base_dir)
            # boto3 upload runs in a worker thread
            await asyncio.to_thread(
                storage_provider.store_commit_output, commit, output_fname
            )
        cleanup_tests(base_dir)
    except Exception:  # noqa: BLE001
        await _fail_commit(
            data_provider,
            commit,
            cfg,
            base_dir,
            "Could not save results, commit data might be inconsistent",
        )
        return False
    return True
