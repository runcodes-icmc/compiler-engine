"""The commit-processing engine.

The pipeline is a producer/consumer pair:

* :mod:`rcc.engine.poller` polls the database for queued commits and feeds
  them into a bounded task queue;
* :mod:`rcc.engine.consumer` pulls commits off that queue, claims them and
  runs them through :mod:`rcc.engine.pipeline` (one commit per asyncio task).

Container execution lives in :mod:`rcc.engine.container`, output grading in
:mod:`rcc.engine.grading` and workspace/file handling in
:mod:`rcc.engine.workspace`.

Cross-module calls use direct imports (``from .pipeline import
process_commit``) rather than importing through this package: importing
``from . import module`` creates an import cycle (pyright flags it), and the
direct import binds the function in the consuming module's namespace, which
is what the tests patch (``mock.patch.object(rcc.engine.consumer,
"process_commit", ...)``).
"""

from .consumer import process_commits
from .container import ContainerLogReader, expect_message, run, run_tests
from .pipeline import process_commit

__all__ = [
    "ContainerLogReader",
    "expect_message",
    "process_commit",
    "process_commits",
    "run",
    "run_tests",
]
