from __future__ import annotations

import json
import subprocess
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codex_oauth_api.isolated_codex import (
    ISOLATED_CODEX_BASE_INSTRUCTIONS,
    CodexOAuthLogin,
    CodexOAuthLoginResult,
    IsolatedCodexAuthenticationError,
    IsolatedCodexJSONClient,
    IsolatedCodexRuntime,
    IsolatedCodexSettings,
)


class FakeThread:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []
        self.run_kwargs: list[dict] = []
        self.turn_kwargs: list[dict] = []

    def run(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        self.run_kwargs.append(kwargs)
        return SimpleNamespace(final_response=self.response)

    def turn(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        self.turn_kwargs.append(kwargs)
        return SimpleNamespace(
            stream=lambda: iter(
                [
                    SimpleNamespace(
                        method="item/agentMessage/delta",
                        payload=SimpleNamespace(delta="첫", turn_id="turn-1"),
                    ),
                    SimpleNamespace(
                        method="item/agentMessage/delta",
                        payload=SimpleNamespace(delta="째", turn_id="turn-1"),
                    ),
                    SimpleNamespace(
                        method="turn/completed",
                        payload=SimpleNamespace(turn=SimpleNamespace(id="turn-1")),
                    ),
                ]
            )
        )


class FakeCodex:
    def __init__(self, response: str):
        self.thread_start_calls: list[dict] = []
        self.thread = FakeThread(response)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def thread_start(
        self,
        *,
        model,
        sandbox=None,
        ephemeral=None,
        base_instructions=None,
        developer_instructions=None,
        config=None,
        service_tier=None,
    ):
        self.thread_start_calls.append(
            {
                "model": model,
                "sandbox": sandbox,
                "ephemeral": ephemeral,
                "base_instructions": base_instructions,
                "developer_instructions": developer_instructions,
                "config": config,
                "service_tier": service_tier,
            }
        )
        return self.thread


def test_runtime_reuses_one_codex_process_and_closes_it_once(tmp_path: Path):
    codex = FakeCodex("공유 응답")
    close_calls = []
    codex.close = lambda: close_calls.append("close")
    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(state_root=tmp_path / "state"),
        codex_factory=lambda: codex,
        sandbox="read-only",
    )

    runtime.start()
    first = runtime.create_client("gpt-5.5", auto_login=False)
    second = runtime.create_client("gpt-5.5", auto_login=False)

    assert first.complete_text("첫 요청") == "공유 응답"
    assert second.complete_text("둘째 요청") == "공유 응답"
    assert len(codex.thread_start_calls) == 2

    runtime.close()
    runtime.close()
    assert close_calls == ["close"]


def test_complete_json_starts_ephemeral_thread_without_global_instructions():
    codex = FakeCodex(json.dumps({"summary": "요약"}, ensure_ascii=False))
    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: codex, sandbox="read-only")

    assert client.complete_json("Extract.", "본문", "summary_schema") == {"summary": "요약"}
    assert codex.thread_start_calls == [
        {
            "model": "gpt-5.5",
            "sandbox": "read-only",
            "ephemeral": True,
            "base_instructions": ISOLATED_CODEX_BASE_INSTRUCTIONS,
            "developer_instructions": None,
            "config": {"skills": {"include_instructions": False}},
            "service_tier": None,
        }
    ]
    assert codex.thread.prompts == [
        "Instruction:\nExtract.\n\nJSON schema name: summary_schema\n\n"
        "Return exactly one JSON object. Do not include markdown or prose.\n\nInput:\n본문"
    ]


def test_stream_text_yields_codex_agent_message_deltas():
    codex = FakeCodex("최종 응답")
    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: codex, sandbox="read-only")

    assert list(client.stream_text("안녕?", service_tier="fast")) == ["첫", "째"]
    assert codex.thread_start_calls == [
        {
            "model": "gpt-5.5",
            "sandbox": "read-only",
            "ephemeral": True,
            "base_instructions": ISOLATED_CODEX_BASE_INSTRUCTIONS,
            "developer_instructions": None,
            "config": {"skills": {"include_instructions": False}},
            "service_tier": "fast",
        }
    ]
    assert codex.thread.prompts == ["안녕?"]
    assert codex.thread.turn_kwargs == [{}]


def test_runtime_prewarms_standard_default_model_threads_and_replenishes_them(tmp_path: Path):
    replenished = Event()

    class PoolingCodex(FakeCodex):
        def thread_start(self, **kwargs):
            thread = FakeThread(f"응답-{len(self.thread_start_calls) + 1}")
            self.thread_start_calls.append(kwargs)
            if len(self.thread_start_calls) == 3:
                replenished.set()
            return thread

    codex = PoolingCodex("미사용")
    codex.close = lambda: None
    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(
            state_root=tmp_path / "state",
            pooled_model="gpt-5.5",
            thread_pool_size=2,
        ),
        codex_factory=lambda: codex,
        sandbox="read-only",
    )

    runtime.start()
    assert len(codex.thread_start_calls) == 2
    assert all(call["service_tier"] is None for call in codex.thread_start_calls)

    client = runtime.create_client("gpt-5.5", auto_login=False)
    assert client.complete_text("일반 요청") in {"응답-1", "응답-2"}
    assert replenished.wait(1)

    client.complete_text("Fast 요청", service_tier="fast")
    assert any(call["service_tier"] == "fast" for call in codex.thread_start_calls)
    runtime.close()


