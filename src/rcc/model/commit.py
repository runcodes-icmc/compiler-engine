import datetime
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from ..languages import Language


class Commit:
    STATUS_IN_QUEUE: int = 0
    STATUS_COMPILING: int = 1
    STATUS_COMPILED: int = 2
    STATUS_RUNNING: int = 10
    STATUS_INCOMPLETE: int = 4
    STATUS_COMPLETED: int = 5
    STATUS_ERROR: int = 6
    STATUS_INTERNAL_ERROR: int = 9
    STATUS_PROCESSING: int = 11

    id: int
    user_email: str
    exercise_id: int
    real_exercise_id: int
    status: int
    commit_hash: str
    corrects: int
    score: float
    is_compiled: bool
    compiled_message: str
    commit_time: datetime.datetime
    compilation_started_time: datetime.datetime | None
    compilation_finished_time: datetime.datetime | None
    compiled_signal: str | int | None
    compiled_error: str
    user_ip: str | None
    aws_key: str
    offering_id: int
    real_offering_id: int
    course_id: int
    is_make: bool
    fname: str | None
    language: Language | None
    # Derived while processing (see rcc.engine.set_extension / copy_source_files).
    extension: str | None
    is_compilable: bool
    # Free-form metadata attached by some tests.
    test_cases: object

    def __init__(
        self,
        commit_id: int,
        user_email: str,
        exercise_id: int,
        real_exercise_id: int,
        status: int,
        commit_hash: str,
        corrects: int,
        score: float,
        is_compiled: bool,
        compiled_message: str,
        commit_time: datetime.datetime,
        compilation_started_time: datetime.datetime | None,
        compilation_finished_time: datetime.datetime | None,
        compiled_signal: str | int | None,
        compiled_error: str,
        user_ip: str | None,
        aws_key: str,
        offering_id: int,
        real_offering_id: int,
        course_id: int,
        fname: str | None = None,
        language: Language | None = None,
    ) -> None:
        self.id = commit_id
        self.user_email = user_email
        self.exercise_id = exercise_id
        self.real_exercise_id = real_exercise_id
        self.status = status
        self.commit_hash = commit_hash
        self.corrects = corrects
        self.score = score
        self.is_compiled = is_compiled
        self.compiled_message = compiled_message
        self.commit_time = commit_time
        self.compilation_started_time = compilation_started_time
        self.compilation_finished_time = compilation_finished_time
        self.compiled_signal = compiled_signal
        self.compiled_error = compiled_error
        self.user_ip = user_ip
        self.aws_key = aws_key
        self.offering_id = offering_id
        self.real_offering_id = real_offering_id
        self.course_id = course_id
        self.is_make = False
        self.fname = fname
        self.language = language
        self.extension = None
        self.is_compilable = False
        self.test_cases = None

    def reset(self) -> None:
        self.score = 0.0
        self.corrects = 0
        self.is_compiled = False
        self.compiled_message = ""
        self.compilation_started_time = None
        self.compilation_finished_time = None
        self.compiled_signal = ""
        self.compiled_error = ""
        self.status = Commit.STATUS_PROCESSING

    @override
    def __str__(self) -> str:
        s = (
            "Commit("
            "id={}, "
            "user_email={}, "
            "exercise_id={}, "
            "real_exercise_id={}, "
            "status={}, "
            "commit_hash={}, "
            "corrects={}, "
            "score={}, "
            "is_compiled={}, "
            "compiled_message={}, "
            "commit_time={}, "
            "compilation_started_time={}, "
            "compilation_finished_time={}, "
            "compiled_signal={}, "
            "compiled_error={}, "
            "user_ip={}, "
            "aws_key={}, "
            "offering_id={}, "
            "real_offering_id={}, "
            "course_id={})"
        )
        return s.format(
            self.id,
            self.user_email,
            self.exercise_id,
            self.real_exercise_id,
            self.status,
            self.commit_hash,
            self.corrects,
            self.score,
            self.is_compiled,
            self.compiled_message,
            self.commit_time,
            self.compilation_started_time,
            self.compilation_finished_time,
            self.compiled_signal,
            self.compiled_error,
            self.user_ip,
            self.aws_key,
            self.offering_id,
            self.real_offering_id,
            self.course_id,
        )
