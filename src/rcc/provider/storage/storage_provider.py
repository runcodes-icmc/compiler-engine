class StorageProvider(object):
    def fetch_commit_file(self, commit, destination):
        raise NotImplementedError()

    def fetch_exercise_file(self, source, destination):
        raise NotImplementedError()

    def fetch_test_case_input_file(self, test_case, destination):
        raise NotImplementedError()

    def fetch_test_case_output_file(self, test_case, destination):
        raise NotImplementedError()

    def fetch_test_case_files(self, test_case, destination):
        raise NotImplementedError()

    def store_commit_output(self, commit, commit_output_fname):
        raise NotImplementedError()
