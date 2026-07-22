from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_oauth_api import cli
from codex_oauth_api import server as server_module
from codex_oauth_api.isolated_codex import ISOLATED_CODEX_BASE_INSTRUCTIONS, IsolatedCodexAuthenticationError
from codex_oauth_api.server import ServerSettings, create_app


class FakeCodexClient:
    def __init__(
        self,
        response: str = "격리된 응답",
        error: Exception | None = None,
        stream_chunks: list[str] | None = None,
    ):
        self.response = response
        self.error = error
        self.stream_chunks = stream_chunks if stream_chunks is not None else [response]
        self.calls: list[tuple[str, str | None]] = []
        self.stream_calls: list[tuple[str, str | None]] = []
        self.service_tiers: list[str | None] = []
        self.stream_service_tiers: list[str | None] = []

    def complete_text(
        self,
        prompt: str,
        *,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> str:
        self.calls.append((prompt, effort))
        self.service_tiers.append(service_tier)
        if self.error is not None:
            raise self.error
        return self.response

    def stream_text(
        self,
        prompt: str,
        *,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        self.stream_calls.append((prompt, effort))
        self.stream_service_tiers.append(service_tier)
        if self.error is not None:
            raise self.error
        yield from self.stream_chunks


def test_chat_completions_returns_openai_compatible_response_and_uses_isolated_settings(tmp_path: Path):
    clients: list[FakeCodexClient] = []
    captured = []

    def factory(model, *, settings, auto_login):
        captured.append((model, settings, auto_login))
        client = FakeCodexClient("안녕하세요 형님")
        clients.append(client)
        return client

    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            default_model="gpt-5.5",
            auto_login=False,
        ),
        codex_client_factory=factory,
    )

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "간결하게 답하세요."},
                {"role": "user", "content": "안녕?"},
            ],
            "reasoning_effort": "high",
            "service_tier": "fast",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-5.5"
    assert body["choices"][0]["index"] == 0
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "안녕하세요 형님"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    assert len(captured) == 1
    model, settings, auto_login = captured[0]
    assert model == "gpt-5.5"
    assert auto_login is False
    assert settings.state_root == tmp_path / "state"
    assert settings.workspace == tmp_path / "state" / "workspace"
    assert settings.base_instructions == ISOLATED_CODEX_BASE_INSTRUCTIONS
    assert clients[0].calls == [
        (
            "system: 간결하게 답하세요.\nuser: 안녕?",
            "high",
        )
    ]
    assert clients[0].service_tiers == ["fast"]


def test_debug_mode_logs_chat_completion_request_and_response_without_auth_header(tmp_path: Path, capsys):
    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            api_key="debug-auth-value",
            debug=True,
        ),
        lambda model, *, settings, auto_login: FakeCodexClient("디버그 응답"),
    )

    response = TestClient(app, client=("203.0.113.10", 50000)).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer debug-auth-value"},
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "로그 확인"}],
        },
    )

    assert response.status_code == 200
    log_entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert log_entries[0] == {
        "body": {
            "messages": [{"content": "로그 확인", "role": "user"}],
            "model": "gpt-5.5",
        },
        "client_ip": "203.0.113.10",
        "event": "codex_oauth_api.request",
        "method": "POST",
        "path": "/v1/chat/completions",
    }
    assert log_entries[1]["event"] == "codex_oauth_api.response"
    assert log_entries[1]["method"] == "POST"
    assert log_entries[1]["path"] == "/v1/chat/completions"
    assert log_entries[1]["status_code"] == 200
    assert log_entries[1]["body"]["choices"][0]["message"] == {"role": "assistant", "content": "디버그 응답"}

    timing_entry = next(entry for entry in log_entries if entry["event"] == "codex_oauth_api.timing")
    assert timing_entry["phase"] == "completion"
    assert timing_entry["timestamp"].endswith("Z")
    assert timing_entry["model_duration_ms"] >= 0
    assert timing_entry["total_duration_ms"] >= timing_entry["model_duration_ms"]

    raw_output = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in log_entries)
    assert "Authorization" not in raw_output
    assert "Bearer debug-auth-value" not in raw_output


def test_debug_mode_logs_streaming_response_chunks_as_they_are_emitted(tmp_path: Path, capsys):
    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            debug=True,
        ),
        lambda model, *, settings, auto_login: FakeCodexClient("완성 응답", stream_chunks=["첫", "째"]),
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "stream": True,
            "messages": [{"role": "user", "content": "stream"}],
        },
    ) as response:
        assert response.status_code == 200
        chunks = [line.removeprefix("data: ") for line in response.iter_lines() if line.startswith("data: ")]

    assert chunks[-1] == "[DONE]"
    log_entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert log_entries[0]["event"] == "codex_oauth_api.request"
    chunk_logs = [entry for entry in log_entries if entry["event"] == "codex_oauth_api.response.chunk"]
    assert [entry["body"]["choices"][0]["delta"] for entry in chunk_logs] == [
        {"role": "assistant"},
        {"content": "첫"},
        {"content": "째"},
        {},
    ]
    assert all(entry["status_code"] == 200 for entry in chunk_logs)
    assert [entry["body"] for entry in chunk_logs[:-1]] == [json.loads(item) for item in chunks[:-2]]
    assert chunk_logs[-1]["body"] == json.loads(chunks[-2])


