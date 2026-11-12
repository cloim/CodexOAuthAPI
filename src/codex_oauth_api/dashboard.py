from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .isolated_codex import CodexOAuthLogin, IsolatedCodexSettings
from .logbuffer import LOG_BUFFER

STATIC_DIR = Path(__file__).parent / "static"
_LOGIN_CODE_WAIT_SECONDS = 30.0
_SSE_POLL_SECONDS = 0.5
_SSE_HEARTBEAT_SECONDS = 15.0
_API_KEY_ENV = "CODEX_OAUTH_API_KEY"


@dataclass(frozen=True)
class _SettingSpec:
    env: str
    kind: str  # str | int | float | bool | list
    description: str
    secret: bool = False


EDITABLE_SETTINGS: tuple[_SettingSpec, ...] = (
    _SettingSpec(_API_KEY_ENV, "str", "/v1/* 와 /api/* 에 필요한 Bearer 토큰", secret=True),
    _SettingSpec("CODEX_OAUTH_API_ALLOWED_IPS", "list", "허용 클라이언트 IP 목록 (쉼표 구분)"),
    _SettingSpec("CODEX_OAUTH_API_TRUSTED_PROXIES", "list", "X-Forwarded-For 를 신뢰할 프록시 IP 목록"),
    _SettingSpec("CODEX_OAUTH_API_HOST", "str", "serve 명령의 바인드 호스트"),
    _SettingSpec("CODEX_OAUTH_API_DEFAULT_MODEL", "str", "요청에 모델이 없을 때 사용하는 모델"),
    _SettingSpec("CODEX_OAUTH_API_THREAD_POOL_SIZE", "int", "기본 모델용 사전 생성 스레드 수"),
    _SettingSpec("CODEX_OAUTH_API_SECONDARY_POOL_SIZE", "int", "다른 모델 또는 fast 티어용 스레드 수"),
    _SettingSpec("CODEX_OAUTH_API_MAX_CONCURRENT_REQUESTS", "int", "동시 처리 요청 수 (0 은 제한 없음)"),
    _SettingSpec("CODEX_OAUTH_API_MAX_QUEUED_REQUESTS", "int", "대기열 최대 길이"),
    _SettingSpec("CODEX_OAUTH_API_TURN_TIMEOUT_SECONDS", "float", "턴 타임아웃 초 (0 은 비활성)"),
    _SettingSpec("CODEX_OAUTH_API_STREAM_HEARTBEAT_SECONDS", "float", "SSE ping 간격 초 (0 은 비활성)"),
    _SettingSpec("CODEX_OAUTH_API_MODELS_CACHE_TTL_SECONDS", "float", "/v1/models 캐시 유지 시간 초"),
    _SettingSpec("CODEX_OAUTH_API_AUTO_LOGIN", "bool", "요청 시 자동 Device Code 로그인 허용"),
    _SettingSpec("CODEX_OAUTH_API_DEBUG", "bool", "요청과 응답 본문 로그 출력"),
    _SettingSpec("CODEX_OAUTH_API_TIMING_LOG", "bool", "본문 없는 타이밍 로그 출력"),
)
_SPEC_BY_ENV = {spec.env: spec for spec in EDITABLE_SETTINGS}


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-2:]}"


