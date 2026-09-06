import asyncio
import configparser
import datetime
import filecmp
import itertools as it
import logging
import os
import shutil
import sys
import threading
import zipfile
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

import docker
import requests

from .cmp import number_cmp, text_cmp, text_cmp2
from .config import (
    DEFAULT_CONCURRENCY_PER_WORKER,
    DEFAULT_CONFIG,
    DEFAULT_LOGGER,
    Config,
    from_dict,
    get_config,
)
from .languages import language_from_extension
from .model import Commit, TestCase, TestCaseResult
from .provider import storage
from .util import (
    count_if,
    deduce_language,
    is_compilable,
    standardize_extension,
)

if TYPE_CHECKING:
    from docker.models.containers import Container

    from .provider.data import DataProvider
    from .provider.storage import StorageProvider

DEFAULT_MKDIR_PERMISSIONS = 0o777

# How long a consumer's pull loop waits on an empty task queue before
# checking whether a non-retryable failure in an in-flight commit must stop
# the consumer.
QUEUE_GET_POLL_TIMEOUT = 1.0

# How long run() waits for the container log reader thread to terminate after
# the container is gone. The thread is a daemon, so this only bounds the wait;
# it never blocks process exit.
CONTAINER_LOG_READER_JOIN_TIMEOUT = 5.0

# Upper bound for the number of concurrent S3 downloads in the prefetch phase.
# The actual bound mirrors ``concurrency_per_worker`` (the number of commits a
# worker processes at once), capped here so a single commit with many
# exercise/test-case files cannot open an unbounded number of connections.
PREFETCH_MAX_CONCURRENT_DOWNLOADS = 8

# Exceptions that stop a consumer instead of failing just one commit.
NON_RETRYABLE_EXCEPTIONS = (MemoryError, OSError, SystemExit, SystemError)


def _get_config() -> Config:
    """Return the registered default configuration, raising if none exists."""
    cfg = get_config(DEFAULT_CONFIG)
    if cfg is None:
        raise RuntimeError("No default configuration registered")
    return cfg


def set_extension(commit: Commit) -> None:
    if commit.fname is None:
        raise ValueError("Commit has no filename; cannot deduce its extension")
    _, extension = os.path.splitext(commit.fname)
    commit.extension = standardize_extension(extension[1:])


def _raise_first_error(results: Iterable[BaseException | None]) -> None:
    """Re-raise the first exception in a ``gather(return_exceptions=True)`` list.

    Used after each concurrent download batch: every in-flight download has
    finished by the time this is called, so the caller's cleanup (which may
    delete ``base_dir``) can never race a worker thread still writing into it.
    """
    for result in results:
        if isinstance(result, BaseException):
            raise result


def _mark_task_done(task: asyncio.Task[None]) -> None:
    """Retrieve a finished task's outcome (suppress 'never retrieved' warnings).

    Used on background tasks whose result may never be awaited on some paths
    (e.g. when the enclosing coroutine is cancelled mid-prefetch). Awaiting
    the task later still yields the same result/exception.
    """
    if not task.cancelled():
        _ = task.exception()


async def _await_task(task: asyncio.Task[None]) -> Exception | None:
    """Await ``task`` and return its exception, or ``None`` on success.

    Non-Exception BaseExceptions (e.g. CancelledError) propagate, so a
    cancellation is never mistaken for a failed download.
    """
    try:
        await task
    except Exception as error:  # noqa: BLE001
        return error
    return None


async def download_commit_file(
    storage_provider: StorageProvider, commit: Commit, base_dir: str
) -> None:
    """Download the commit source file into ``<base_dir>/<cfg.src_dir>``.

    Split out of :func:`copy_source_files` so the download can start
    concurrently with the prefetch database queries in :func:`process_commit`.
    """
    if commit.fname is None:
        raise ValueError("Commit has no filename; cannot download its source file")
    cfg = _get_config()
    src_dir = os.path.join(base_dir, str(cfg.src_dir))
    os.makedirs(src_dir, DEFAULT_MKDIR_PERMISSIONS)
    destination = os.path.join(src_dir, os.path.basename(commit.fname))
    # boto3 download runs in a worker thread
    await asyncio.to_thread(storage_provider.fetch_commit_file, commit, destination)