def test_streaming_logs_role_chunk_before_waiting_for_first_model_delta(tmp_path: Path, monkeypatch):
    events = []

    class OrderedStreamingClient(FakeCodexClient):
        def stream_text(
            self,
            prompt: str,
            *,
            effort: str | None = None,
            service_tier: str | None = None,
        ):
            events.append("model_delta")
            yield "첫"

    def capture_debug_log(event, **fields):
        if event == "codex_oauth_api.response.chunk":
            delta = fields["body"]["choices"][0]["delta"]
            if delta == {"role": "assistant"}:
                events.append("role_chunk")

    monkeypatch.setattr(server_module, "_print_debug_log", capture_debug_log)
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", debug=True),
        lambda model, *, settings, auto_login: OrderedStreamingClient(),
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "stream"}]},
    ) as response:
        lines = [line for line in response.iter_lines() if line]
        assert lines[-1] == "data: [DONE]"

    assert events[:2] == ["role_chunk", "model_delta"]


def test_streaming_authentication_error_is_emitted_as_sse_error_after_stream_starts(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        lambda model, *, settings, auto_login: FakeCodexClient(
            error=IsolatedCodexAuthenticationError("Codex OAuth login is required")
        ),
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "stream"}]},
    ) as response:
        chunks = [line.removeprefix("data: ") for line in response.iter_lines() if line.startswith("data: ")]

    assert response.status_code == 200
    assert json.loads(chunks[0])["choices"][0]["delta"] == {"role": "assistant"}
    assert json.loads(chunks[1]) == {
        "error": {
            "message": "Codex OAuth login is required for this isolated API server state.",
            "type": "codex_authentication_required",
            "code": "codex_authentication_required",
        }
    }
    assert chunks[-1] == "[DONE]"


def test_application_lifespan_starts_and_closes_shared_runtime_once(tmp_path: Path, capsys):
    events = []
    clients = []

    class FakeRuntime:
        def __init__(self, settings):
            events.append(("init", settings.state_root))

        def start(self):
            events.append("start")

        def create_client(self, model, *, auto_login):
            events.append(("client", model, auto_login))
            client = FakeCodexClient("공유 런타임 응답")
            clients.append(client)
            return client

        def close(self):
            events.append("close")

    app = create_app(
        ServerSettings(state_root=tmp_path / "state", auto_login=False, debug=True),
        codex_runtime_factory=FakeRuntime,
    )

    with TestClient(app) as client:
        for content in ["첫 요청", "둘째 요청"]:
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": content}]},
            )
            assert response.status_code == 200

    assert events == [
        ("init", tmp_path / "state"),
        "start",
        ("client", "gpt-5.5", False),
        ("client", "gpt-5.5", False),
        "close",
    ]
    assert [client.calls for client in clients] == [
        [("user: 첫 요청", None)],
        [("user: 둘째 요청", None)],
    ]
    log_entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    runtime_timing = next(entry for entry in log_entries if entry.get("phase") == "runtime_startup")
    assert runtime_timing["duration_ms"] >= 0
    assert runtime_timing["timestamp"].endswith("Z")


def test_chat_completions_uses_default_model_when_request_model_is_omitted(tmp_path: Path):
    captured = []

    def factory(model, *, settings, auto_login):
        captured.append(model)
        return FakeCodexClient("기본 모델 응답")

    app = create_app(ServerSettings(state_root=tmp_path / "state", default_model="gpt-5.5"), factory)

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "안녕?"}]},
    )

    assert response.status_code == 200
    assert captured == ["gpt-5.5"]
    assert response.json()["model"] == "gpt-5.5"


def test_streaming_chat_completions_streams_codex_deltas_as_openai_sse_chunks(tmp_path: Path):
    clients: list[FakeCodexClient] = []

    def factory(model, *, settings, auto_login):
        client = FakeCodexClient("완성 응답", stream_chunks=["스", "트", "림"])
        clients.append(client)
        return client

    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        factory,
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "stream"}],
            "stream": True,
            "reasoning_effort": "minimal",
            "service_tier": "fast",
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = [line.removeprefix("data: ") for line in response.iter_lines() if line.startswith("data: ")]

    assert chunks[-1] == "[DONE]"
    payloads = [json.loads(item) for item in chunks[:-1]]
    assert [payload["choices"][0]["delta"] for payload in payloads] == [
        {"role": "assistant"},
        {"content": "스"},
        {"content": "트"},
        {"content": "림"},
        {},
    ]
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert clients[0].calls == []
    assert clients[0].stream_calls == [("user: stream", "minimal")]
    assert clients[0].stream_service_tiers == ["fast"]


