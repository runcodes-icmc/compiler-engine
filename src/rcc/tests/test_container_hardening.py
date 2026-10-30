"""Unit tests for the container-hardening flags added to `run()` in
container.py (compiler-monitor-oportunidades.md, items S1/S2/S10):
`network_mode="none"`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`.

These flags only constrain what the container is *allowed* to do; they do
not touch the image, volumes, or anything else that determines what
actually runs inside it. A full end-to-end run (actually executing a
submission inside the hardened container) is exercised by the existing
`rcc.tests.engine.test_process_commit_hello` suite, which this change does
not otherwise modify. This test instead pins down, at the unit level, that
the Docker SDK is called with exactly the previous arguments plus the new
hardening ones -- so a reviewer does not have to trust "it still works",
they can see the call surface is additive.
"""

import asyncio
import datetime
import logging
import unittest
from typing import override
from unittest.mock import MagicMock, patch

from rcc.config import Config
from rcc.engine import container
from rcc.languages import Language
from rcc.model import Commit
from rcc.provider.data import DataProvider


def build_commit() -> Commit:
    return Commit(
        1,
        "user@example.com",
        1,
        1,
        Commit.STATUS_RUNNING,
        "",
        0,
        0,
        False,
        "",
        datetime.datetime.now(tz=datetime.UTC),
        None,
        None,
        None,
        "",
        None,
        "hello.c",
        1,
        1,
        1,
        "hello.c",
        Language("C", ["c", "h"]),
    )


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


class TestContainerHardening(unittest.TestCase):
    cfg: Config = Config({"base_exec_timeout": 1.0})
    commit: Commit = build_commit()
    fake_container: MagicMock = MagicMock()
    fake_client: MagicMock = MagicMock()
    data_provider: MagicMock = MagicMock()

    @override
    def setUp(self) -> None:
        self.cfg = Config({"base_exec_timeout": 1.0})
        self.commit = build_commit()
        self.fake_container = MagicMock()
        self.fake_container.logs.return_value = iter([])
        self.fake_client = MagicMock()
        self.fake_client.containers.run.return_value = self.fake_container
        self.data_provider = MagicMock(spec=DataProvider)
        self.data_provider.update_commit = _async_noop
        logging.getLogger("run.codes").addHandler(logging.NullHandler())

    def test_run_passes_hardening_flags_without_changing_prior_arguments(
        self,
    ) -> None:
        with patch.object(container.docker, "from_env", return_value=self.fake_client):

            async def go() -> None:
                try:
                    await container.run(
                        self.cfg,
                        self.data_provider,
                        self.commit,
                        [],
                        "/tmp/base",
                        "/tmp/remote",
                    )
                except Exception as exc:  # noqa: BLE001
                    # The fake log stream ends immediately, so the
                    # compilation/run handshake raises downstream; this test
                    # only cares about how containers.run() was invoked.
                    logging.getLogger(__name__).debug(
                        "expected downstream failure from the fake log stream: %s",
                        exc,
                    )

            asyncio.run(go())

        self.fake_client.containers.run.assert_called_once()
        call = self.fake_client.containers.run.call_args
        args = call.args
        kwargs = call.kwargs
        language = self.commit.language
        assert language is not None

        self.assertEqual(args[0], language.image)
        self.assertTrue(kwargs["detach"])
        self.assertFalse(kwargs["remove"])
        self.assertEqual(
            kwargs["volumes"],
            {"/tmp/remote": {"bind": "/root", "mode": "rw"}},
        )

        # The hardening flags themselves.
        self.assertEqual(kwargs["network_mode"], "none")
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertEqual(kwargs["security_opt"], ["no-new-privileges"])


if __name__ == "__main__":
    _ = unittest.main()
