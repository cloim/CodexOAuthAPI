from __future__ import annotations

import argparse
import secrets
from collections.abc import Sequence
from dataclasses import replace

import uvicorn

from .isolated_codex import CodexOAuthLogin, ISOLATED_CODEX_BASE_INSTRUCTIONS, IsolatedCodexSettings
from .server import ServerSettings, create_app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Codex OpenAI-compatible API server.")
    parser.add_argument("command", nargs="?", choices=("serve", "login", "generate-key"), default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="Print request and response bodies to the console.")
    args = parser.parse_args(argv)

    if args.command == "generate-key":
        print(f"CODEX_OAUTH_API_KEY={secrets.token_urlsafe(32)}")
        return 0

    settings = ServerSettings.from_env()
    if args.debug:
        settings = replace(settings, debug=True)
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

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
