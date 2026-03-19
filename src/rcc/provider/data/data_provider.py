class DataProvider(object):
    def fetch_commits_in_queue(self):
        raise NotImplementedError()

    def update_commit(self, _commit):
        raise NotImplementedError()

    def store_commit_test_results(self, _commit, _test_results):
        raise NotImplementedError()

    def delete_commit_test_results(self, _commit):
        raise NotImplementedError()

    def fetch_exercise_files(self, _commit):
        raise NotImplementedError()

    def fetch_test_cases(self, _commit):
        raise NotImplementedError()
