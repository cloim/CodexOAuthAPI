from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_does_not_depend_on_excodex_path_source():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"excodex"' not in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert "../EXCodex" not in pyproject
    assert "editable = true" not in pyproject


def test_runtime_code_does_not_import_excodex_package():
    source_files = list((PROJECT_ROOT / "src").rglob("*.py"))
    assert source_files

    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in source_files
        if "excodex" in path.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []
