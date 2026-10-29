from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codex_oauth_api import isolated_codex as isolated_module
from codex_oauth_api import server as server_module
from codex_oauth_api.isolated_codex import (
    CodexTurnCancelledError,
    IsolatedCodexJSONClient,
    IsolatedCodexRuntime,
    IsolatedCodexSettings,
    _SingleFlightLogin,
)
from codex_oauth_api.server import ServerSettings, _ConcurrencyGate, create_app


class TransportClosedError(Exception):
    pass


class BlockingClient:
    """Fake codex client whose turn only ends once cancel() is called."""

    def __init__(self):
        self.cancelled = Event()

    def complete_text(self, prompt, *, effort=None, service_tier=None):
        self.cancelled.wait(5)
        return "늦은 응답"

    def stream_text(self, prompt, *, effort=None, service_tier=None):
        self.cancelled.wait(5)
        yield from ()

    def cancel(self):
        self.cancelled.set()


class RaisingClient:
    def __init__(self, error: BaseException):
        self.error = error

    def complete_text(self, prompt, *, effort=None, service_tier=None):
        raise self.error

    def stream_text(self, prompt, *, effort=None, service_tier=None):
        raise self.error
        yield  # pragma: no cover


def _sse_payloads(response) -> list[str]:
    return [line.removeprefix("data: ") for line in response.iter_lines() if line.startswith("data: ")]


