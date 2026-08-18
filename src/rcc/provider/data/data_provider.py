from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...model import Commit, TestCase, TestCaseResult


class DataProvider:
    """Abstract interface for data providers.

    Implementations access the database asynchronously and hold a per-process
    connection pool that must be opened before use (and closed on shutdown).
    """

    async def open(self) -> None:
        """Open any process-local resources (e.g. a connection pool)."""

    async def close(self) -> None:
        """Close any process-local resources (e.g. a connection pool)."""

    async def fetch_commits_in_queue(self) -> list[Commit]:
        raise NotImplementedError()

    async def update_commit(self, _commit: Commit) -> None:
        raise NotImplementedError()

    async def store_commit_test_results(
        self, _commit: Commit, _test_results: list[TestCaseResult]
    ) -> None:
        raise NotImplementedError()

    async def delete_commit_test_results(self, _commit: Commit) -> None:
        raise NotImplementedError()

    async def fetch_exercise_files(self, _commit: Commit) -> list[str]:
        raise NotImplementedError()

    async def fetch_test_cases(self, _commit: Commit) -> list[TestCase]:
        raise NotImplementedError()

    async def claim_commit(self, _commit: Commit) -> bool:
        """Atomically take an ``STATUS_IN_QUEUE`` commit for processing.

        Return ``True`` if this caller won the commit, ``False`` if another
        worker had already taken it. The default implementation always
        claims: providers without real locking semantics (such as test
        doubles) never reject a commit.
        """
        return True

    async def release_commit(self, _commit: Commit) -> None:
        """Return a claimed commit to the queue after a retryable failure.

        Only the worker that holds the claim may call this. The default is a
        no-op for providers without locking semantics.
        """
