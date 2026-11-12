from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from codex_oauth_api.dashboard import mask_secret
from codex_oauth_api.isolated_codex import CodexOAuthLoginResult
from codex_oauth_api.logbuffer import LOG_BUFFER, LogBuffer
from codex_oauth_api.server import ServerSettings, create_app

LOOPBACK = ("127.0.0.1", 50000)


class FakeLogin:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def login_with_device_code(self, on_device_code, on_login_complete):
        result = CodexOAuthLoginResult(verification_url="https://example.test/device", user_code="ABCD-1234")
        on_device_code(result)
        if self.fail:
            raise RuntimeError("denied")
        on_login_complete()
        return result


def make_client(tmp_path: Path, **overrides):
    settings = ServerSettings(state_root=tmp_path / "state", env_file=tmp_path / ".env", **overrides)
    app = create_app(
        settings,
        codex_client_factory=lambda *a, **k: None,
        auth_login_factory=lambda _s: FakeLogin(),
    )
    LOG_BUFFER.clear()
    return TestClient(app, client=LOOPBACK), settings


def write_auth(settings: ServerSettings, **tokens):
    path = settings.state_root / "codex-home" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": "me@example.test"}).encode()).decode().rstrip("=")
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "refresh_token": "secret-refresh-token",
                    "id_token": f"h.{claims}.s",
                    "account_id": "acct-1234567890",
                    **tokens,
                },
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dashboard_served_at_root_and_dashboard(tmp_path: Path):
    client, _ = make_client(tmp_path)
    for path in ("/", "/dashboard"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "CodexOAuthAPI" in response.text


def test_auth_status_never_exposes_tokens(tmp_path: Path):
    client, settings = make_client(tmp_path)
    assert client.get("/api/auth/status").json()["authenticated"] is False

    write_auth(settings)
    body = client.get("/api/auth/status").json()
    assert body["authenticated"] is True
    assert body["email"] == "me@example.test"
    assert body["account_id"] == "acct...7890"
    assert body["last_refresh"] == "2026-01-01T00:00:00Z"
    assert "secret-access-token" not in json.dumps(body)
    assert "secret-refresh-token" not in json.dumps(body)


def test_login_returns_device_code_then_completes(tmp_path: Path):
    client, _ = make_client(tmp_path)
    response = client.post("/api/auth/login")
    assert response.status_code == 200
    login = response.json()["login"]
    assert login["verification_url"] == "https://example.test/device"
    assert login["user_code"] == "ABCD-1234"

    deadline = time.time() + 2
    while time.time() < deadline and client.get("/api/auth/status").json()["login"]["status"] == "waiting":
        time.sleep(0.02)
    assert client.get("/api/auth/status").json()["login"]["status"] == "completed"


def test_login_failure_is_reported(tmp_path: Path):
    settings = ServerSettings(state_root=tmp_path / "state", env_file=tmp_path / ".env")
    app = create_app(
        settings,
        codex_client_factory=lambda *a, **k: None,
        auth_login_factory=lambda _s: FakeLogin(fail=True),
    )
    client = TestClient(app, client=LOOPBACK)
    client.post("/api/auth/login")
    deadline = time.time() + 2
    status = {}
    while time.time() < deadline:
        status = client.get("/api/auth/status").json()["login"]
        if status["status"] == "failed":
            break
        time.sleep(0.02)
    assert status["status"] == "failed"
    assert status["error"] == "denied"


def test_logout_removes_auth_file(tmp_path: Path):
    client, settings = make_client(tmp_path)
    path = write_auth(settings)
    assert client.post("/api/auth/logout").json() == {"authenticated": False, "removed": True}
    assert not path.exists()
    assert client.post("/api/auth/logout").json()["removed"] is False


def test_logs_filter_clear_and_stream(tmp_path: Path):
    client, _ = make_client(tmp_path)
    LOG_BUFFER.add("info", "timing", "completion: total=12")
    LOG_BUFFER.add("error", "error", "boom happened")

    assert len(client.get("/api/logs").json()["data"]) >= 2
    assert [r["message"] for r in client.get("/api/logs", params={"level": "error"}).json()["data"]] == ["boom happened"]
    assert [r["type"] for r in client.get("/api/logs", params={"q": "COMPLETION"}).json()["data"]] == ["timing"]

    stream = client.get("/api/logs/stream", params={"follow": "false", "type": "error"})
    assert stream.status_code == 200
    events = [line for line in stream.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 1 and "boom happened" in events[0]

    assert client.delete("/api/logs").json() == {"cleared": True}
    assert client.get("/api/logs", params={"type": "timing"}).json()["data"] == []


def test_log_buffer_keeps_only_latest_records():
    buffer = LogBuffer(maxlen=3)
    for index in range(5):
        buffer.add("info", "system", f"m{index}")
    assert [r["message"] for r in buffer.list()] == ["m2", "m3", "m4"]


def test_requests_are_recorded_as_access_logs(tmp_path: Path):
    client, _ = make_client(tmp_path)
    client.get("/v1/models")
    messages = [r["message"] for r in client.get("/api/logs", params={"type": "access"}).json()["data"]]
    assert any(m.startswith("GET /v1/models -> 200") for m in messages)


def test_settings_masks_api_key_and_updates_env_file(tmp_path: Path):
    client, settings = make_client(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("TZ=Asia/Seoul\nCODEX_OAUTH_API_KEY=super-secret-key\n", encoding="utf-8")

    items = {item["name"]: item for item in client.get("/api/settings").json()["items"]}
    assert items["CODEX_OAUTH_API_KEY"]["value"] == mask_secret("super-secret-key")
    assert "super-secret-key" not in json.dumps(items)

    response = client.put(
        "/api/settings",
        json={
            "values": {
                "CODEX_OAUTH_API_KEY": items["CODEX_OAUTH_API_KEY"]["value"],
                "CODEX_OAUTH_API_DEFAULT_MODEL": "gpt-test",
                "CODEX_OAUTH_API_DEBUG": "true",
                "CODEX_OAUTH_API_ALLOWED_IPS": "127.0.0.1, 10.0.0.2",
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    text = env_path.read_text(encoding="utf-8")
    assert "TZ=Asia/Seoul" in text
    assert "CODEX_OAUTH_API_KEY=super-secret-key" in text
    assert "CODEX_OAUTH_API_DEFAULT_MODEL=gpt-test" in text
    assert "CODEX_OAUTH_API_ALLOWED_IPS=127.0.0.1,10.0.0.2" in text

    client.put("/api/settings", json={"values": {"CODEX_OAUTH_API_DEFAULT_MODEL": ""}})
    assert "CODEX_OAUTH_API_DEFAULT_MODEL" not in env_path.read_text(encoding="utf-8")


def test_settings_rejects_invalid_values(tmp_path: Path):
    client, _ = make_client(tmp_path)
    for values in (
        {"UNKNOWN": "x"},
        {"CODEX_OAUTH_API_THREAD_POOL_SIZE": "many"},
        {"CODEX_OAUTH_API_DEBUG": "yes"},
        {"CODEX_OAUTH_API_HOST": "a\nb"},
        {"CODEX_OAUTH_API_STATE_ROOT": "/tmp/x"},
    ):
        assert client.put("/api/settings", json={"values": values}).status_code == 400
    assert client.put("/api/settings", json={}).status_code == 400
    assert not (tmp_path / ".env").exists()


def test_generate_key_returns_fresh_key_without_saving(tmp_path: Path):
    client, _ = make_client(tmp_path)
    first = client.post("/api/settings/generate-key").json()["api_key"]
    second = client.post("/api/settings/generate-key").json()["api_key"]
    assert len(first) >= 32 and first != second
    assert not (tmp_path / ".env").exists()


def test_api_requires_key_when_configured(tmp_path: Path):
    client, _ = make_client(tmp_path, api_key="k3y")
    assert client.get("/api/auth/status").status_code == 401
    assert client.get("/api/auth/status", headers={"Authorization": "Bearer k3y"}).status_code == 200
    assert client.get("/").status_code == 200


def test_api_is_loopback_only_without_credentials(tmp_path: Path):
    settings = ServerSettings(state_root=tmp_path / "state", env_file=tmp_path / ".env")
    app = create_app(settings, codex_client_factory=lambda *a, **k: None)
    remote = TestClient(app, client=("203.0.113.5", 50000))
    assert remote.get("/api/settings").status_code == 403
    assert remote.get("/").status_code == 200