def _mask_identifier(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def read_auth_summary(auth_path: Path) -> dict[str, Any]:
    """Summarises auth.json without ever returning token values."""
    summary: dict[str, Any] = {
        "authenticated": False,
        "account_id": None,
        "email": None,
        "last_refresh": None,
    }
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return summary
    if not isinstance(data, dict):
        return summary
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    if not tokens.get("access_token") and not data.get("OPENAI_API_KEY"):
        return summary
    summary["authenticated"] = True
    summary["account_id"] = _mask_identifier(tokens.get("account_id"))
    email = _decode_jwt_claims(tokens.get("id_token")).get("email")
    summary["email"] = email if isinstance(email, str) else None
    last_refresh = data.get("last_refresh")
    if not isinstance(last_refresh, str):
        try:
            last_refresh = datetime.fromtimestamp(auth_path.stat().st_mtime).astimezone().isoformat()
        except OSError:
            last_refresh = None
    summary["last_refresh"] = last_refresh
    return summary


class _LoginTracker:
    """Runs the device-code login on a background thread and exposes its progress."""

    def __init__(self, login_factory: Callable[[IsolatedCodexSettings], Any], isolated: IsolatedCodexSettings) -> None:
        self._login_factory = login_factory
        self._isolated = isolated
        self._lock = Lock()
        self._state: dict[str, Any] = {"status": "idle"}
        self._code_ready = Event()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state["status"] in ("starting", "waiting"):
                current = dict(self._state)
                code_ready = self._code_ready
            else:
                self._state = {"status": "starting", "started_at": datetime.now().astimezone().isoformat()}
                self._code_ready = code_ready = Event()
                current = None
        if current is None:
            LOG_BUFFER.add("info", "auth", "Device Code 로그인을 시작했어요.")
            Thread(target=self._run, args=(code_ready,), name="codex-dashboard-login", daemon=True).start()
        code_ready.wait(_LOGIN_CODE_WAIT_SECONDS)
        return self.snapshot()

    def reset(self) -> None:
        with self._lock:
            if self._state["status"] not in ("starting", "waiting"):
                self._state = {"status": "idle"}

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _run(self, code_ready: Event) -> None:
        def on_device_code(result: Any) -> None:
            self._update(status="waiting", verification_url=result.verification_url, user_code=result.user_code)
            LOG_BUFFER.add("info", "auth", "Device Code를 발급했어요. 인증 완료를 기다리는 중이에요.")
            code_ready.set()

        def on_complete() -> None:
            LOG_BUFFER.add("info", "auth", "Codex OAuth 로그인이 완료되었어요.")

        try:
            self._login_factory(self._isolated).login_with_device_code(on_device_code, on_complete)
        except BaseException as error:
            self._update(status="failed", error=str(error) or type(error).__name__)
            LOG_BUFFER.add("error", "auth", f"Codex OAuth 로그인에 실패했어요: {error}")
        else:
            self._update(status="completed", completed_at=datetime.now().astimezone().isoformat())
        finally:
            code_ready.set()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _format_env_value(value: str) -> str:
    return f'"{value}"' if any(char in value for char in " #") else value


def _write_env_file(path: Path, updates: dict[str, str | None]) -> None:
    """Applies updates in place, keeping unrelated lines; None removes the key."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        key = line.strip().partition("=")[0].strip()
        if "=" in line and key in pending:
            value = pending.pop(key)
            if value is not None:
                output.append(f"{key}={_format_env_value(value)}")
        else:
            output.append(line)
    for key, value in pending.items():
        if value is not None:
            output.append(f"{key}={_format_env_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _validate_setting(spec: _SettingSpec, raw: str) -> str:
    value = raw.strip()
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        raise ValueError("줄바꿈과 따옴표는 사용할 수 없어요.")
    if value == "":
        return value
    try:
        if spec.kind == "int":
            if int(value) < 0:
                raise ValueError
        elif spec.kind == "float":
            if float(value) < 0:
                raise ValueError
        elif spec.kind == "bool":
            if value not in ("true", "false"):
                raise ValueError
            return value
        elif spec.kind == "list":
            return ",".join(item.strip() for item in value.split(",") if item.strip())
    except ValueError:
        expected = {"int": "0 이상의 정수", "float": "0 이상의 숫자", "bool": "true 또는 false"}[spec.kind]
        raise ValueError(f"{expected}여야 해요.") from None
    return value


def _effective_value(settings: Any, env: str) -> str:
    mapping: dict[str, Any] = {
        _API_KEY_ENV: settings.api_key,
        "CODEX_OAUTH_API_ALLOWED_IPS": ",".join(settings.allowed_ips),
        "CODEX_OAUTH_API_TRUSTED_PROXIES": ",".join(settings.trusted_proxies),
        "CODEX_OAUTH_API_HOST": settings.host,
        "CODEX_OAUTH_API_DEFAULT_MODEL": settings.default_model,
        "CODEX_OAUTH_API_THREAD_POOL_SIZE": settings.thread_pool_size,
        "CODEX_OAUTH_API_SECONDARY_POOL_SIZE": settings.secondary_pool_size,
        "CODEX_OAUTH_API_MAX_CONCURRENT_REQUESTS": settings.max_concurrent_requests,
        "CODEX_OAUTH_API_MAX_QUEUED_REQUESTS": settings.max_queued_requests,
        "CODEX_OAUTH_API_TURN_TIMEOUT_SECONDS": settings.turn_timeout_seconds,
        "CODEX_OAUTH_API_STREAM_HEARTBEAT_SECONDS": settings.stream_heartbeat_seconds,
        "CODEX_OAUTH_API_MODELS_CACHE_TTL_SECONDS": settings.models_cache_ttl_seconds,
        "CODEX_OAUTH_API_AUTO_LOGIN": settings.auto_login,
        "CODEX_OAUTH_API_DEBUG": settings.debug,
        "CODEX_OAUTH_API_TIMING_LOG": settings.timing,
    }
    value = mapping[env]
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def register_dashboard(
    app: FastAPI,
    *,
    settings: Any,
    isolated_settings: IsolatedCodexSettings,
    login_factory: Callable[[IsolatedCodexSettings], Any] | None = None,
) -> None:
    auth_path = isolated_settings.codex_home / "auth.json"
    env_path = Path(settings.env_file) if getattr(settings, "env_file", None) else Path.cwd() / ".env"
    tracker = _LoginTracker(login_factory or (lambda s: CodexOAuthLogin(settings=s)), isolated_settings)

    def read_index() -> HTMLResponse:
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=404, detail="Dashboard is not installed.") from None
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return read_index()

    # Auth

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, Any]:
        summary = await asyncio.to_thread(read_auth_summary, auth_path)
        login = tracker.snapshot()
        if summary["authenticated"] and login["status"] in ("completed", "failed"):
            tracker.reset()
            login = tracker.snapshot()
        return {**summary, "login": login}

    @app.post("/api/auth/login")
    async def auth_login() -> dict[str, Any]:
        login = await asyncio.to_thread(tracker.start)
        if login["status"] not in ("waiting", "completed"):
            raise HTTPException(status_code=502, detail=login.get("error") or "Device Code를 받지 못했어요.")
        return {"login": login}

    @app.post("/api/auth/logout")
    async def auth_logout() -> dict[str, Any]:
        removed = False
        try:
            auth_path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"auth.json을 삭제하지 못했어요: {error.strerror}") from None
        tracker.reset()
        LOG_BUFFER.add("info", "auth", "인증 정보를 정리했어요." if removed else "정리할 인증 정보가 없었어요.")
        return {"authenticated": False, "removed": removed}

    # Logs

    @app.get("/api/logs")
    async def list_logs(
        q: str | None = None,
        level: str | None = None,
        type: str | None = None,
        after: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=500),
    ) -> dict[str, Any]:
        records = LOG_BUFFER.list(query=q, level=level, log_type=type, after=after, limit=limit)
        return {"data": records}

    @app.delete("/api/logs")
    async def clear_logs() -> dict[str, Any]:
        LOG_BUFFER.clear()
        return {"cleared": True}

    @app.get("/api/logs/stream")
    async def stream_logs(
        request: Request,
        q: str | None = None,
        level: str | None = None,
        type: str | None = None,
        after: int = Query(0, ge=0),
        follow: bool = True,
    ) -> StreamingResponse:
        async def events():
            cursor = after
            idle = 0.0
            while True:
                latest = LOG_BUFFER.list(after=cursor, limit=1)
                upper = latest[-1]["id"] if latest else cursor
                records = [
                    record
                    for record in LOG_BUFFER.list(query=q, level=level, log_type=type, after=cursor)
                    if record["id"] <= upper
                ]
                cursor = upper
                for record in records:
                    yield f"id: {record['id']}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
                if not follow:
                    return
                if await request.is_disconnected():
                    return
                idle = 0.0 if records else idle + _SSE_POLL_SECONDS
                if idle >= _SSE_HEARTBEAT_SECONDS:
                    idle = 0.0
                    yield ": ping\n\n"
                await asyncio.sleep(_SSE_POLL_SECONDS)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Settings

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        file_values = _read_env_file(env_path)
        items = []
        for spec in EDITABLE_SETTINGS:
            stored = file_values.get(spec.env, "")
            effective = _effective_value(settings, spec.env)
            items.append(
                {
                    "name": spec.env,
                    "kind": spec.kind,
                    "description": spec.description,
                    "secret": spec.secret,
                    "value": mask_secret(stored) if spec.secret else stored,
                    "effective": mask_secret(effective) if spec.secret else effective,
                    "is_set": bool(stored),
                }
            )
        return {
            "items": items,
            "env_file": str(env_path),
            "state_root": str(settings.state_root),
            "restart_required": True,
        }

    @app.put("/api/settings")
    async def update_settings(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values 객체가 필요해요.")
        stored = _read_env_file(env_path)
        updates: dict[str, str | None] = {}
        for name, raw in values.items():
            spec = _SPEC_BY_ENV.get(name)
            if spec is None:
                raise HTTPException(status_code=400, detail=f"수정할 수 없는 설정이에요: {name}")
            if not isinstance(raw, str):
                raise HTTPException(status_code=400, detail=f"{name} 값은 문자열이어야 해요.")
            if spec.secret and raw == mask_secret(stored.get(name)) and name in stored:
                continue
            try:
                cleaned = _validate_setting(spec, raw)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=f"{name}: {error}") from None
            updates[name] = cleaned or None
        if updates:
            try:
                await asyncio.to_thread(_write_env_file, env_path, updates)
            except OSError as error:
                raise HTTPException(status_code=500, detail=f".env를 저장하지 못했어요: {error.strerror}") from None
            LOG_BUFFER.add("info", "system", f"설정을 저장했어요: {', '.join(sorted(updates))}")
        return JSONResponse({"saved": sorted(updates), "restart_required": bool(updates)})

    @app.post("/api/settings/generate-key")
    async def generate_key() -> dict[str, str]:
        return {"api_key": secrets.token_urlsafe(32)}
