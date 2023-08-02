from __future__ import unicode_literals

class Commit:
    STATUS_IN_QUEUE = 0
    STATUS_COMPILING = 1
    STATUS_COMPILED = 2
    STATUS_RUNNING = 3
    STATUS_INCOMPLETE = 4
    STATUS_COMPLETED = 5
    STATUS_ERROR = 6
    STATUS_INTERNAL_ERROR = 9
    STATUS_RUNNING = 10
    STATUS_PROCESSING = 11

    def __init__(
        self,
        commit_id,
        user_email,
        exercise_id,
        real_exercise_id,
        status,
        commit_hash,
        corrects,
        score,
        is_compiled,
        compiled_message,
        commit_time,
        compilation_started_time,
        compilation_finished_time,
        compiled_signal,
        compiled_error,
        user_ip,
        aws_key,
        offering_id,
        real_offering_id,
        course_id,
        fname=None,
        language=None,
    ):
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

    def reset(self):
        self.score = 0.0
        self.corrects = 0
        self.is_compiled = False
        self.compiled_message = ""
        self.compilation_started_time = None
        self.compilation_finished_time = None
        self.compiled_signal = ""
        self.compiled_error = ""
        self.status = Commit.STATUS_PROCESSING

    def __str__(self):
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
