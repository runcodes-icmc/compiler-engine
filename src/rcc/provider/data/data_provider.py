class DataProvider(object):
    """Abstract interface for data providers.

    Implementations access the database asynchronously and hold a per-process
    connection pool that must be opened before use (and closed on shutdown).
    """

    async def open(self):
        """Open any process-local resources (e.g. a connection pool)."""

    async def close(self):
        """Close any process-local resources (e.g. a connection pool)."""

    async def fetch_commits_in_queue(self):
        raise NotImplementedError()

    async def update_commit(self, _commit):
        raise NotImplementedError()

    async def store_commit_test_results(self, _commit, _test_results):
        raise NotImplementedError()

    async def delete_commit_test_results(self, _commit):
        raise NotImplementedError()

    async def fetch_exercise_files(self, _commit):
        raise NotImplementedError()

    async def fetch_test_cases(self, _commit):
        raise NotImplementedError()
