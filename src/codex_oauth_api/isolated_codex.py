from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread, current_thread
from typing import Any


ISOLATED_CODEX_BASE_INSTRUCTIONS = (
    "You are an isolated Codex SDK worker. "
    "Use only the current turn input and the explicit instructions inside it. "
    "Do not use session history, project instruction files, user instruction files, "
    "global instruction files, or skills."
)


_CODEX_POPEN_PATCH_LOCK = Lock()
_REPLENISH_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_CLOSE_JOIN_TIMEOUT_SECONDS = 5.0


class IsolatedCodexAuthenticationError(RuntimeError):
    pass


class CodexTurnCancelledError(RuntimeError):
    pass


def coerce_reasoning_effort(effort: str | None):
    if effort is None:
        return None
    try:
        from openai_codex.types import ReasoningEffort
    except ImportError as error:
        raise RuntimeError("openai-codex package is required") from error

    try:
        return ReasoningEffort(effort)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(f"Unsupported reasoning effort {effort!r}. Expected one of: {allowed}") from error


def _is_transport_closed_error(error: BaseException) -> bool:
    return type(error).__name__ == "TransportClosedError"


def _log_runtime_event(message: str) -> None:
    print(f"codex_oauth_api.runtime: {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class IsolatedCodexSettings:
    state_root: Path | str = field(default_factory=lambda: Path.cwd() / ".codex-oauth-api-state")
    cwd: Path | str | None = None
    base_instructions: str = ISOLATED_CODEX_BASE_INSTRUCTIONS
    pooled_model: str | None = None
    thread_pool_size: int = 0
    secondary_pool_size: int = 0
    max_secondary_pools: int = 8

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd))
        if self.thread_pool_size < 0:
            raise ValueError("thread_pool_size must be zero or greater")
        if self.secondary_pool_size < 0:
            raise ValueError("secondary_pool_size must be zero or greater")
        if self.thread_pool_size > 0 and self.pooled_model is None:
            raise ValueError("pooled_model is required when thread_pool_size is greater than zero")

    @property
    def codex_home(self) -> Path:
        return self.state_root / "codex-home"

    @property
    def app_home(self) -> Path:
        return self.state_root / "home"

    @property
    def workspace(self) -> Path:
        if self.cwd is not None:
            return self.cwd
        return self.state_root / "workspace"

    def ensure_directories(self) -> None:
        for path in [self.codex_home, self.app_home, self.workspace]:
            path.mkdir(parents=True, exist_ok=True)

    def build_codex_config(self, codex_config_class):
        self.ensure_directories()
        return codex_config_class(
            cwd=str(self.workspace),
            env={
                "CODEX_HOME": str(self.codex_home),
                "HOME": str(self.app_home),
                "USERPROFILE": str(self.app_home),
            },
        )


@dataclass(frozen=True)
class CodexOAuthLoginResult:
    verification_url: str
    user_code: str


class CodexOAuthLogin:
    def __init__(
        self,
        *,
        settings: IsolatedCodexSettings | None = None,
        codex_class=None,
        codex_config_class=None,
    ):
        self.settings = settings or IsolatedCodexSettings()
        self.codex_class = codex_class
        self.codex_config_class = codex_config_class

    def login_with_device_code(
        self,
        on_device_code: Callable[[CodexOAuthLoginResult], None] | None = None,
        on_login_complete: Callable[[], None] | None = None,
    ) -> CodexOAuthLoginResult:
        codex_class = self.codex_class
        codex_config_class = self.codex_config_class
        if codex_class is None or codex_config_class is None:
            codex_class, codex_config_class = self._load_codex_sdk()

        codex_factory = build_hidden_window_codex_factory(codex_class, codex_config_class, self.settings)
        with codex_factory() as codex:
            login = codex.login_chatgpt_device_code()
            result = CodexOAuthLoginResult(
                verification_url=login.verification_url,
                user_code=login.user_code,
            )
            if on_device_code is not None:
                on_device_code(result)
            login.wait()
            if on_login_complete is not None:
                on_login_complete()
            return result

    def _load_codex_sdk(self):
        try:
            from openai_codex import Codex, CodexConfig
        except ImportError as error:
            raise RuntimeError("openai-codex package is required") from error

        return Codex, CodexConfig


