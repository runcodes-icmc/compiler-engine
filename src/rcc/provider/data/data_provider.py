class DataProvider(object):
    def fetch_commits_in_queue(self):
        raise NotImplementedError()

    def update_commit(self, commit):
        raise NotImplementedError()

    def store_commit_test_results(self, commit, test_results):
        raise NotImplementedError()

    def delete_commit_test_results(self, commit):
        raise NotImplementedError()

    def fetch_exercise_files(self, commit):
        raise NotImplementedError()

    def fetch_test_cases(self, commit):
        raise NotImplementedError()
