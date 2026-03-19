class StorageProvider(object):
    def fetch_commit_file(self, _commit, _destination):
        raise NotImplementedError()

    def fetch_exercise_file(self, _source, _destination):
        raise NotImplementedError()

    def fetch_test_case_input_file(self, _test_case, _destination):
        raise NotImplementedError()

    def fetch_test_case_output_file(self, _test_case, _destination):
        raise NotImplementedError()

    def fetch_test_case_files(self, _test_case, _destination):
        raise NotImplementedError()

    def store_commit_output(self, _commit, _commit_output_fname):
        raise NotImplementedError()