async def copy_source_files(
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
    cfg = _get_config()
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
    _raise_first_error(results)


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
    _raise_first_error(results)


def create_container_cfg_file(
    commit: Commit, test_cases: list[TestCase], base_dir: str
) -> None:
    cfg = _get_config()
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


class ContainerLogReader:
    """Bridge between docker-py's blocking log generator and asyncio.

    ``container.logs(stream=True)`` returns a blocking generator: iterating it
    on the event loop would stall the whole worker for the container's
    lifetime. Instead, a daemon reader thread consumes the generator and
    pushes every decoded line into an :class:`asyncio.Queue`. Because
    ``asyncio.Queue`` is not thread-safe, pushes go through
    ``loop.call_soon_threadsafe``, which is the documented way to enqueue from
    another thread.

    When the generator is exhausted — the container stopped or its API
    connection closed — the ``END`` sentinel is pushed, so consumers can
    distinguish "end of stream" from "no data yet". The thread terminates by
    itself when the generator ends; if a container hangs forever the thread
    stays blocked in the generator, but it is a daemon thread and the number
    of stuck readers is bounded by the worker's per-commit concurrency limit,
    so it can never block process exit.

    One queue item corresponds to exactly one chunk yielded by the generator
    (which the docker API delivers line by line), preserving the semantics of
    the previous synchronous ``next(outputs)`` reads.
    """

    # Sentinel pushed once the log stream ends.
    END: object = object()

    _generator: Iterable[bytes | str]
    _loop: asyncio.AbstractEventLoop
    _queue: asyncio.Queue[object]
    _thread: threading.Thread

    def __init__(
        self, logs_generator: Iterable[bytes | str], loop: asyncio.AbstractEventLoop
    ) -> None:
        self._generator = logs_generator
        self._loop = loop
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(
            target=self._read_loop,
            name="container-log-reader",
            daemon=True,
        )

    def start(self) -> None:
        """Start the reader thread."""
        self._thread.start()

    def stop(self) -> None:
        """Best-effort wait for the reader thread to terminate.

        The thread ends on its own once the generator is exhausted (which
        happens when the container exits or its API connection closes); this
        only bounds how long we are willing to wait for it.
        """
        if self._thread.is_alive():
            self._thread.join(timeout=CONTAINER_LOG_READER_JOIN_TIMEOUT)

    async def get(self) -> object:
        """Return the next decoded log line, or ``END`` when the stream ended."""
        return await self._queue.get()

    def _push(self, item: object) -> bool:
        """Schedule an item for the loop thread; False if the loop is closing."""
        try:
            _ = self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
            return True
        except RuntimeError:
            # Event loop is closed (worker shutting down): stop reading.
            return False

    def _read_loop(self) -> None:
        try:
            for raw in self._generator:
                line = raw.decode("utf8") if isinstance(raw, bytes) else raw
                if not self._push(line.strip()):
                    break
        except Exception as e:  # noqa: BLE001
            # The generator raised (connection dropped, decode error, ...):
            # treat the stream as ended.
            logger = logging.getLogger(DEFAULT_LOGGER)
            logger.debug("Container log stream terminated: %s", e)
        finally:
            _ = self._push(self.END)


async def expect_message(
    log_reader: ContainerLogReader, expected: str, timeout: float
) -> None:
    """Wait for the next line of the container's log stream to be ``expected``.

    Reads decoded, stripped lines from the :class:`ContainerLogReader` queue.
    Raises ``TimeoutError`` when nothing arrives within ``timeout`` seconds,
    ``RuntimeError`` when the stream ends early, and ``RuntimeError`` when the
    stream delivers an unexpected line.
    """
    message = await asyncio.wait_for(log_reader.get(), timeout)
    if message is ContainerLogReader.END:
        raise RuntimeError(f"Container log stream ended before receiving `{expected}`")
    if message != expected:
        raise RuntimeError(f"Expected `{expected}`, got `{message}`")


def _read_compilation_error_file(fname: str) -> str:
    """Read the compiler's error output file, tolerating undecodable bytes.

    'replace' is used because the compiler output may include text from the
    user-submitted file, which we have no control of.
    """
    with open(fname, errors="replace") as err_file:
        return "".join(err_file.readlines()).strip()


async def run(
    data_provider: DataProvider,
    commit: Commit,
    test_cases: list[TestCase],
    base_dir: str,
    remote_dir: str,
) -> None:
    """Run the submitted code in a container, streaming its log messages.

    Every blocking docker SDK call (client creation, container start, the log
    stream, ``wait``/``kill``/``remove``) is offloaded to a worker thread so
    the event loop stays free to process other commits while the container
    runs. Container logs are pumped into the loop by a dedicated reader thread
    (see :class:`ContainerLogReader`) and awaited via :func:`expect_message`.

    A fresh docker client is created per commit: each concurrent task gets its
    own client, which also sidesteps docker-py thread-safety questions.
    """
    cfg = _get_config()

    client = await asyncio.to_thread(docker.from_env)
    volumes: dict[str, dict[str, str]] = {
        remote_dir: {"bind": "/root", "mode": "rw"},
    }
    language = commit.language
    if language is None:
        raise RuntimeError("Commit has no language; cannot start its container")
    container = await asyncio.to_thread(
        client.containers.run,
        language.image,
        detach=True,
        remove=False,
        volumes=volumes,
    )

    log_reader = ContainerLogReader(
        cast(
            Iterable[bytes],
            await asyncio.to_thread(container.logs, stream=True),
        ),
        asyncio.get_running_loop(),
    )
    log_reader.start()

    try:
        await _run_compilation(data_provider, commit, cfg, base_dir, log_reader)

        commit.compilation_finished_time = datetime.datetime.now(tz=datetime.UTC)
        await data_provider.update_commit(commit)

        if commit.status != Commit.STATUS_ERROR:
            await _run_execution(data_provider, commit, test_cases, cfg, log_reader)

        await _teardown_container(container, cfg)
    finally:
        log_reader.stop()


async def _run_compilation(
    data_provider: DataProvider,
    commit: Commit,
    cfg: Config,
    base_dir: str,
    log_reader: ContainerLogReader,
) -> None:
    """Drive the container's compilation phase via its log stream.

    Raises ``RuntimeError`` when the container does not report the
    ``compilation.done`` message within ``compilation_timeout``.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)
    if not commit.is_compilable:
        # NOTE: does not make much sense, but seems to be needed
        commit.is_compiled = True
        return

    try:
        await expect_message(
            log_reader, "compilation.start", cast(float, cfg.compilation_timeout)
        )
        commit.status = Commit.STATUS_COMPILING
        await data_provider.update_commit(commit)

        await expect_message(
            log_reader, "compilation.done", cast(float, cfg.compilation_timeout)
        )

        err_fname = os.path.join(base_dir, str(cfg.compilation_error_file))
        compiled_error = await asyncio.to_thread(
            _read_compilation_error_file, err_fname
        )
        if compiled_error != "":
            commit.status = Commit.STATUS_ERROR
            commit.compiled_error = compiled_error
            commit.compiled_signal = 1
            commit.is_compiled = False
        else:
            commit.status = Commit.STATUS_COMPILED
            commit.is_compiled = True
    except TimeoutError:
        logger.warning("Compilation timed out", exc_info=True)
        raise RuntimeError("Compilation timed out")


async def _run_execution(
    data_provider: DataProvider,
    commit: Commit,
    test_cases: list[TestCase],
    cfg: Config,
    log_reader: ContainerLogReader,
) -> None:
    """Drive the container's test execution phase via its log stream.

    Raises ``RuntimeError`` when the container does not report the
    ``run.done`` message within the test cases' time budget.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)
    try:
        base_timeout = cast(float, cfg.base_exec_timeout) * (1 + len(test_cases))
        timeout = base_timeout + sum(c.cpu_time for c in test_cases)
        await expect_message(log_reader, "run.start", timeout)

        commit.status = Commit.STATUS_RUNNING
        await data_provider.update_commit(commit)

        await expect_message(log_reader, "run.done", timeout)
    except TimeoutError:
        logger.warning("Execution timed out", exc_info=True)
        raise RuntimeError("Execution timed out")


async def _teardown_container(container: Container, cfg: Config) -> None:
    """Wait for the container, killing it on timeout, then remove it."""
    logger = logging.getLogger(DEFAULT_LOGGER)
    try:
        _ = await asyncio.to_thread(
            container.wait, timeout=cast(float, cfg.base_exec_timeout)
        )
    except requests.exceptions.ReadTimeout:
        logger.exception("Container wait timed out")
        await asyncio.to_thread(container.kill)
    finally:
        try:
            await asyncio.to_thread(container.remove, force=True)
        except Exception:
            logger.exception("Container removal failed")


async def run_tests(
    data_provider: DataProvider,
    storage_provider: StorageProvider,
    commit: Commit,
    test_cases: list[TestCase],
    base_dir: str,
    remote_dir: str,
) -> list[TestCaseResult]:
    """Run the submitted code in a container and collect the test results.

    Runs on the caller's event loop so commit status updates can be pushed to
    the (async) data provider.
    """
    await run(data_provider, commit, test_cases, base_dir, remote_dir)

    if commit.status == Commit.STATUS_ERROR:
        return []
    # `process_test_results` is synchronous and downloads the expected output
    # of every test case from S3: run the whole batch in a worker thread so
    # the boto3 calls do not block the event loop.
    return await asyncio.to_thread(
        process_test_results_batch, storage_provider, commit, test_cases, base_dir
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


def prepare_output_file(commit: Commit, base_dir: str) -> str:
    cfg = _get_config()

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


def cleanup_tests(base_dir: str) -> None:
    if os.path.isdir(base_dir):
        # Errors propagate: no error handler is passed, so rmtree() raises on
        # the first failure. The previous onexc handler re-raised the same
        # exception, which is equivalent but crashed on Python 3.12+ where
        # the handler receives the exception itself, not a 3-tuple.
        shutil.rmtree(base_dir)


async def process_commit(
    data_provider: DataProvider, commit: Commit, cfg: Config | None = None
) -> None:
    """Process a single commit: compile it, run its test cases, store results.

    Fully async: every interaction with the data provider is awaited. Runs on
    the caller's event loop (a consumer's main loop or a test runner).

    The work is split into phases, one helper per phase; a helper returns
    ``False`` (or ``None``) when it failed the commit (logged and marked
    INTERNAL_ERROR), which ends processing early.
    """
    if cfg is None:
        cfg = get_config(DEFAULT_CONFIG)
    if cfg is None:
        raise RuntimeError("No default configuration registered")
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
            data_provider, storage_provider, commit, test_cases, base_dir, remote_dir
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
        download_commit_file(storage_provider, commit, base_dir)
    )
    # Every path below awaits (or cancels) this task; the done callback only
    # guarantees the outcome is retrieved on the paths that never do (e.g.
    # this coroutine cancelled mid-prefetch), avoiding 'exception was never
    # retrieved' warnings.
    download_task.add_done_callback(_mark_task_done)

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
            download_error = await _await_task(download_task)
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
        _ = await _await_task(download_task)
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
    # Bound for the S3 downloads: mirror the per-consumer commit concurrency
    # so a consumer never opens more simultaneous downloads than it has
    # in-flight commits, capped at a sane small maximum (and never zero,
    # which would deadlock every download).
    concurrency = int(
        str(cfg.get("concurrency_per_worker", DEFAULT_CONCURRENCY_PER_WORKER))
    )
    download_semaphore = asyncio.Semaphore(
        max(1, min(concurrency, PREFETCH_MAX_CONCURRENT_DOWNLOADS))
    )
    try:
        commit_file_error = await _await_task(download_task)
        if commit_file_error is not None:
            raise commit_file_error
        create_container_cfg_file(commit, test_cases, base_dir)
        await copy_source_files(
            data_provider, storage_provider, commit, base_dir, download_semaphore
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
            output_fname = prepare_output_file(commit, base_dir)
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


async def _claim_and_run(
    data_provider: DataProvider,
    commit: Commit,
    cfg: Config | None,
    semaphore: asyncio.Semaphore,
    fatal: asyncio.Event,
    logger: logging.Logger,
) -> None:
    """Claim ``commit`` and process it, releasing the slot afterwards.

    The poller can enqueue the same commit more than once (a commit stays
    STATUS_IN_QUEUE until a consumer takes it, e.g. while it waits in the
    bounded task queue), so several consumers may pull copies of it. The
    claim is a conditional UPDATE (IN_QUEUE -> PROCESSING) in the provider,
    which is the only state shared across consumers: only the consumer whose
    update wins processes the commit; the losers drop their copies.
    """
    try:
        try:
            claimed = await data_provider.claim_commit(commit)
        except Exception as e:
            # Claim failed (e.g. a database hiccup): the commit stays
            # IN_QUEUE and the poller re-enqueues it later.
            logger.warning(f"Caught retryable exception: {e}", exc_info=True)
            return

        if not claimed:
            logger.debug(f"Commit {commit.id} already taken; skipping")
            return

        try:
            await process_commit(data_provider, commit, cfg)
        except NON_RETRYABLE_EXCEPTIONS as e:
            logger.warning(f"Caught non-retryable exception: {e}")
            fatal.set()
        except Exception as e:
            logger.warning(f"Caught retryable exception: {e}", exc_info=True)
            # We still hold the claim: give the commit back so the poller
            # can retry it.
            try:
                await data_provider.release_commit(commit)
            except Exception:
                logger.warning(
                    f"Could not release commit {commit.id} back to the queue",
                    exc_info=True,
                )
    finally:
        semaphore.release()


async def process_commits(
    data_provider: DataProvider,
    commit_queue: asyncio.Queue[Commit | None],
    cfg: Config | None = None,
    manage_pool: bool = True,
) -> None:
    """Consumer main loop: pull commits from the queue and process them.

    Producer/consumer structure: a single loop pulls commits from the queue
    and spawns one task per commit, claimed and processed by
    :func:`_claim_and_run`. An ``asyncio.Semaphore`` sized
    ``concurrency_per_worker`` bounds the number of commits in flight inside
    this consumer; the slot is acquired before the task is spawned and
    released in the task's ``finally`` block, so a failing commit can never
    leak a slot.

    ``queue.get`` waits with a bounded timeout so the loop can notice
    failures of in-flight tasks. When the ``None`` stop hint arrives the loop
    stops pulling and drains every in-flight commit before exiting.
    Non-retryable exceptions stop the consumer (after the in-flight commits
    finish); retryable ones are logged and skipped.

    The database connection pool is opened here when ``manage_pool`` is set
    (the default) and closed when the consumer stops. Callers that share one
    provider across several consumers (``rcc.main``) pass ``manage_pool=False``
    and own the pool lifecycle themselves.
    """
    logger = logging.getLogger(DEFAULT_LOGGER)

    # In production the main process's handlers are inherited; add handlers
    # only when running standalone (e.g. tests).
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler(sys.stderr)
        console_fmt = logging.Formatter(
            "[%(asctime)s] %(module)s:%(lineno)d: <%(taskName)s> %(message)s",
            defaults={"taskName": "-"},
        )
        console_handler.setFormatter(console_fmt)
        logger.addHandler(console_handler)

    logger.debug("Consumer started")

    if cfg is not None:
        # Helper functions in this module read the global registry
        # (_get_config), so make sure the config in use is the registered
        # one (it already is when main() built it).
        if get_config(DEFAULT_CONFIG) is not cfg:
            _ = from_dict(DEFAULT_CONFIG, cfg.get_dict())
    else:
        cfg = get_config(DEFAULT_CONFIG)

    if cfg is None:
        # No configuration was passed and none is registered: fall back to
        # the default concurrency (process_commit() would fail on the missing
        # configuration anyway).
        concurrency = DEFAULT_CONCURRENCY_PER_WORKER
    else:
        concurrency = int(
            str(cfg.get("concurrency_per_worker", DEFAULT_CONCURRENCY_PER_WORKER))
        )

    if manage_pool:
        try:
            await data_provider.open()
        except Exception:
            logger.exception("Failed to open database connection pool")
            raise

    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    fatal = asyncio.Event()

    try:
        while True:
            if fatal.is_set():
                break

            try:
                # Bounded wait: lets the loop observe `fatal` (set by an
                # in-flight task) instead of blocking on an empty queue.
                commit = await asyncio.wait_for(
                    commit_queue.get(), timeout=QUEUE_GET_POLL_TIMEOUT
                )
            except TimeoutError:
                continue
            except NON_RETRYABLE_EXCEPTIONS as e:
                logger.warning(f"Caught non-retryable exception: {e}")
                break
            except Exception as e:
                logger.warning(f"Caught retryable exception: {e}", exc_info=True)
                continue

            try:
                if commit is None:
                    break

                _ = await semaphore.acquire()
                try:
                    task = asyncio.create_task(
                        _claim_and_run(
                            data_provider, commit, cfg, semaphore, fatal, logger
                        )
                    )
                except BaseException:
                    # Spawning failed: give the slot back immediately.
                    semaphore.release()
                    raise

                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            except NON_RETRYABLE_EXCEPTIONS as e:
                logger.warning(f"Caught non-retryable exception: {e}")
                break
            except Exception as e:
                logger.warning(f"Caught retryable exception: {e}", exc_info=True)

        if in_flight:
            _ = await asyncio.gather(*in_flight)
    finally:
        if manage_pool:
            await data_provider.close()

    logger.debug("Consumer stopped")
