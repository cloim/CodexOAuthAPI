from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import TextIO

from codex_oauth_api import main as app_main


def test_ensure_standard_streams_uses_state_logs_when_streams_are_missing(tmp_path: Path):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    created_streams: list[TextIO] = []

    try:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]

        app_main.ensure_standard_streams(tmp_path)
        assert sys.stdout is not None
        assert sys.stderr is not None
        created_streams.extend([sys.stdout, sys.stderr])

        print("out-line")
        sys.stderr.write("err-line\n")
        sys.stdout.flush()
        sys.stderr.flush()

        assert (tmp_path / "server.out.log").read_text(encoding="utf-8") == "out-line\n"
        assert (tmp_path / "server.err.log").read_text(encoding="utf-8") == "err-line\n"
    finally:
        for stream in created_streams:
            stream.close()
        sys.stdout = original_stdout
        sys.stderr = original_stderr


def test_ensure_standard_streams_keeps_existing_streams(tmp_path: Path):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stdout = io.StringIO()
    stderr = io.StringIO()

    try:
        sys.stdout = stdout
        sys.stderr = stderr

        app_main.ensure_standard_streams(tmp_path)

        assert sys.stdout is stdout
        assert sys.stderr is stderr
        assert not (tmp_path / "server.out.log").exists()
        assert not (tmp_path / "server.err.log").exists()
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
