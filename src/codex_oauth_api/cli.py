from __future__ import annotations

import argparse
import secrets
from collections.abc import Sequence
from dataclasses import replace

import copy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from .isolated_codex import CodexOAuthLogin, ISOLATED_CODEX_BASE_INSTRUCTIONS, IsolatedCodexSettings
from .server import ServerSettings, create_app

def build_log_config() -> dict[str, Any]:
    config = copy.deepcopy(LOGGING_CONFIG)
    time_format = "%Y-%m-%d %H:%M:%S"
    config["formatters"]["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
    config["formatters"]["default"]["datefmt"] = time_format
    config["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    config["formatters"]["access"]["datefmt"] = time_format
    return config



def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Codex OpenAI-compatible API server.")
    parser.add_argument("command", nargs="?", choices=("serve", "login", "generate-key"), default="serve")
    parser.add_argument("--host", default=None, help="Bind host. Defaults to CODEX_OAUTH_API_HOST.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument("--debug", action="store_true", help="Print request and response bodies to the console.")
    parser.add_argument("--timing", action="store_true", help="Print timing logs without request or response bodies.")
    args = parser.parse_args(argv)

    if args.command == "generate-key":
        print(f"CODEX_OAUTH_API_KEY={secrets.token_urlsafe(32)}")
        return 0

    settings = ServerSettings.from_env()
    if args.debug:
        settings = replace(settings, debug=True)
    if args.timing:
        settings = replace(settings, timing=True)
    if args.host is not None:
        settings = replace(settings, host=args.host)
    if args.command == "login":
        login_settings = IsolatedCodexSettings(
            state_root=settings.state_root,
            cwd=settings.workspace,
            base_instructions=ISOLATED_CODEX_BASE_INSTRUCTIONS,
        )
        login = CodexOAuthLogin(settings=login_settings)
        result = login.login_with_device_code(
            lambda device_code: print(
                f"Verification URL: {device_code.verification_url}\nUser code: {device_code.user_code}"
            ),
            lambda: print("Codex OAuth login completed."),
        )
        print(f"Login state stored under: {settings.state_root}")
        return 0 if result.user_code else 1

    uvicorn.run(create_app(settings), host=settings.host, port=args.port, log_config=build_log_config())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