def test_settings_build_codex_config_uses_project_local_state_and_excludes_openai_api_key(
    tmp_path: Path,
    monkeypatch,
):
    class CapturingConfig:
        def __init__(self, cwd=None, env=None):
            self.cwd = cwd
            self.env = env

    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    settings = IsolatedCodexSettings(state_root=tmp_path / "state")

    config = settings.build_codex_config(CapturingConfig)

    assert config.cwd == str(tmp_path / "state" / "workspace")
    assert config.env == {
        "CODEX_HOME": str(tmp_path / "state" / "codex-home"),
        "HOME": str(tmp_path / "state" / "home"),
        "USERPROFILE": str(tmp_path / "state" / "home"),
    }
    assert (tmp_path / "state" / "workspace").is_dir()
    assert "OPENAI_API_KEY" not in config.env


def test_client_auto_logs_in_and_retries_once_after_401():
    events = []

    class UnauthorizedThread:
        def run(self, prompt: str):
            events.append(("run", prompt))
            raise RuntimeError("unexpected status 401 Unauthorized: Missing bearer or basic authentication")

    class SuccessThread:
        def run(self, prompt: str):
            events.append(("run", prompt))
            return SimpleNamespace(final_response=json.dumps({"ok": True}))

    class UnauthorizedCodex:
        def __enter__(self):
            events.append("enter-unauthorized")
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def thread_start(self, **kwargs):
            return UnauthorizedThread()

    class SuccessCodex:
        def __enter__(self):
            events.append("enter-success")
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def thread_start(self, **kwargs):
            return SuccessThread()

    class FakeOAuthLogin:
        def login_with_device_code(self, on_device_code=None, on_login_complete=None):
            events.append("login")
            if on_device_code is not None:
                on_device_code(CodexOAuthLoginResult("https://example.test/device", "ABCD-EFGH"))
            if on_login_complete is not None:
                on_login_complete()

    codexes = iter([UnauthorizedCodex(), SuccessCodex()])
    client = IsolatedCodexJSONClient(
        "gpt-5.5",
        codex_factory=lambda: next(codexes),
        oauth_login=FakeOAuthLogin(),
        on_device_code=lambda result: events.append(("code", result.user_code)),
        on_login_complete=lambda: events.append("complete"),
    )

    assert client.complete_json("Return JSON.", "body", "schema") == {"ok": True}
    assert events == [
        "enter-unauthorized",
        (
            "run",
            "Instruction:\nReturn JSON.\n\nJSON schema name: schema\n\n"
            "Return exactly one JSON object. Do not include markdown or prose.\n\nInput:\nbody",
        ),
        "login",
        ("code", "ABCD-EFGH"),
        "complete",
        "enter-success",
        (
            "run",
            "Instruction:\nReturn JSON.\n\nJSON schema name: schema\n\n"
            "Return exactly one JSON object. Do not include markdown or prose.\n\nInput:\nbody",
        ),
    ]


def test_oauth_login_uses_device_code_flow(tmp_path: Path):
    events = []

    class FakeLogin:
        verification_url = "https://example.test/device"
        user_code = "ABCD-EFGH"

        def wait(self):
            events.append("wait")

    class FakeCodex:
        def __init__(self, config):
            events.append(("cwd", config.cwd))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login_chatgpt_device_code(self):
            events.append("login_chatgpt_device_code")
            return FakeLogin()

    class CapturingConfig:
        def __init__(self, cwd=None, env=None):
            self.cwd = cwd
            self.env = env

    settings = IsolatedCodexSettings(state_root=tmp_path / "state")
    login = CodexOAuthLogin(settings=settings, codex_class=FakeCodex, codex_config_class=CapturingConfig)

    result = login.login_with_device_code(
        lambda device_code: events.append(("code", device_code.user_code)),
        lambda: events.append("complete"),
    )

    assert result.verification_url == "https://example.test/device"
    assert result.user_code == "ABCD-EFGH"
    assert events == [
        ("cwd", str(tmp_path / "state" / "workspace")),
        "login_chatgpt_device_code",
        ("code", "ABCD-EFGH"),
        "wait",
        "complete",
    ]


def test_client_maps_401_to_authentication_error_when_auto_login_is_disabled():
    class UnauthorizedThread:
        def run(self, prompt: str):
            raise RuntimeError("unexpected status 401 Unauthorized: Missing bearer or basic authentication")

    class UnauthorizedCodex:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def thread_start(self, **kwargs):
            return UnauthorizedThread()

    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: UnauthorizedCodex(), auto_login=False)

    with pytest.raises(IsolatedCodexAuthenticationError, match="Codex OAuth login is required"):
        client.complete_text("안녕?")


def test_hidden_window_factory_hides_codex_process_on_windows():
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return SimpleNamespace()

    class LaunchingCodex:
        def __init__(self, config):
            from openai_codex import client as codex_client

            codex_client.subprocess.Popen(["codex"], creationflags=0)

    class CapturingConfig:
        def __init__(self, cwd=None, env=None):
            self.cwd = cwd
            self.env = env

    client = IsolatedCodexJSONClient("gpt-5.5")
    factory = client._build_hidden_window_codex_factory(LaunchingCodex, CapturingConfig)

    with (
        patch("codex_oauth_api.isolated_codex.os.name", "nt"),
        patch("openai_codex.client.subprocess.Popen", side_effect=fake_popen),
    ):
        factory()

    assert len(popen_calls) == 1
    assert popen_calls[0]["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert popen_calls[0]["startupinfo"].wShowWindow == subprocess.SW_HIDE
