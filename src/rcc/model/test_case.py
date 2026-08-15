from typing import override


class TestCase:
    IO_TYPE_TEXT: int = 1
    IO_TYPE_NUMERIC: int = 2
    IO_TYPE_BINARY: int = 3

    id: int
    exercise_id: int
    input_type: int
    output_type: int
    show_input: bool
    show_expected_output: bool
    max_mem_size: int
    cpu_time: int
    stack_size: int
    show_user_output: bool
    file_size: int
    abs_error: float | None
    last_update: object
    files: list[str]

    def __init__(
        self,
        test_case_id: int,
        exercise_id: int,
        input_type: int,
        output_type: int,
        show_input: bool,
        show_expected_output: bool,
        max_mem_size: int,
        cpu_time: int,
        stack_size: int,
        show_user_output: bool,
        file_size: int,
        abs_error: float | None,
        last_update: object,
        files: list[str] | None = None,
    ) -> None:
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
        self.files = list(files) if files is not None else []

    @override
    def __str__(self) -> str:
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
    STATUS_CORRECT: int = 1
    STATUS_MALFORMED: int = 2
    STATUS_INCORRECT: int = 0

    id: int
    commit_id: int
    test_case_id: int
    cpu_time: str | float
    status: int
    status_message: str
    mem_used: int
    output: str
    output_type: int
    error: str

    def __init__(
        self,
        commit_id: int,
        test_case_id: int,
        cpu_time: str | float,
        status: int,
        status_message: str,
        mem_used: int = -1,
        output: str = "",
        output_type: int = 2,
        error: str = "",
    ) -> None:
        self.id = -1
        self.commit_id = commit_id
        self.test_case_id = test_case_id
        self.cpu_time = cpu_time
        self.status = status
        self.status_message = status_message
        self.mem_used = mem_used
        self.output = output
        self.output_type = output_type
        self.error = error

    @override
    def __str__(self) -> str:
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
