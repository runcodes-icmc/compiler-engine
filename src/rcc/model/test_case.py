class TestCase:
    IO_TYPE_TEXT = 1
    IO_TYPE_NUMERIC = 2
    IO_TYPE_BINARY = 3

    def __init__(
        self,
        test_case_id,
        exercise_id,
        input_type,
        output_type,
        show_input,
        show_expected_output,
        max_mem_size,
        cpu_time,
        stack_size,
        show_user_output,
        file_size,
        abs_error,
        last_update,
        files=[],
    ):
        self.id = test_case_id
        self.exercise_id = exercise_id
        self.input_type = input_type
        self.output_type = output_type
        self.show_input = show_input
        self.show_expected_output = show_expected_output
        self.max_mem_size = max_mem_size
        self.cpu_time = cpu_time
        self.stack_size = stack_size
        self.show_user_output = show_user_output
        self.file_size = file_size
        self.abs_error = abs_error
        self.last_update = last_update
        self.files = files

    def __str__(self):
        s = (
            "TestCase("
            "id={}, "
            "exercise_id={}, "
            "input_type={}, "
            "output_type={}, "
            "show_input={}, "
            "show_expected_output={}, "
            "max_mem_size={}, "
            "cpu_time={}, "
            "stack_size={}, "
            "show_user_output={}, "
            "file_size={}, "
            "abs_error={}, "
            "last_update={}, "
            "files={})"
        )
        return s.format(
            self.id,
            self.exercise_id,
            self.input_type,
            self.output_type,
            self.show_input,
            self.show_expected_output,
            self.max_mem_size,
            self.cpu_time,
            self.stack_size,
            self.show_user_output,
            self.file_size,
            self.abs_error,
            self.last_update,
            self.files,
        )


class TestCaseResult:
    STATUS_CORRECT = 1
    STATUS_MALFORMED = 2
    STATUS_INCORRECT = 0

    def __init__(
        self,
        commit_id,
        test_case_id,
        cpu_time,
        status,
        status_message,
        mem_used=-1,
        output="",
        output_type=2,
        error="",
    ):
        self.id = -1
        self.commit_id = commit_id
        self.test_case_id = test_case_id
        self.cpu_time = cpu_time
        self.status = status
        self.status_message = status_message
        self.mem_used = mem_used
        self.output = output
        self.output_type = 2
        self.error = ""

    def __str__(self):
        s = (
            "TestCaseResult("
            "commit_id={}, "
            "test_case_id={}, "
            "cpu_time={}, "
            "status={}, "
            "status_message={}, "
            "mem_used={}, "
            "output={}, "
            "output_type={}, "
            "error={})"
        )
        return s.format(
            self.id,
            self.commit_id,
            self.test_case_id,
            self.cpu_time,
            self.status,
            self.status_message,
            self.mem_used,
            self.output,
            self.output_type,
            self.error,
        )
