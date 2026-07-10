from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from .cli import main


def _default_state_root() -> Path:
    configured_root = os.environ.get("CODEX_OAUTH_API_STATE_ROOT")
    if configured_root is not None:
        return Path(configured_root)
    return Path.cwd() / ".codex-oauth-api-state"


def _open_log(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8", buffering=1)


def ensure_standard_streams(state_root: Path | str | None = None) -> None:
    root = Path(state_root) if state_root is not None else _default_state_root()
    if sys.stdout is None:
        sys.stdout = _open_log(root / "server.out.log")
    if sys.stderr is None:
        sys.stderr = _open_log(root / "server.err.log")


def run() -> int:
    ensure_standard_streams()
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
