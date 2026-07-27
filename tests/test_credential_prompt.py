from __future__ import annotations

from dataclasses import dataclass

from ozon_v2.adapters.credential_prompt import CredentialPromptLauncher
from ozon_v2.adapters.fs_repo import FsRepo

from tests.helpers import RuntimeTestCase


@dataclass
class FakeProcess:
    pid: int


class CredentialPromptLauncherTests(RuntimeTestCase):
    def test_open_or_focus_does_not_open_duplicate_popup_when_process_is_alive(self) -> None:
        repo = FsRepo(self.context)
        popen_calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            popen_calls.append(command)
            return FakeProcess(pid=777)

        launcher = CredentialPromptLauncher(
            repo,
            popen_factory=fake_popen,
            process_is_alive=lambda pid: pid == 777,
            cooldown_seconds=3600,
        )

        first = launcher.open_or_focus()
        second = launcher.open_or_focus()

        self.assertEqual("credential_assistant.opened", first.code)
        self.assertEqual("credential_assistant.already_open", second.code)
        self.assertEqual(1, len(popen_calls))

    def test_open_or_focus_respects_cooldown_after_recent_popup(self) -> None:
        repo = FsRepo(self.context)
        popen_calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            popen_calls.append(command)
            return FakeProcess(pid=888)

        launcher = CredentialPromptLauncher(
            repo,
            popen_factory=fake_popen,
            process_is_alive=lambda _pid: False,
            cooldown_seconds=3600,
        )

        first = launcher.open_or_focus()
        second = launcher.open_or_focus()

        self.assertEqual("credential_assistant.opened", first.code)
        self.assertEqual("credential_assistant.cooldown", second.code)
        self.assertEqual(1, len(popen_calls))
        self.assertIsNotNone(second.cooldown_until)

    def test_force_open_bypasses_cooldown(self) -> None:
        repo = FsRepo(self.context)
        popen_calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            popen_calls.append(command)
            return FakeProcess(pid=999)

        launcher = CredentialPromptLauncher(
            repo,
            popen_factory=fake_popen,
            process_is_alive=lambda _pid: False,
            cooldown_seconds=3600,
        )

        launcher.open_or_focus()
        forced = launcher.open_or_focus(force=True)

        self.assertEqual("credential_assistant.opened", forced.code)
        self.assertEqual(2, len(popen_calls))