def test_chat_completions_rejects_unsupported_service_tier(tmp_path: Path):
    app = create_app(ServerSettings(state_root=tmp_path / "state"), lambda *args, **kwargs: FakeCodexClient())

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "안녕?"}],
            "service_tier": "priority",
        },
    )

    assert response.status_code == 422


def test_server_settings_from_env_parses_allowed_ips(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_OAUTH_API_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_OAUTH_API_ALLOWED_IPS", " 203.0.113.10,198.51.100.7,, 192.0.2.3 ")
    monkeypatch.setenv("CODEX_OAUTH_API_ALLOWED_IP", "10.0.0.1")
    monkeypatch.setenv("CODEX_OAUTH_API_THREAD_POOL_SIZE", "6")

    settings = ServerSettings.from_env()

    assert settings.allowed_ips == ("203.0.113.10", "198.51.100.7", "192.0.2.3")
    assert settings.thread_pool_size == 6


def test_server_settings_from_env_defaults_allowed_ips_to_empty_tuple(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_OAUTH_API_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("CODEX_OAUTH_API_ALLOWED_IPS", raising=False)

    settings = ServerSettings.from_env()

    assert settings.allowed_ips == ()
    assert settings.thread_pool_size == 4


def test_server_settings_from_env_loads_dotenv_values(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEX_OAUTH_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_API_ALLOWED_IPS", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_API_ALLOWED_IP", raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "CODEX_OAUTH_API_KEY=dotenv-secret",
                "CODEX_OAUTH_API_ALLOWED_IPS=203.0.113.10, 198.51.100.7",
                "CODEX_OAUTH_API_ALLOWED_IP=10.0.0.1",
            ]
        ),
        encoding="utf-8",
    )

    settings = ServerSettings.from_env()

    assert settings.api_key == "dotenv-secret"
    assert settings.allowed_ips == ("203.0.113.10", "198.51.100.7")


def test_server_settings_from_env_keeps_os_environment_precedence_over_dotenv(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_OAUTH_API_KEY", "env-secret")
    monkeypatch.setenv("CODEX_OAUTH_API_ALLOWED_IPS", "192.0.2.3")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "CODEX_OAUTH_API_KEY=dotenv-secret",
                "CODEX_OAUTH_API_ALLOWED_IPS=203.0.113.10",
            ]
        ),
        encoding="utf-8",
    )

    settings = ServerSettings.from_env()

    assert settings.api_key == "env-secret"
    assert settings.allowed_ips == ("192.0.2.3",)


def test_health_remains_public_when_allowed_ips_are_configured(tmp_path: Path):
    app = create_app(ServerSettings(state_root=tmp_path / "state", allowed_ips=("203.0.113.10",)))

    response = TestClient(app, client=("198.51.100.7", 50000)).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_endpoint_rejects_unlisted_client_ip(tmp_path: Path):
    app = create_app(ServerSettings(state_root=tmp_path / "state", allowed_ips=("203.0.113.10",)))

    response = TestClient(app, client=("198.51.100.7", 50000)).get("/v1/models")

    assert response.status_code == 403


def test_chat_completions_rejects_unlisted_client_ip(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", allowed_ips=("203.0.113.10",)),
        lambda model, *, settings, auto_login: FakeCodexClient("허용됨"),
    )

    response = TestClient(app, client=("198.51.100.7", 50000)).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "안녕"}]},
    )

    assert response.status_code == 403


def test_models_endpoint_allows_listed_client_ip(tmp_path: Path):
    app = create_app(ServerSettings(state_root=tmp_path / "state", allowed_ips=("203.0.113.10",)))

    response = TestClient(app, client=("203.0.113.10", 50000)).get("/v1/models")

    assert response.status_code == 200


def test_chat_completions_allows_listed_client_ip(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", allowed_ips=("203.0.113.10",)),
        lambda model, *, settings, auto_login: FakeCodexClient("허용됨"),
    )

    response = TestClient(app, client=("203.0.113.10", 50000)).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "안녕"}]},
    )

    assert response.status_code == 200


