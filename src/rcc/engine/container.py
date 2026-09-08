"""Container execution: run the submitted code in docker and drive it."""

import asyncio
import datetime
import logging
import os
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

import docker
import requests

from ..config import DEFAULT_LOGGER
from ..model import Commit, TestCase, TestCaseResult
from .grading import process_test_results_batch

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Config
    from ..provider.data import DataProvider
    from ..provider.storage import StorageProvider

# How long run() waits for the container log reader thread to terminate after
# the container is gone. The thread is a daemon, so this only bounds the wait;
# it never blocks process exit.
CONTAINER_LOG_READER_JOIN_TIMEOUT = 5.0


class ContainerLogReader:
    """Bridge between docker-py's blocking log generator and asyncio.

    ``container.logs(stream=True)`` returns a blocking generator: iterating it
    on the event loop would stall the whole consumer for the container's
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
    of stuck readers is bounded by the consumer's concurrency limit, so it
    can never block process exit.

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
            # Event loop is closed (consumer shutting down): stop reading.
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
    cfg: Config,
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
    cfg: Config,
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
    await run(cfg, data_provider, commit, test_cases, base_dir, remote_dir)

    if commit.status == Commit.STATUS_ERROR:
        return []
    # `process_test_results` is synchronous and downloads the expected output
    # of every test case from S3: run the whole batch in a worker thread so
    # the boto3 calls do not block the event loop.
    return await asyncio.to_thread(
        process_test_results_batch, storage_provider, commit, test_cases, base_dir
    )
