from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from .isolated_codex import (
    ISOLATED_CODEX_BASE_INSTRUCTIONS,
    IsolatedCodexAuthenticationError,
    IsolatedCodexRuntime,
    IsolatedCodexSettings,
    coerce_reasoning_effort,
)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value is not None else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value is not None else default


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class ServerSettings:
    state_root: Path | str = field(default_factory=lambda: Path.cwd() / ".codex-oauth-api-state")
    default_model: str = "gpt-5.5"
    thread_pool_size: int = 4
    secondary_pool_size: int = 1
    api_key: str | None = None
    allowed_ips: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    auto_login: bool = False
    debug: bool = False
    timing: bool = False
    host: str = "127.0.0.1"
    turn_timeout_seconds: float = 600.0
    max_concurrent_requests: int = 8
    max_queued_requests: int = 32
    stream_heartbeat_seconds: float = 15.0
    models_cache_ttl_seconds: float = 300.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        object.__setattr__(self, "allowed_ips", tuple(self.allowed_ips))
        object.__setattr__(self, "trusted_proxies", tuple(self.trusted_proxies))
        for name in (
            "thread_pool_size",
            "secondary_pool_size",
            "max_concurrent_requests",
            "max_queued_requests",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be zero or greater")
        for name in ("turn_timeout_seconds", "stream_heartbeat_seconds", "models_cache_ttl_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be zero or greater")

    @property
    def workspace(self) -> Path:
        return self.state_root / "workspace"

    @classmethod
    def from_env(cls) -> ServerSettings:
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        state_root = os.environ.get("CODEX_OAUTH_API_STATE_ROOT")
        default_model = os.environ.get("CODEX_OAUTH_API_DEFAULT_MODEL")
        api_key = os.environ.get("CODEX_OAUTH_API_KEY")
        host = os.environ.get("CODEX_OAUTH_API_HOST")
        return cls(
            state_root=Path(state_root) if state_root is not None else Path.cwd() / ".codex-oauth-api-state",
            default_model=default_model if default_model is not None else "gpt-5.5",
            thread_pool_size=_env_int("CODEX_OAUTH_API_THREAD_POOL_SIZE", 4),
            secondary_pool_size=_env_int("CODEX_OAUTH_API_SECONDARY_POOL_SIZE", 1),
            api_key=api_key,
            allowed_ips=_env_list("CODEX_OAUTH_API_ALLOWED_IPS"),
            trusted_proxies=_env_list("CODEX_OAUTH_API_TRUSTED_PROXIES"),
            auto_login=os.environ.get("CODEX_OAUTH_API_AUTO_LOGIN") == "true",
            timing=os.environ.get("CODEX_OAUTH_API_TIMING_LOG") == "true",
            host=host if host is not None else "127.0.0.1",
            turn_timeout_seconds=_env_float("CODEX_OAUTH_API_TURN_TIMEOUT_SECONDS", 600.0),
            max_concurrent_requests=_env_int("CODEX_OAUTH_API_MAX_CONCURRENT_REQUESTS", 8),
            max_queued_requests=_env_int("CODEX_OAUTH_API_MAX_QUEUED_REQUESTS", 32),
            stream_heartbeat_seconds=_env_float("CODEX_OAUTH_API_STREAM_HEARTBEAT_SECONDS", 15.0),
            models_cache_ttl_seconds=_env_float("CODEX_OAUTH_API_MODELS_CACHE_TTL_SECONDS", 300.0),
        )


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[ChatMessage]
    model: str | None = None
    stream: bool = False
    reasoning_effort: str | None = None
    service_tier: Literal["fast"] | None = None


def _print_debug_log(
    event: str,
    *,
    method: str,
    path: str,
    body: Any,
    client_ip: str | None = None,
    status_code: int | None = None,
) -> None:
    record = {
        "body": body,
        "event": event,
        "method": method,
        "path": path,
    }
    if client_ip is not None:
        record["client_ip"] = client_ip
    if status_code is not None:
        record["status_code"] = status_code
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def _print_timing_log(phase: str, **durations_ms: float) -> None:
    record = {
        "event": "codex_oauth_api.timing",
        "phase": phase,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    record.update({key: round(value, 3) for key, value in durations_ms.items()})
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def _error_body(message: str, error_type: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code}}


def _map_codex_runtime_error(error: RuntimeError) -> tuple[int, str, str]:
    message = str(error)
    lower = message.lower()
    if any(keyword in lower for keyword in ("usage limit", "rate limit", "too many requests", "purchase more credits", "quota")):
        return 429, "insufficient_quota", "requests"
    return 500, "internal_error", "server_error"


def _codex_error_response(error: BaseException) -> tuple[int, dict[str, Any]]:
    if isinstance(error, IsolatedCodexAuthenticationError):
        return 503, _error_body(
            "Codex OAuth login is required for this isolated API server state.",
            "codex_authentication_required",
            "codex_authentication_required",
        )
    if isinstance(error, RuntimeError):
        status_code, error_code, error_type = _map_codex_runtime_error(error)
        return status_code, _error_body(str(error), error_type, error_code)
    if type(error).__name__ == "TransportClosedError":
        return 503, _error_body(
            "The Codex runtime connection was lost. The server restarts it on the next request.",
            "server_error",
            "codex_runtime_unavailable",
        )
    return 500, _error_body(str(error) or type(error).__name__, "server_error", "internal_error")


def _timeout_error_body(timeout_seconds: float) -> dict[str, Any]:
    return _error_body(
        f"Codex did not finish the turn within {timeout_seconds:g} seconds.",
        "timeout_error",
        "turn_timeout",
    )


def _resolve_client_ip(request: Request, trusted_proxies: tuple[str, ...]) -> str:
    peer = request.client.host if request.client is not None else ""
    if not trusted_proxies or peer not in trusted_proxies:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(hops):
        if hop not in trusted_proxies:
            return hop
    return hops[0] if hops else peer


def _cancel_in_background(codex_client: Any) -> None:
    cancel = getattr(codex_client, "cancel", None)
    if cancel is not None:
        Thread(target=cancel, name="codex-turn-cancel", daemon=True).start()


class _ConcurrencyGate:
    """Caps running Codex turns and rejects callers once the wait queue is full."""

    def __init__(self, max_active: int, max_waiting: int) -> None:
        self._semaphore = asyncio.Semaphore(max_active) if max_active > 0 else None
        self._max_waiting = max_waiting
        self._waiting = 0

    async def acquire(self) -> bool:
        if self._semaphore is None:
            return True
        if self._semaphore.locked() and self._waiting >= self._max_waiting:
            return False
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        return True

    def release(self) -> None:
        if self._semaphore is not None:
            self._semaphore.release()


class _ReleasingStreamingResponse(StreamingResponse):
    """Runs a release callback however the response ends, even before the body starts."""

    def __init__(self, *args: Any, on_close: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


def create_app(
    settings: ServerSettings | None = None,
    codex_client_factory: Callable[..., Any] | None = None,
    codex_runtime_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    settings = settings or ServerSettings()
    timing_enabled = settings.debug or settings.timing
    isolated_settings = IsolatedCodexSettings(
        state_root=settings.state_root,
        cwd=settings.workspace,
        base_instructions=ISOLATED_CODEX_BASE_INSTRUCTIONS,
        pooled_model=settings.default_model,
        thread_pool_size=settings.thread_pool_size,
        secondary_pool_size=settings.secondary_pool_size,
    )
    runtime = None
    if codex_client_factory is None:
        runtime_factory = codex_runtime_factory or IsolatedCodexRuntime
        runtime = runtime_factory(isolated_settings)
    gate = _ConcurrencyGate(settings.max_concurrent_requests, settings.max_queued_requests)
    models_cache: dict[str, Any] = {"ids": None, "expires_at": 0.0}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime is not None:
            runtime_started_at = time.perf_counter()
            await run_in_threadpool(runtime.start)
            if timing_enabled:
                _print_timing_log(
                    "runtime_startup",
                    duration_ms=(time.perf_counter() - runtime_started_at) * 1000,
                )
        try:
            yield
        finally:
            if runtime is not None:
                await run_in_threadpool(runtime.close)

    app = FastAPI(title="CodexOAuthAPI", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def enforce_v1_access_policy(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            debug_method = request.method
            debug_path = request.url.path
            client_host = _resolve_client_ip(request, settings.trusted_proxies)
            if settings.allowed_ips and client_host not in settings.allowed_ips:
                response_body = _error_body("Client IP is not allowed.", "forbidden_client_ip", "forbidden_client_ip")
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response",
                        method=debug_method,
                        path=debug_path,
                        status_code=403,
                        body=response_body,
                        client_ip=client_host,
                    )
                return JSONResponse(status_code=403, content=response_body)

            if settings.api_key is not None and not secrets.compare_digest(
                request.headers.get("authorization", "").encode("utf-8"),
                f"Bearer {settings.api_key}".encode("utf-8"),
            ):
                response_body = _error_body("Invalid or missing API key.", "invalid_request_error", "invalid_api_key")
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response",
                        method=debug_method,
                        path=debug_path,
                        status_code=401,
                        body=response_body,
                    )
                return JSONResponse(status_code=401, content=response_body)

        return await call_next(request)

    @app.get("/health")
    async def health():
        if runtime is not None and getattr(runtime, "started", False) and hasattr(runtime, "ensure_running"):
            try:
                await run_in_threadpool(runtime.ensure_running)
            except Exception as error:
                return JSONResponse(
                    status_code=503,
                    content={"status": "unavailable", "detail": str(error) or type(error).__name__},
                )
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        model_ids = [settings.default_model]
        if runtime is not None and hasattr(runtime, "list_models"):
            cached = models_cache["ids"]
            if cached is not None and time.monotonic() < models_cache["expires_at"]:
                model_ids = cached
            else:
                try:
                    listed = await run_in_threadpool(runtime.list_models)
                except Exception:
                    listed = []
                if listed:
                    model_ids = list(dict.fromkeys(listed))
                    models_cache["ids"] = model_ids
                    models_cache["expires_at"] = time.monotonic() + settings.models_cache_ttl_seconds
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "codex-oauth-api",
                }
                for model_id in model_ids
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, payload: ChatCompletionRequest):
        request_started_at = time.perf_counter()
        debug_method = request.method
        debug_path = request.url.path
        if settings.debug:
            _print_debug_log(
                "codex_oauth_api.request",
                method=debug_method,
                path=debug_path,
                body=payload.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
                client_ip=_resolve_client_ip(request, settings.trusted_proxies),
            )

        def error_response(status_code: int, response_body: dict[str, Any], headers=None) -> JSONResponse:
            if settings.debug:
                _print_debug_log(
                    "codex_oauth_api.response",
                    method=debug_method,
                    path=debug_path,
                    status_code=status_code,
                    body=response_body,
                )
            return JSONResponse(status_code=status_code, content=response_body, headers=headers)

        def log_completion(model_started_at: float) -> None:
            if timing_enabled:
                _print_timing_log(
                    "completion",
                    model_duration_ms=(time.perf_counter() - model_started_at) * 1000,
                    total_duration_ms=(time.perf_counter() - request_started_at) * 1000,
                )

        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        model = payload.model if payload.model is not None else settings.default_model
        prompt_lines = []
        for message in payload.messages:
            content = message.content
            if isinstance(content, str):
                rendered = content
            elif content is None:
                rendered = ""
            elif isinstance(content, list):
                rendered_parts = []
                for part in content:
                    if isinstance(part, dict) and "type" in part and part["type"] == "text":
                        if "text" not in part or not isinstance(part["text"], str):
                            return error_response(
                                400,
                                _error_body(
                                    "Text content parts must include a string text field.",
                                    "invalid_request_error",
                                    "invalid_content_part",
                                ),
                            )
                        rendered_parts.append(part["text"])
                    else:
                        rendered_parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
                rendered = "\n".join(rendered_parts)
            else:
                rendered = json.dumps(content, ensure_ascii=False, sort_keys=True)
            prompt_lines.append(f"{message.role}: {rendered}")
        prompt = "\n".join(prompt_lines)

        if payload.reasoning_effort is not None:
            try:
                coerce_reasoning_effort(payload.reasoning_effort)
            except ValueError as error:
                return error_response(
                    400,
                    _error_body(str(error), "invalid_request_error", "invalid_reasoning_effort"),
                )

        if not await gate.acquire():
            return error_response(
                429,
                _error_body(
                    "The server is handling too many requests. Retry shortly.",
                    "server_overloaded",
                    "server_overloaded",
                ),
                headers={"Retry-After": "1"},
            )
        released = False

        def release_gate() -> None:
            nonlocal released
            if not released:
                released = True
                gate.release()

        try:
            if codex_client_factory is not None:
                codex_client = codex_client_factory(
                    model,
                    settings=isolated_settings,
                    auto_login=settings.auto_login,
                )
            else:
                if runtime is None:
                    raise RuntimeError("Codex runtime is unavailable")
                codex_client = await run_in_threadpool(
                    runtime.create_client, model, auto_login=settings.auto_login
                )
        except Exception as error:
            release_gate()
            return error_response(*_codex_error_response(error))

        timeout_seconds = settings.turn_timeout_seconds

        if payload.stream:
            def build_chunk(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
                return {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish_reason,
                        }
                    ],
                }

            def sse(chunk: dict[str, Any]) -> str:
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response.chunk",
                        method=debug_method,
                        path=debug_path,
                        status_code=200,
                        body=chunk,
                    )
                return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            async def stream_events():
                yield sse(build_chunk({"role": "assistant"}, None))

                loop = asyncio.get_running_loop()
                queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

                def emit(item: tuple[str, Any]) -> None:
                    try:
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                    except RuntimeError:
                        pass

                def produce() -> None:
                    try:
                        for delta in codex_client.stream_text(
                            prompt,
                            effort=payload.reasoning_effort,
                            service_tier=payload.service_tier,
                        ):
                            emit(("delta", delta))
                    except BaseException as error:
                        emit(("error", error))
                    else:
                        emit(("done", None))

                model_started_at = time.perf_counter()
                deadline = model_started_at + timeout_seconds if timeout_seconds > 0 else None
                heartbeat = settings.stream_heartbeat_seconds or None
                first_delta_received = False
                finished = False
                Thread(target=produce, name="codex-stream-producer", daemon=True).start()
                try:
                    while True:
                        wait_seconds = heartbeat
                        if deadline is not None:
                            remaining = deadline - time.perf_counter()
                            wait_seconds = remaining if wait_seconds is None else min(wait_seconds, remaining)
                        try:
                            if wait_seconds is not None and wait_seconds <= 0:
                                raise TimeoutError
                            kind, value = await asyncio.wait_for(queue.get(), wait_seconds)
                        except TimeoutError:
                            if deadline is not None and time.perf_counter() >= deadline:
                                yield sse(_timeout_error_body(timeout_seconds))
                                log_completion(model_started_at)
                                yield "data: [DONE]\n\n"
                                return
                            yield ": ping\n\n"
                            continue

                        if kind == "error":
                            finished = True
                            _, error_body = _codex_error_response(value)
                            yield sse(error_body)
                            log_completion(model_started_at)
                            yield "data: [DONE]\n\n"
                            return
                        if kind == "done":
                            finished = True
                            break
                        if not first_delta_received:
                            first_delta_received = True
                            if timing_enabled:
                                _print_timing_log(
                                    "first_token",
                                    duration_ms=(time.perf_counter() - request_started_at) * 1000,
                                )
                        yield sse(build_chunk({"content": value}, None))

                    stop_chunk = sse(build_chunk({}, "stop"))
                    log_completion(model_started_at)
                    yield stop_chunk
                    yield "data: [DONE]\n\n"
                finally:
                    if not finished:
                        _cancel_in_background(codex_client)

            return _ReleasingStreamingResponse(
                stream_events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                on_close=release_gate,
            )

        model_started_at = time.perf_counter()
        try:
            completion = run_in_threadpool(
                codex_client.complete_text,
                prompt,
                effort=payload.reasoning_effort,
                service_tier=payload.service_tier,
            )
            if timeout_seconds > 0:
                content = await asyncio.wait_for(completion, timeout_seconds)
            else:
                content = await completion
        except TimeoutError:
            _cancel_in_background(codex_client)
            response = error_response(504, _timeout_error_body(timeout_seconds))
            log_completion(model_started_at)
            return response
        except Exception as error:
            response = error_response(*_codex_error_response(error))
            log_completion(model_started_at)
            return response
        finally:
            release_gate()

        response_body = {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if settings.debug:
            _print_debug_log(
                "codex_oauth_api.response",
                method=debug_method,
                path=debug_path,
                status_code=200,
                body=response_body,
            )
        log_completion(model_started_at)
        return response_body

    return app


app = create_app(ServerSettings.from_env())