def test_ip_rejection_takes_precedence_over_api_key_rejection(tmp_path: Path):
    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            api_key="secret",
            allowed_ips=("203.0.113.10",),
        ),
        lambda model, *, settings, auto_login: FakeCodexClient("차단되어야 함"),
    )

    response = TestClient(app, client=("198.51.100.7", 50000)).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json={"messages": [{"role": "user", "content": "안녕"}]},
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "message": "Client IP is not allowed.",
            "type": "forbidden_client_ip",
            "code": "forbidden_client_ip",
        }
    }


def test_models_endpoint_requires_api_key_for_allowed_client_ip(tmp_path: Path):
    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            api_key="secret",
            allowed_ips=("203.0.113.10",),
        )
    )
    client = TestClient(app, client=("203.0.113.10", 50000))

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_debug_access_control_rejection_logs_no_auth_header_or_secret(tmp_path: Path, capsys):
    app = create_app(
        ServerSettings(
            state_root=tmp_path / "state",
            api_key="debug-auth-value",
            allowed_ips=("203.0.113.10",),
            debug=True,
        ),
        lambda model, *, settings, auto_login: FakeCodexClient("차단되어야 함"),
    )

    response = TestClient(app, client=("203.0.113.10", 50000)).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json={"messages": [{"role": "user", "content": "로그 확인"}]},
    )

    assert response.status_code == 401
    log_entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert log_entries == [
        {
            "body": {
                "error": {
                    "code": "invalid_api_key",
                    "message": "Invalid or missing API key.",
                    "type": "invalid_request_error",
                }
            },
            "event": "codex_oauth_api.response",
            "method": "POST",
            "path": "/v1/chat/completions",
            "status_code": 401,
        }
    ]

    raw_output = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in log_entries)
    assert "Authorization" not in raw_output
    assert "Bearer wrong" not in raw_output
    assert "debug-auth-value" not in raw_output


def test_configured_api_key_requires_exact_bearer_token(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", api_key="secret"),
        lambda model, *, settings, auto_login: FakeCodexClient("인증됨"),
    )
    client = TestClient(app)

    assert client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "안녕"}]}).status_code == 401
    assert (
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={"messages": [{"role": "user", "content": "안녕"}]},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"messages": [{"role": "user", "content": "안녕"}]},
        ).status_code
        == 200
    )


def test_codex_authentication_error_maps_to_service_unavailable(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        lambda model, *, settings, auto_login: FakeCodexClient(
            error=IsolatedCodexAuthenticationError("Codex OAuth login is required")
        ),
    )

    response = TestClient(app).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "안녕"}]},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "message": "Codex OAuth login is required for this isolated API server state.",
            "type": "codex_authentication_required",
            "code": "codex_authentication_required",
        }
    }


def test_models_endpoint_lists_default_model(tmp_path: Path):
    app = create_app(ServerSettings(state_root=tmp_path / "state", default_model="gpt-5.5"))

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": "gpt-5.5",
                "object": "model",
                "created": 0,
                "owned_by": "codex-oauth-api",
            }
        ],
    }


def test_cli_login_uses_isolated_local_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_OAUTH_API_STATE_ROOT", str(tmp_path / "state"))

    captured = []

    class FakeLogin:
        def __init__(self, *, settings):
            captured.append(settings)

        def login_with_device_code(self, on_device_code=None, on_login_complete=None):
            if on_device_code is not None:
                on_device_code(type("DeviceCode", (), {"verification_url": "https://example.test", "user_code": "ABCD"})())
            if on_login_complete is not None:
                on_login_complete()
            return type("Result", (), {"user_code": "ABCD"})()

    monkeypatch.setattr(cli, "CodexOAuthLogin", FakeLogin)

    assert cli.main(["login"]) == 0
    assert len(captured) == 1
    assert captured[0].state_root == tmp_path / "state"
    assert captured[0].workspace == tmp_path / "state" / "workspace"
    assert captured[0].base_instructions == ISOLATED_CODEX_BASE_INSTRUCTIONS


def test_cli_generate_key_prints_dotenv_assignment(monkeypatch, capsys):
    captured = {}

    def fake_token_urlsafe(byte_count):
        captured["byte_count"] = byte_count
        return "generated-url-safe-key"

    monkeypatch.setattr(cli.secrets, "token_urlsafe", fake_token_urlsafe)

    assert cli.main(["generate-key"]) == 0

    assert captured == {"byte_count": 32}
    assert capsys.readouterr().out == "CODEX_OAUTH_API_KEY=generated-url-safe-key\n"


def test_cli_serve_debug_enables_debug_logging_setting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_OAUTH_API_STATE_ROOT", str(tmp_path / "state"))
    captured = {}

    def fake_create_app(settings):
        captured["settings"] = settings
        return object()

    def fake_run(app, *, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve", "--debug", "--host", "127.0.0.1", "--port", "9999"]) == 0
    assert captured["settings"].debug is True
    assert captured["settings"].state_root == tmp_path / "state"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
