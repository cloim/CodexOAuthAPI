# Dotenv Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan intentionally excludes implementation code because repository instructions require planning documents to contain intent, scope, sequence, contracts, acceptance criteria, and verification steps only.

**Goal:** Load CodexOAuthAPI settings from an optional project-local `.env` file while preserving OS environment variable precedence.

**Architecture:** Use `python-dotenv` to load `.env` before `ServerSettings.from_env()` reads exact `CODEX_OAUTH_API_*` keys. Keep the settings object as the single source of runtime configuration and document safe local usage through `.env.example`.

**Tech Stack:** Python 3.12, uv, python-dotenv, FastAPI, pytest.

## Global Constraints

- `.env` is optional.
- Values already present in `os.environ` take precedence over `.env`.
- Keep exact key names only.
- Do not add fallback key lookup or legacy-key support.
- Commit `.env.example` with placeholder values only.
- Keep real `.env` files untracked.
- Use `uv add python-dotenv` for the dependency change.
- Run all Python commands through `uv run`.

---

## File Structure

- Modify `src/codex_oauth_api/server.py`: load `.env` before exact environment key reads.
- Modify `tests/test_openai_compatible_api.py`: add regression tests for `.env` loading and OS environment precedence.
- Modify `README.md`: document `.env` usage and precedence.
- Modify `pyproject.toml` and `uv.lock`: add `python-dotenv`.
- Modify `.gitignore`: ignore real `.env` files.
- Create `.env.example`: safe placeholder template.

### Task 1: Dotenv Dependency and Settings Tests

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_openai_compatible_api.py`

**Interfaces:**
- Consumes: `ServerSettings.from_env()`
- Produces: tests proving `.env` loading and OS environment precedence

- [ ] **Step 1: Add `python-dotenv` dependency with uv**

Run `uv add python-dotenv`.

- [ ] **Step 2: Add failing dotenv tests**

Add tests proving:

- `ServerSettings.from_env()` reads `CODEX_OAUTH_API_KEY` and `CODEX_OAUTH_API_ALLOWED_IPS` from `.env`.
- OS environment values override `.env` values.
- Alternate key names are ignored.

- [ ] **Step 3: Run focused tests and verify RED**

Run `uv run pytest tests/test_openai_compatible_api.py -q`.

Expected result before implementation: dotenv tests fail because `.env` is not loaded yet.

### Task 2: Dotenv Loading Implementation

**Files:**
- Modify: `src/codex_oauth_api/server.py`
- Test: `tests/test_openai_compatible_api.py`

**Interfaces:**
- Consumes: `python-dotenv.load_dotenv`
- Produces: `.env` values available to existing exact key reads

- [ ] **Step 1: Load `.env` before exact env reads**

Call `load_dotenv(override=False)` inside the settings-loading path before reading `os.environ`.

- [ ] **Step 2: Run focused tests and verify GREEN**

Run `uv run pytest tests/test_openai_compatible_api.py -q`.

Expected result after implementation: all focused tests pass.

### Task 3: Safe Local Template and Documentation

**Files:**
- Create: `.env.example`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Consumes: final dotenv behavior
- Produces: documented local setup contract

- [ ] **Step 1: Add `.env.example`**

Include safe placeholder values for `CODEX_OAUTH_API_KEY` and `CODEX_OAUTH_API_ALLOWED_IPS`.

- [ ] **Step 2: Ignore real `.env` files**

Ensure `.env` is ignored while `.env.example` remains trackable.

- [ ] **Step 3: Update README**

Document `.env` usage, OS environment precedence, and placeholder-only example values.

### Task 4: Full Verification and Commit

**Files:**
- Verify all changed files

**Interfaces:**
- Consumes: implemented dotenv support and docs
- Produces: committed feature on an implementation branch

- [ ] **Step 1: Run full verification**

Run:

- `uv run pytest -q`
- `git diff --check`

Expected result: all tests pass and whitespace check returns cleanly.

- [ ] **Step 2: Review final git state**

Run:

- `git diff --stat`
- `git status --short --branch`

Expected result: only dotenv-related files are modified.

- [ ] **Step 3: Commit implementation**

Commit dotenv implementation, tests, dependency files, `.env.example`, `.gitignore`, and README updates together.
