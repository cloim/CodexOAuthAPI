from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, current_thread
from typing import Any


ISOLATED_CODEX_BASE_INSTRUCTIONS = (
    "You are an isolated Codex SDK worker. "
    "Use only the current turn input and the explicit instructions inside it. "
    "Do not use session history, project instruction files, user instruction files, "
    "global instruction files, or skills."
)


_CODEX_POPEN_PATCH_LOCK = Lock()


class IsolatedCodexAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IsolatedCodexSettings:
    state_root: Path | str = field(default_factory=lambda: Path.cwd() / ".codex-oauth-api-state")
    cwd: Path | str | None = None
    base_instructions: str = ISOLATED_CODEX_BASE_INSTRUCTIONS
    pooled_model: str | None = None
    thread_pool_size: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd))
        if self.thread_pool_size < 0:
            raise ValueError("thread_pool_size must be zero or greater")
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
        self.settings = settings or IsolatedCodexSettings(
            state_root=state_root if state_root is not None else Path.cwd() / ".codex-oauth-api-state",
            cwd=cwd,
            base_instructions=(
                base_instructions if base_instructions is not None else ISOLATED_CODEX_BASE_INSTRUCTIONS
            ),
        )

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
            effort=self._coerce_reasoning_effort(effort),
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
            effort=self._coerce_reasoning_effort(effort),
            service_tier=service_tier,
        )

    def _run_codex(self, prompt: str, *, effort=None, service_tier: str | None = None) -> str:
        try:
            return self._run_codex_once(prompt, effort=effort, service_tier=service_tier)
        except RuntimeError as error:
            if self.auto_login and self._is_authentication_error(error):
                self._login_with_oauth()
                try:
                    return self._run_codex_once(prompt, effort=effort, service_tier=service_tier)
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _run_codex_once(self, prompt: str, *, effort=None, service_tier: str | None = None) -> str:
        codex_factory = self.codex_factory
        sandbox = self.sandbox
        if codex_factory is None:
            codex_factory, default_sandbox = self._load_codex_sdk()
            if sandbox is None:
                sandbox = default_sandbox

        with codex_factory() as codex:
            if self.thread_factory is None:
                thread = codex.thread_start(
                    model=self.model,
                    sandbox=sandbox,
                    ephemeral=True,
                    base_instructions=self.settings.base_instructions,
                    developer_instructions=None,
                    config={"skills": {"include_instructions": False}},
                    service_tier=service_tier,
                )
            else:
                thread = self.thread_factory(self.model, service_tier)
            run_kwargs = {}
            if effort is not None:
                run_kwargs["effort"] = effort
            result = thread.run(prompt, **run_kwargs)
        return result.final_response

    def _stream_codex(self, prompt: str, *, effort=None, service_tier: str | None = None):
        try:
            yield from self._stream_codex_once(prompt, effort=effort, service_tier=service_tier)
        except RuntimeError as error:
            if self.auto_login and self._is_authentication_error(error):
                self._login_with_oauth()
                try:
                    yield from self._stream_codex_once(prompt, effort=effort, service_tier=service_tier)
                    return
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _stream_codex_once(self, prompt: str, *, effort=None, service_tier: str | None = None):
        codex_factory = self.codex_factory
        sandbox = self.sandbox
        if codex_factory is None:
            codex_factory, default_sandbox = self._load_codex_sdk()
            if sandbox is None:
                sandbox = default_sandbox

        with codex_factory() as codex:
            if self.thread_factory is None:
                thread = codex.thread_start(
                    model=self.model,
                    sandbox=sandbox,
                    ephemeral=True,
                    base_instructions=self.settings.base_instructions,
                    developer_instructions=None,
                    config={"skills": {"include_instructions": False}},
                    service_tier=service_tier,
                )
            else:
                thread = self.thread_factory(self.model, service_tier)
            run_kwargs = {}
            if effort is not None:
                run_kwargs["effort"] = effort
            turn = thread.turn(prompt, **run_kwargs)
            for event in turn.stream():
                if event.method == "item/agentMessage/delta":
                    delta = event.payload.delta
                    if delta:
                        yield delta

    def _coerce_reasoning_effort(self, effort: str | None):
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

    def _login_with_oauth(self) -> None:
        oauth_login = self.oauth_login or CodexOAuthLogin(settings=self.settings)
        oauth_login.login_with_device_code(
            self.on_device_code or self._print_device_code,
            self.on_login_complete or self._print_login_complete,
        )

    def _print_device_code(self, result: CodexOAuthLoginResult) -> None:
        print("Codex OAuth login is required for the isolated CODEX_HOME.")
        print(f"Verification URL: {result.verification_url}")
        print(f"User code: {result.user_code}")
        print("Complete the login in your browser to continue.")

    def _print_login_complete(self) -> None:
        print("Codex OAuth login completed.")

    def _is_authentication_error(self, error: RuntimeError) -> bool:
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
        self._thread_pool: Queue[Any] = Queue()
        self._pool_lock = Lock()
        self._replenishment_threads: set[Thread] = set()
        self._closing = False

    def start(self) -> None:
        if self._codex is not None:
            return

        factory = self._codex_factory
        if factory is None:
            try:
                from openai_codex import Codex, CodexConfig, Sandbox
            except ImportError as error:
                raise RuntimeError("openai-codex package is required") from error
            factory = build_hidden_window_codex_factory(Codex, CodexConfig, self.settings)
            if self.sandbox is None:
                self.sandbox = Sandbox.read_only

        self._codex = factory()
        self._closing = False
        for _ in range(self.settings.thread_pool_size):
            self._thread_pool.put(self._start_thread(self.settings.pooled_model, None))

    def create_client(self, model: str, *, auto_login: bool) -> IsolatedCodexJSONClient:
        if self._codex is None:
            raise RuntimeError("Codex runtime has not been started")
        return IsolatedCodexJSONClient(
            model,
            settings=self.settings,
            codex_factory=self._shared_codex_factory,
            sandbox=self.sandbox,
            auto_login=auto_login,
            thread_factory=self._acquire_thread,
        )

    def close(self) -> None:
        if self._codex is None:
            return
        with self._pool_lock:
            self._closing = True
            workers = list(self._replenishment_threads)
        for worker in workers:
            worker.join()
        codex = self._codex
        self._codex = None
        while True:
            try:
                self._thread_pool.get_nowait()
            except Empty:
                break
        codex.close()

    def _start_thread(self, model: str | None, service_tier: str | None):
        if self._codex is None:
            raise RuntimeError("Codex runtime has not been started")
        if model is None:
            raise RuntimeError("A model is required to start a Codex thread")
        return self._codex.thread_start(
            model=model,
            sandbox=self.sandbox,
            ephemeral=True,
            base_instructions=self.settings.base_instructions,
            developer_instructions=None,
            config={"skills": {"include_instructions": False}},
            service_tier=service_tier,
        )

    def _acquire_thread(self, model: str, service_tier: str | None):
        uses_pool = (
            self.settings.thread_pool_size > 0
            and model == self.settings.pooled_model
            and service_tier is None
        )
        if not uses_pool:
            return self._start_thread(model, service_tier)
        try:
            thread = self._thread_pool.get_nowait()
        except Empty:
            return self._start_thread(model, None)
        self._schedule_replenishment()
        return thread

    def _schedule_replenishment(self) -> None:
        with self._pool_lock:
            if self._closing:
                return
            worker = Thread(
                target=self._replenish_thread_pool,
                name="codex-thread-pool-replenishment",
                daemon=True,
            )
            self._replenishment_threads.add(worker)
            worker.start()

    def _replenish_thread_pool(self) -> None:
        try:
            thread = self._start_thread(self.settings.pooled_model, None)
            with self._pool_lock:
                if not self._closing:
                    self._thread_pool.put(thread)
        finally:
            with self._pool_lock:
                self._replenishment_threads.discard(current_thread())

    def _shared_codex_factory(self):
        if self._codex is None:
            raise RuntimeError("Codex runtime has not been started")
        return nullcontext(self._codex)


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