def test_invalid_reasoning_effort_returns_400_before_streaming(tmp_path: Path, monkeypatch):
    def reject(effort):
        raise ValueError(f"Unsupported reasoning effort {effort!r}")

    monkeypatch.setattr(server_module, "coerce_reasoning_effort", reject)
    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        lambda model, *, settings, auto_login: pytest.fail("client must not be created"),
    )
    client = TestClient(app)

    for stream in (False, True):
        response = client.post(
            "/v1/chat/completions",
            json={"stream": stream, "reasoning_effort": "extreme", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_reasoning_effort"


def test_transport_closed_error_maps_to_503_and_sse_error(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        lambda model, *, settings, auto_login: RaisingClient(TransportClosedError("closed")),
    )
    client = TestClient(app)

    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "codex_runtime_unavailable"

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as stream_response:
        payloads = _sse_payloads(stream_response)
    assert json.loads(payloads[-2])["error"]["code"] == "codex_runtime_unavailable"
    assert payloads[-1] == "[DONE]"


def test_unexpected_exception_in_stream_still_finishes_stream(tmp_path: Path):
    app = create_app(
        ServerSettings(state_root=tmp_path / "state"),
        lambda model, *, settings, auto_login: RaisingClient(KeyError("boom")),
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        payloads = _sse_payloads(response)
    assert json.loads(payloads[-2])["error"]["code"] == "internal_error"
    assert payloads[-1] == "[DONE]"


def test_turn_timeout_returns_504_and_cancels_turn(tmp_path: Path):
    blocking = BlockingClient()
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", turn_timeout_seconds=0.1),
        lambda model, *, settings, auto_login: blocking,
    )

    response = TestClient(app).post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "turn_timeout"
    assert blocking.cancelled.wait(1)


def test_stream_timeout_emits_error_and_cancels_turn(tmp_path: Path):
    blocking = BlockingClient()
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", turn_timeout_seconds=0.1, stream_heartbeat_seconds=0),
        lambda model, *, settings, auto_login: blocking,
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        payloads = _sse_payloads(response)

    assert json.loads(payloads[-2])["error"]["code"] == "turn_timeout"
    assert payloads[-1] == "[DONE]"
    assert blocking.cancelled.wait(1)


def test_stream_sends_heartbeat_while_waiting_for_first_token(tmp_path: Path):
    class SlowClient:
        def stream_text(self, prompt, *, effort=None, service_tier=None):
            time.sleep(0.3)
            yield "늦은 토큰"

    app = create_app(
        ServerSettings(state_root=tmp_path / "state", stream_heartbeat_seconds=0.05),
        lambda model, *, settings, auto_login: SlowClient(),
    )

    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        lines = [line for line in response.iter_lines() if line]

    assert ": ping" in lines
    content_chunks = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: {")]
    assert content_chunks[1]["choices"][0]["delta"] == {"content": "늦은 토큰"}
    assert lines[-1] == "data: [DONE]"


def test_concurrency_gate_rejects_when_queue_is_full():
    async def scenario():
        gate = _ConcurrencyGate(max_active=1, max_waiting=1)
        assert await gate.acquire()
        waiter = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        assert await gate.acquire() is False
        gate.release()
        assert await waiter
        gate.release()

    asyncio.run(scenario())


def test_overloaded_server_returns_429_with_retry_after(tmp_path: Path):
    blocking = BlockingClient()
    app = create_app(
        ServerSettings(state_root=tmp_path / "state", max_concurrent_requests=1, max_queued_requests=0),
        lambda model, *, settings, auto_login: blocking,
    )
    body = {"messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as client:
        first = Thread(target=client.post, args=("/v1/chat/completions",), kwargs={"json": body})
        first.start()
        time.sleep(0.2)
        rejected = client.post("/v1/chat/completions", json=body)
        blocking.cancel()
        first.join(5)
        accepted = client.post("/v1/chat/completions", json=body)

    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "1"
    assert rejected.json()["error"]["code"] == "server_overloaded"
    assert accepted.status_code == 200


def test_models_list_is_cached(tmp_path: Path):
    calls = []

    class FakeRuntime:
        def __init__(self, settings):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def list_models(self):
            calls.append("list")
            return ["gpt-5.5", "gpt-5.5-mini"]

    app = create_app(ServerSettings(state_root=tmp_path / "state"), codex_runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        first = client.get("/v1/models").json()
        second = client.get("/v1/models").json()

    assert first == second
    assert calls == ["list"]


def test_trusted_proxy_forwarded_client_ip_is_used_for_allowlist(tmp_path: Path):
    def make_client(trusted_proxies):
        app = create_app(
            ServerSettings(
                state_root=tmp_path / "state",
                allowed_ips=("203.0.113.10",),
                trusted_proxies=trusted_proxies,
            ),
            lambda model, *, settings, auto_login: None,
        )
        return TestClient(app, client=("172.18.0.1", 50000))

    headers = {"X-Forwarded-For": "198.51.100.99, 203.0.113.10"}
    assert make_client(()).get("/v1/models", headers=headers).status_code == 403
    assert make_client(("172.18.0.1",)).get("/v1/models", headers=headers).status_code == 200
    spoofed = {"X-Forwarded-For": "203.0.113.10, 198.51.100.99"}
    assert make_client(("172.18.0.1",)).get("/v1/models", headers=spoofed).status_code == 403


class FakeProcess:
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class ProcessBackedCodex:
    def __init__(self):
        self._client = SimpleNamespace(_proc=FakeProcess(), _reader_thread=None)
        self.closed = False
        self.thread_start_calls = []

    def thread_start(self, **kwargs):
        self.thread_start_calls.append(kwargs)
        return SimpleNamespace(
            turn=lambda prompt, **kw: SimpleNamespace(run=lambda: SimpleNamespace(final_response="재시작 응답"))
        )

    def close(self):
        self.closed = True


def test_runtime_restarts_dead_codex_process_on_next_request(tmp_path: Path):
    created: list[ProcessBackedCodex] = []

    def factory():
        codex = ProcessBackedCodex()
        created.append(codex)
        return codex

    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(state_root=tmp_path / "state"),
        codex_factory=factory,
        sandbox="read-only",
    )
    runtime.start()
    assert runtime.is_healthy()

    created[0]._client._proc.exit_code = 1
    assert not runtime.is_healthy()

    client = runtime.create_client("gpt-5.5", auto_login=False)
    assert client.complete_text("hi") == "재시작 응답"
    assert len(created) == 2
    assert created[0].closed
    assert runtime.is_healthy()
    runtime.close()


def test_health_reports_503_when_runtime_restart_fails(tmp_path: Path):
    class BrokenRuntime:
        started = True

        def __init__(self, settings):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def ensure_running(self):
            raise RuntimeError("codex app-server failed to start")

    app = create_app(ServerSettings(state_root=tmp_path / "state"), codex_runtime_factory=BrokenRuntime)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_pool_replenishment_retries_after_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(isolated_module, "_REPLENISH_RETRY_DELAYS", (0.01, 0.01))
    replenished = Event()
    attempts = []

    class FlakyCodex(ProcessBackedCodex):
        def thread_start(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 2:
                raise RuntimeError("temporary failure")
            if len(attempts) == 3:
                replenished.set()
            return super().thread_start(**kwargs)

    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(state_root=tmp_path / "state", pooled_model="gpt-5.5", thread_pool_size=1),
        codex_factory=FlakyCodex,
        sandbox="read-only",
    )
    runtime.start()
    runtime.create_client("gpt-5.5", auto_login=False).complete_text("hi")

    assert replenished.wait(2)
    deadline = time.monotonic() + 2
    while runtime._pools[("gpt-5.5", None)].qsize() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime._pools[("gpt-5.5", None)].qsize() == 1
    runtime.close()


def test_stale_pooled_thread_falls_back_to_fresh_thread(tmp_path: Path):
    class StaleThread:
        def turn(self, prompt, **kwargs):
            raise RuntimeError("thread not found")

    class StaleFirstCodex(ProcessBackedCodex):
        def thread_start(self, **kwargs):
            self.thread_start_calls.append(kwargs)
            if len(self.thread_start_calls) == 1:
                return StaleThread()
            return ProcessBackedCodex.thread_start(self, **kwargs)

    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(state_root=tmp_path / "state", pooled_model="gpt-5.5", thread_pool_size=1),
        codex_factory=StaleFirstCodex,
        sandbox="read-only",
    )
    runtime.start()

    assert runtime.create_client("gpt-5.5", auto_login=False).complete_text("hi") == "재시작 응답"
    runtime.close()


def test_secondary_pool_prewarms_fast_tier_threads(tmp_path: Path):
    codex = ProcessBackedCodex()
    runtime = IsolatedCodexRuntime(
        IsolatedCodexSettings(
            state_root=tmp_path / "state",
            pooled_model="gpt-5.5",
            thread_pool_size=0,
            secondary_pool_size=1,
        ),
        codex_factory=lambda: codex,
        sandbox="read-only",
    )
    runtime.start()
    runtime.create_client("gpt-5.5", auto_login=False).complete_text("hi", service_tier="fast")

    deadline = time.monotonic() + 2
    while runtime._pools[("gpt-5.5", "fast")].qsize() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime._pools[("gpt-5.5", "fast")].qsize() == 1
    assert all(call["service_tier"] == "fast" for call in codex.thread_start_calls)
    runtime.close()


class InterruptibleTurn:
    def __init__(self):
        self.interrupted = Event()

    def interrupt(self):
        self.interrupted.set()

    def run(self):
        self.interrupted.wait(5)
        return SimpleNamespace(final_response="")

    def stream(self):
        yield SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta="첫"))
        self.interrupted.wait(5)


class SingleTurnCodex:
    def __init__(self, turn):
        self.turn_handle = turn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def thread_start(self, **kwargs):
        return SimpleNamespace(turn=lambda prompt, **kw: self.turn_handle)


def test_client_cancel_interrupts_active_turn():
    turn = InterruptibleTurn()
    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: SingleTurnCodex(turn), sandbox="read-only")
    worker = Thread(target=client.complete_text, args=("hi",))
    worker.start()
    time.sleep(0.1)

    client.cancel()
    worker.join(2)

    assert turn.interrupted.is_set()
    assert not worker.is_alive()


def test_client_cancelled_before_start_does_not_run_turn():
    turn = InterruptibleTurn()
    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: SingleTurnCodex(turn), sandbox="read-only")
    client.cancel()

    with pytest.raises(CodexTurnCancelledError):
        client.complete_text("hi")


def test_closing_stream_generator_interrupts_turn():
    turn = InterruptibleTurn()
    client = IsolatedCodexJSONClient("gpt-5.5", codex_factory=lambda: SingleTurnCodex(turn), sandbox="read-only")
    stream = client.stream_text("hi")

    assert next(stream) == "첫"
    stream.close()

    assert turn.interrupted.is_set()


def test_single_flight_login_runs_once_for_concurrent_callers():
    gate = _SingleFlightLogin()
    started = Event()
    release = Event()
    calls = []

    def slow_login():
        calls.append("login")
        started.set()
        release.wait(2)

    first = Thread(target=gate.run, args=(slow_login,))
    first.start()
    assert started.wait(1)
    second = Thread(target=gate.run, args=(slow_login,))
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2)
    second.join(2)

    assert calls == ["login"]