class _SingleFlightLogin:
    """Runs one device-code login at a time and lets waiting callers reuse its result."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._completed_logins = 0

    def run(self, login: Callable[[], None]) -> None:
        observed = self._completed_logins
        with self._lock:
            if self._completed_logins != observed:
                return
            login()
            self._completed_logins += 1


_OAUTH_LOGIN_GATE = _SingleFlightLogin()


class IsolatedCodexJSONClient:
    def __init__(
        self,
        model: str,
        *,
        settings: IsolatedCodexSettings | None = None,
        codex_factory=None,
        sandbox=None,
        state_root: Path | str | None = None,
        cwd: Path | str | None = None,
        base_instructions: str | None = None,
        auto_login: bool = True,
        oauth_login=None,
        on_device_code=None,
        on_login_complete=None,
        thread_factory=None,
        fresh_thread_factory=None,
    ):
        if settings is not None and any(value is not None for value in [state_root, cwd, base_instructions]):
            raise ValueError("settings cannot be combined with state_root, cwd, or base_instructions")

        self.model = model
        self.codex_factory = codex_factory
        self.sandbox = sandbox
        self.auto_login = auto_login
        self.oauth_login = oauth_login
        self.on_device_code = on_device_code
        self.on_login_complete = on_login_complete
        self.thread_factory = thread_factory
        self.fresh_thread_factory = fresh_thread_factory
        self.settings = settings or IsolatedCodexSettings(
            state_root=state_root if state_root is not None else Path.cwd() / ".codex-oauth-api-state",
            cwd=cwd,
            base_instructions=(
                base_instructions if base_instructions is not None else ISOLATED_CODEX_BASE_INSTRUCTIONS
            ),
        )
        self._turn_lock = Lock()
        self._active_turn = None
        self._cancelled = False

    def complete_json(self, instruction: str, input_text: str, schema_name: str) -> dict[str, Any]:
        prompt = self._build_prompt(instruction, input_text, schema_name)
        content = self._run_codex(prompt)
        return self._parse_json_object(content)

    def complete_text(
        self,
        prompt: str,
        *,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> str:
        return self._run_codex(
            prompt,
            effort=coerce_reasoning_effort(effort),
            service_tier=service_tier,
        )

    def stream_text(
        self,
        prompt: str,
        *,
        effort: str | None = None,
        service_tier: str | None = None,
    ):
        yield from self._stream_codex(
            prompt,
            effort=coerce_reasoning_effort(effort),
            service_tier=service_tier,
        )

    def cancel(self) -> None:
        """Interrupt the running turn, or the next one if it has not started yet."""
        with self._turn_lock:
            self._cancelled = True
            turn = self._active_turn
        if turn is not None:
            self._interrupt_quietly(turn)

    def _run_codex(self, prompt: str, *, effort=None, service_tier: str | None = None) -> str:
        try:
            return self._run_codex_once(prompt, effort=effort, service_tier=service_tier)
        except RuntimeError as error:
            if self._should_login_and_retry(error):
                self._login_with_oauth()
                try:
                    return self._run_codex_once(prompt, effort=effort, service_tier=service_tier)
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _run_codex_once(self, prompt: str, *, effort=None, service_tier: str | None = None) -> str:
        codex_factory, sandbox = self._resolve_codex_factory()
        with codex_factory() as codex:
            turn = self._start_turn(codex, sandbox, prompt, effort=effort, service_tier=service_tier)
            try:
                result = turn.run()
            finally:
                self._deactivate_turn()
        return result.final_response

    def _stream_codex(self, prompt: str, *, effort=None, service_tier: str | None = None):
        try:
            yield from self._stream_codex_once(prompt, effort=effort, service_tier=service_tier)
        except RuntimeError as error:
            if self._should_login_and_retry(error):
                self._login_with_oauth()
                try:
                    yield from self._stream_codex_once(prompt, effort=effort, service_tier=service_tier)
                    return
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _stream_codex_once(self, prompt: str, *, effort=None, service_tier: str | None = None):
        codex_factory, sandbox = self._resolve_codex_factory()
        with codex_factory() as codex:
            turn = self._start_turn(codex, sandbox, prompt, effort=effort, service_tier=service_tier)
            try:
                for event in turn.stream():
                    if event.method == "item/agentMessage/delta":
                        delta = event.payload.delta
                        if delta:
                            yield delta
            except GeneratorExit:
                self._interrupt_quietly(turn)
                raise
            finally:
                self._deactivate_turn()

    def _resolve_codex_factory(self):
        codex_factory = self.codex_factory
        sandbox = self.sandbox
        if codex_factory is None:
            codex_factory, default_sandbox = self._load_codex_sdk()
            if sandbox is None:
                sandbox = default_sandbox
        return codex_factory, sandbox

    def _start_turn(self, codex, sandbox, prompt: str, *, effort=None, service_tier: str | None = None):
        self._raise_if_cancelled()
        run_kwargs = {}
        if effort is not None:
            run_kwargs["effort"] = effort

        if self.thread_factory is None:
            thread = self._start_fresh_thread(codex, sandbox, service_tier)
            turn = thread.turn(prompt, **run_kwargs)
        else:
            thread = self.thread_factory(self.model, service_tier)
            try:
                turn = thread.turn(prompt, **run_kwargs)
            except Exception as error:
                # A prewarmed thread can go stale; retry once on a freshly started thread.
                if (
                    self.fresh_thread_factory is None
                    or self._is_authentication_error(error)
                    or _is_transport_closed_error(error)
                ):
                    raise
                self._raise_if_cancelled()
                thread = self.fresh_thread_factory(self.model, service_tier)
                turn = thread.turn(prompt, **run_kwargs)

        with self._turn_lock:
            self._active_turn = turn
            cancelled = self._cancelled
        if cancelled:
            self._interrupt_quietly(turn)
            self._deactivate_turn()
            raise CodexTurnCancelledError("Codex turn was cancelled")
        return turn

    def _start_fresh_thread(self, codex, sandbox, service_tier: str | None):
        return codex.thread_start(
            model=self.model,
            sandbox=sandbox,
            ephemeral=True,
            base_instructions=self.settings.base_instructions,
            developer_instructions=None,
            config={"skills": {"include_instructions": False}},
            service_tier=service_tier,
        )

    def _deactivate_turn(self) -> None:
        with self._turn_lock:
            self._active_turn = None

    def _raise_if_cancelled(self) -> None:
        with self._turn_lock:
            if self._cancelled:
                raise CodexTurnCancelledError("Codex turn was cancelled")

    def _interrupt_quietly(self, turn) -> None:
        interrupt = getattr(turn, "interrupt", None)
        if interrupt is None:
            return
        try:
            interrupt()
        except Exception as error:
            _log_runtime_event(f"turn interrupt failed: {error!r}")

    def _should_login_and_retry(self, error: RuntimeError) -> bool:
        return self.auto_login and not self._cancelled and self._is_authentication_error(error)

    def _login_with_oauth(self) -> None:
        oauth_login = self.oauth_login or CodexOAuthLogin(settings=self.settings)
        _OAUTH_LOGIN_GATE.run(
            lambda: oauth_login.login_with_device_code(
                self.on_device_code or self._print_device_code,
                self.on_login_complete or self._print_login_complete,
            )
        )

    def _print_device_code(self, result: CodexOAuthLoginResult) -> None:
        print("Codex OAuth login is required for the isolated CODEX_HOME.")
        print(f"Verification URL: {result.verification_url}")
        print(f"User code: {result.user_code}")
        print("Complete the login in your browser to continue.")

    def _print_login_complete(self) -> None:
        print("Codex OAuth login completed.")

    def _is_authentication_error(self, error: BaseException) -> bool:
        return "401 Unauthorized" in str(error)

    def _map_runtime_error(self, error: RuntimeError) -> RuntimeError:
        if self._is_authentication_error(error):
            return IsolatedCodexAuthenticationError(
                "Codex OAuth login is required for the isolated CODEX_HOME. "
                "Run the request again with auto_login=True to start the device-code login flow. "
                "OPENAI_API_KEY is not supported."
            )
        return error

    def _load_codex_sdk(self):
        try:
            from openai_codex import Codex, CodexConfig, Sandbox
        except ImportError as error:
            raise RuntimeError("openai-codex package is required") from error

        return self._build_hidden_window_codex_factory(Codex, CodexConfig), Sandbox.read_only

    def _build_hidden_window_codex_factory(self, codex_class, codex_config_class):
        return build_hidden_window_codex_factory(codex_class, codex_config_class, self.settings)

    def _build_prompt(self, instruction: str, input_text: str, schema_name: str) -> str:
        return "\n\n".join(
            [
                f"Instruction:\n{instruction}",
                f"JSON schema name: {schema_name}",
                "Return exactly one JSON object. Do not include markdown or prose.",
                f"Input:\n{input_text}",
            ]
        )

    def _parse_json_object(self, content: str) -> dict[str, Any]:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Codex response must be a JSON object")
        return data


def _codex_process_is_alive(codex) -> bool:
    client = getattr(codex, "_client", None)
    if client is None:
        return True
    if not hasattr(client, "_proc"):
        return True
    proc = client._proc
    if proc is None:
        return False
    poll = getattr(proc, "poll", None)
    if poll is not None and poll() is not None:
        return False
    reader = getattr(client, "_reader_thread", None)
    if reader is not None and not reader.is_alive():
        return False
    return True


PoolKey = tuple[str, str | None]


class IsolatedCodexRuntime:
    def __init__(
        self,
        settings: IsolatedCodexSettings,
        *,
        codex_factory=None,
        sandbox=None,
    ) -> None:
        self.settings = settings
        self._codex_factory = codex_factory
        self.sandbox = sandbox
        self._codex = None
        self._started = False
        self._generation = 0
        self._pools: dict[PoolKey, Queue[Any]] = {}
        self._pending: dict[PoolKey, int] = {}
        self._pool_lock = Lock()
        self._runtime_lock = Lock()
        self._replenishment_threads: set[Thread] = set()
        self._closing = False
        self._closed_event = Event()

    @property
    def _default_pool_key(self) -> PoolKey | None:
        if self.settings.pooled_model is None:
            return None
        return (self.settings.pooled_model, None)

    def start(self) -> None:
        if self._started:
            return

        if self._codex_factory is None:
            try:
                from openai_codex import Codex, CodexConfig, Sandbox
            except ImportError as error:
                raise RuntimeError("openai-codex package is required") from error
            self._codex_factory = build_hidden_window_codex_factory(Codex, CodexConfig, self.settings)
            if self.sandbox is None:
                self.sandbox = Sandbox.read_only

        self._codex = self._codex_factory()
        self._closing = False
        self._closed_event.clear()
        self._started = True
        default_key = self._default_pool_key
        if default_key is not None and self.settings.thread_pool_size > 0:
            pool: Queue[Any] = Queue()
            for _ in range(self.settings.thread_pool_size):
                pool.put(self._start_thread(*default_key))
            with self._pool_lock:
                self._pools[default_key] = pool

    @property
    def started(self) -> bool:
        return self._started

    def is_healthy(self) -> bool:
        codex = self._codex
        return self._started and codex is not None and _codex_process_is_alive(codex)

    def ensure_running(self) -> None:
        """Restart the shared Codex process when it has exited or lost its transport."""
        if not self._started:
            raise RuntimeError("Codex runtime has not been started")
        if self.is_healthy():
            return
        with self._runtime_lock:
            if self._closing:
                raise RuntimeError("Codex runtime is shutting down")
            if self.is_healthy():
                return
            previous = self._codex
            self._codex = None
            with self._pool_lock:
                self._generation += 1
                self._pools.clear()
                self._pending.clear()
            if previous is not None:
                try:
                    previous.close()
                except Exception as error:
                    _log_runtime_event(f"closing the dead Codex process failed: {error!r}")
            _log_runtime_event("restarting the Codex process")
            self._codex = self._codex_factory()
            default_key = self._default_pool_key
            if default_key is not None:
                self._top_up_pool(default_key)

    def create_client(self, model: str, *, auto_login: bool) -> IsolatedCodexJSONClient:
        self.ensure_running()
        return IsolatedCodexJSONClient(
            model,
            settings=self.settings,
            codex_factory=self._shared_codex_factory,
            sandbox=self.sandbox,
            auto_login=auto_login,
            thread_factory=self._acquire_thread,
            fresh_thread_factory=self._start_thread,
        )

    def list_models(self) -> list[str]:
        self.ensure_running()
        return [model.id for model in self._codex.models(include_hidden=False).data]

    def close(self) -> None:
        if not self._started:
            return
        with self._pool_lock:
            self._closing = True
            self._closed_event.set()
            workers = list(self._replenishment_threads)
        for worker in workers:
            worker.join(timeout=_CLOSE_JOIN_TIMEOUT_SECONDS)
        with self._runtime_lock:
            codex = self._codex
            self._codex = None
            self._started = False
            with self._pool_lock:
                self._pools.clear()
                self._pending.clear()
        if codex is not None:
            codex.close()

    def _start_thread(self, model: str | None, service_tier: str | None):
        codex = self._codex
        if codex is None:
            raise RuntimeError("Codex runtime is unavailable")
        if model is None:
            raise RuntimeError("A model is required to start a Codex thread")
        return codex.thread_start(
            model=model,
            sandbox=self.sandbox,
            ephemeral=True,
            base_instructions=self.settings.base_instructions,
            developer_instructions=None,
            config={"skills": {"include_instructions": False}},
            service_tier=service_tier,
        )

    def _pool_capacity(self, key: PoolKey) -> int:
        if key == self._default_pool_key:
            return self.settings.thread_pool_size
        return self.settings.secondary_pool_size

    def _acquire_thread(self, model: str, service_tier: str | None):
        key = (model, service_tier)
        if self._pool_capacity(key) <= 0:
            return self._start_thread(model, service_tier)

        with self._pool_lock:
            pool = self._pools.get(key)
            if pool is None:
                secondary_pools = sum(1 for existing in self._pools if existing != self._default_pool_key)
                if key != self._default_pool_key and secondary_pools >= self.settings.max_secondary_pools:
                    pool = None
                else:
                    pool = self._pools[key] = Queue()
        if pool is None:
            return self._start_thread(model, service_tier)

        try:
            thread = pool.get_nowait()
        except Empty:
            thread = None
        self._top_up_pool(key)
        if thread is None:
            return self._start_thread(model, service_tier)
        return thread

    def _top_up_pool(self, key: PoolKey) -> None:
        capacity = self._pool_capacity(key)
        with self._pool_lock:
            if self._closing:
                return
            pool = self._pools.setdefault(key, Queue())
            pending = self._pending.get(key, 0)
            missing = capacity - pool.qsize() - pending
            if missing <= 0:
                return
            self._pending[key] = pending + missing
            generation = self._generation
            for _ in range(missing):
                worker = Thread(
                    target=self._replenish_thread_pool,
                    args=(key, generation),
                    name="codex-thread-pool-replenishment",
                    daemon=True,
                )
                self._replenishment_threads.add(worker)
                worker.start()

    def _replenish_thread_pool(self, key: PoolKey, generation: int) -> None:
        try:
            for attempt, delay in enumerate((0.0, *_REPLENISH_RETRY_DELAYS)):
                if delay and self._closed_event.wait(delay):
                    return
                with self._pool_lock:
                    if self._closing or generation != self._generation:
                        return
                try:
                    thread = self._start_thread(*key)
                except Exception as error:
                    _log_runtime_event(
                        f"thread pool replenishment for {key!r} failed (attempt {attempt + 1}): {error!r}"
                    )
                    continue
                with self._pool_lock:
                    if not self._closing and generation == self._generation and key in self._pools:
                        self._pools[key].put(thread)
                return
        finally:
            with self._pool_lock:
                if generation == self._generation and key in self._pending:
                    self._pending[key] = max(0, self._pending[key] - 1)
                self._replenishment_threads.discard(current_thread())

    def _shared_codex_factory(self):
        codex = self._codex
        if codex is None:
            raise RuntimeError("Codex runtime is unavailable")
        return nullcontext(codex)


def build_hidden_window_popen(original_popen):
    def hidden_window_popen(*args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        return original_popen(*args, **kwargs)

    return hidden_window_popen


def build_hidden_window_codex_factory(codex_class, codex_config_class, settings: IsolatedCodexSettings):
    def create_codex():
        config = settings.build_codex_config(codex_config_class)

        if os.name != "nt":
            return codex_class(config)

        from openai_codex import client as codex_client

        with _CODEX_POPEN_PATCH_LOCK:
            original_popen = codex_client.subprocess.Popen
            codex_client.subprocess.Popen = build_hidden_window_popen(original_popen)
            try:
                return codex_class(config)
            finally:
                codex_client.subprocess.Popen = original_popen

    return create_codex
