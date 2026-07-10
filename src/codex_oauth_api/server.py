from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from .isolated_codex import (
    ISOLATED_CODEX_BASE_INSTRUCTIONS,
    IsolatedCodexAuthenticationError,
    IsolatedCodexJSONClient,
    IsolatedCodexSettings,
)


@dataclass(frozen=True)
class ServerSettings:
    state_root: Path | str = field(default_factory=lambda: Path.cwd() / ".codex-oauth-api-state")
    default_model: str = "gpt-5.5"
    api_key: str | None = None
    allowed_ips: tuple[str, ...] = ()
    auto_login: bool = False
    debug: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        object.__setattr__(self, "allowed_ips", tuple(self.allowed_ips))

    @property
    def workspace(self) -> Path:
        return self.state_root / "workspace"

    @classmethod
    def from_env(cls) -> ServerSettings:
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        state_root = os.environ.get("CODEX_OAUTH_API_STATE_ROOT")
        default_model = os.environ.get("CODEX_OAUTH_API_DEFAULT_MODEL")
        api_key = os.environ.get("CODEX_OAUTH_API_KEY")
        allowed_ips = tuple(
            item.strip()
            for item in os.environ.get("CODEX_OAUTH_API_ALLOWED_IPS", "").split(",")
            if item.strip()
        )
        auto_login = os.environ.get("CODEX_OAUTH_API_AUTO_LOGIN") == "true"
        return cls(
            state_root=Path(state_root) if state_root is not None else Path.cwd() / ".codex-oauth-api-state",
            default_model=default_model if default_model is not None else "gpt-5.5",
            api_key=api_key,
            allowed_ips=allowed_ips,
            auto_login=auto_login,
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
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def create_app(
    settings: ServerSettings | None = None,
    codex_client_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    settings = settings or ServerSettings()

    app = FastAPI(title="CodexOAuthAPI", version="0.1.0")

    @app.middleware("http")
    async def enforce_v1_access_policy(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            debug_method = request.method
            debug_path = request.url.path
            client_host = request.client.host if request.client is not None else ""
            if settings.allowed_ips and client_host not in settings.allowed_ips:
                response_body = {
                    "error": {
                        "message": "Client IP is not allowed.",
                        "type": "forbidden_client_ip",
                        "code": "forbidden_client_ip",
                    }
                }
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response",
                        method=debug_method,
                        path=debug_path,
                        status_code=403,
                        body=response_body,
                    )
                return JSONResponse(
                    status_code=403,
                    content=response_body,
                )

            if settings.api_key is not None and request.headers.get("authorization") != f"Bearer {settings.api_key}":
                response_body = {
                    "error": {
                        "message": "Invalid or missing API key.",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                }
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response",
                        method=debug_method,
                        path=debug_path,
                        status_code=401,
                        body=response_body,
                    )
                return JSONResponse(
                    status_code=401,
                    content=response_body,
                )

        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": settings.default_model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "codex-oauth-api",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, payload: ChatCompletionRequest):
        debug_method = request.method
        debug_path = request.url.path
        if settings.debug:
            _print_debug_log(
                "codex_oauth_api.request",
                method=debug_method,
                path=debug_path,
                body=payload.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
                client_ip=request.client.host if request.client is not None else "",
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
                            response_body = {
                                "error": {
                                    "message": "Text content parts must include a string text field.",
                                    "type": "invalid_request_error",
                                    "code": "invalid_content_part",
                                }
                            }
                            if settings.debug:
                                _print_debug_log(
                                    "codex_oauth_api.response",
                                    method=debug_method,
                                    path=debug_path,
                                    status_code=400,
                                    body=response_body,
                                )
                            return JSONResponse(
                                status_code=400,
                                content=response_body,
                            )
                        rendered_parts.append(part["text"])
                    else:
                        rendered_parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
                rendered = "\n".join(rendered_parts)
            else:
                rendered = json.dumps(content, ensure_ascii=False, sort_keys=True)
            prompt_lines.append(f"{message.role}: {rendered}")
        prompt = "\n".join(prompt_lines)

        isolated_settings = IsolatedCodexSettings(
            state_root=settings.state_root,
            cwd=settings.workspace,
            base_instructions=ISOLATED_CODEX_BASE_INSTRUCTIONS,
        )
        factory = codex_client_factory or IsolatedCodexJSONClient
        codex_client = factory(model, settings=isolated_settings, auto_login=settings.auto_login)

        if payload.stream:
            def next_stream_delta(iterator):
                try:
                    return True, next(iterator)
                except StopIteration:
                    return False, None

            iterator = codex_client.stream_text(prompt, effort=payload.reasoning_effort)
            try:
                has_first_delta, first_delta = await run_in_threadpool(next_stream_delta, iterator)
            except IsolatedCodexAuthenticationError:
                response_body = {
                    "error": {
                        "message": "Codex OAuth login is required for this isolated API server state.",
                        "type": "codex_authentication_required",
                        "code": "codex_authentication_required",
                    }
                }
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response",
                        method=debug_method,
                        path=debug_path,
                        status_code=503,
                        body=response_body,
                    )
                return JSONResponse(
                    status_code=503,
                    content=response_body,
                )

            role_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            stop_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }

            def print_stream_chunk_debug(chunk: dict[str, Any]) -> None:
                if settings.debug:
                    _print_debug_log(
                        "codex_oauth_api.response.chunk",
                        method=debug_method,
                        path=debug_path,
                        status_code=200,
                        body=chunk,
                    )

            async def stream_events():
                print_stream_chunk_debug(role_chunk)
                yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

                if has_first_delta and first_delta is not None:
                    first_content_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": first_delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    print_stream_chunk_debug(first_content_chunk)
                    yield f"data: {json.dumps(first_content_chunk, ensure_ascii=False)}\n\n"

                while True:
                    has_delta, delta = await run_in_threadpool(next_stream_delta, iterator)
                    if not has_delta:
                        break
                    content_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    print_stream_chunk_debug(content_chunk)
                    yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"

                print_stream_chunk_debug(stop_chunk)
                yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream_events(), media_type="text/event-stream")

        try:
            content = await run_in_threadpool(
                codex_client.complete_text,
                prompt,
                effort=payload.reasoning_effort,
            )
        except IsolatedCodexAuthenticationError:
            response_body = {
                "error": {
                    "message": "Codex OAuth login is required for this isolated API server state.",
                    "type": "codex_authentication_required",
                    "code": "codex_authentication_required",
                }
            }
            if settings.debug:
                _print_debug_log(
                    "codex_oauth_api.response",
                    method=debug_method,
                    path=debug_path,
                    status_code=503,
                    body=response_body,
                )
            return JSONResponse(
                status_code=503,
                content=response_body,
            )

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
        return response_body

    return app


app = create_app(ServerSettings.from_env())
