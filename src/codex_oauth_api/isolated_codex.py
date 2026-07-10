from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd))

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

    def complete_text(self, prompt: str, *, effort: str | None = None) -> str:
        return self._run_codex(prompt, effort=self._coerce_reasoning_effort(effort))

    def stream_text(self, prompt: str, *, effort: str | None = None):
        yield from self._stream_codex(prompt, effort=self._coerce_reasoning_effort(effort))

    def _run_codex(self, prompt: str, *, effort=None) -> str:
        try:
            return self._run_codex_once(prompt, effort=effort)
        except RuntimeError as error:
            if self.auto_login and self._is_authentication_error(error):
                self._login_with_oauth()
                try:
                    return self._run_codex_once(prompt, effort=effort)
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _run_codex_once(self, prompt: str, *, effort=None) -> str:
        codex_factory = self.codex_factory
        sandbox = self.sandbox
        if codex_factory is None:
            codex_factory, default_sandbox = self._load_codex_sdk()
            if sandbox is None:
                sandbox = default_sandbox

        with codex_factory() as codex:
            thread = codex.thread_start(
                model=self.model,
                sandbox=sandbox,
                ephemeral=True,
                base_instructions=self.settings.base_instructions,
                developer_instructions=None,
                config={"skills": {"include_instructions": False}},
            )
            run_kwargs = {}
            if effort is not None:
                run_kwargs["effort"] = effort
            result = thread.run(prompt, **run_kwargs)
        return result.final_response

    def _stream_codex(self, prompt: str, *, effort=None):
        try:
            yield from self._stream_codex_once(prompt, effort=effort)
        except RuntimeError as error:
            if self.auto_login and self._is_authentication_error(error):
                self._login_with_oauth()
                try:
                    yield from self._stream_codex_once(prompt, effort=effort)
                    return
                except RuntimeError as retry_error:
                    raise self._map_runtime_error(retry_error) from retry_error
            raise self._map_runtime_error(error) from error

    def _stream_codex_once(self, prompt: str, *, effort=None):
        codex_factory = self.codex_factory
        sandbox = self.sandbox
        if codex_factory is None:
            codex_factory, default_sandbox = self._load_codex_sdk()
            if sandbox is None:
                sandbox = default_sandbox

        with codex_factory() as codex:
            thread = codex.thread_start(
                model=self.model,
                sandbox=sandbox,
                ephemeral=True,
                base_instructions=self.settings.base_instructions,
                developer_instructions=None,
                config={"skills": {"include_instructions": False}},
            )
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
