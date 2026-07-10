# Edge IP Token Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan intentionally excludes implementation code because repository instructions require planning documents to contain intent, scope, sequence, contracts, acceptance criteria, and verification steps only.

**Goal:** Protect all `/v1/*` API endpoints so only approved direct client IPs with the configured bearer token can use the OpenAI-compatible API.

**Architecture:** Add a shared FastAPI access-control boundary for `/v1/*` paths. The boundary checks the exact direct client host from `request.client.host` before checking the existing exact bearer token. Route implementations continue to handle their existing response logic after the access policy passes.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, FastAPI TestClient, uv.

## Global Constraints

- Protect all `/v1/*` endpoints.
- Keep `/health` public for service health checks.
- Reuse the existing single bearer token setting.
- Add `CODEX_OAUTH_API_ALLOWED_IPS` as an optional comma-separated list of direct client IP addresses.
- Match allowed IP entries by exact string equality against `request.client.host`.
- Use `request.client.host` only; do not trust forwarded headers.
- If allowed IPs are configured and the direct client IP is absent from the list, return `403`.
- If the bearer token is configured and missing or wrong, return `401`.
- Run IP filtering before bearer-token filtering.
- Keep debug logs free of request headers and secret values.
- Do not add fallback key lookup or legacy-key support.
- Do not extract functions or constants unless referenced 3 or more times.

---

## File Structure

- Modify `src/codex_oauth_api/server.py`: extend `ServerSettings`, parse the allowed-IP environment value, and add shared `/v1/*` access control.
- Modify `tests/test_openai_compatible_api.py`: add regression tests for `/health`, `/v1/models`, `/v1/chat/completions`, IP rejection, token rejection, and environment parsing.
- Modify `README.md`: document `CODEX_OAUTH_API_ALLOWED_IPS` and clarify that `/v1/*` requires the configured policy while `/health` remains public.

### Task 1: Server Settings and Policy Tests

**Files:**
- Modify: `tests/test_openai_compatible_api.py`
- Modify: `src/codex_oauth_api/server.py`

**Interfaces:**
- Consumes: `ServerSettings.from_env()`
- Produces: `ServerSettings.allowed_ips: tuple[str, ...]`

- [ ] **Step 1: Add failing tests for allowed-IP parsing**

Add tests proving:

- `CODEX_OAUTH_API_ALLOWED_IPS` is parsed as a tuple of exact string entries.
- Whitespace around entries is trimmed.
- Empty comma entries are ignored.
- When the environment key is absent, `allowed_ips` is an empty tuple.
- Only the exact key `CODEX_OAUTH_API_ALLOWED_IPS` is read.

- [ ] **Step 2: Run the focused parsing tests**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result before implementation: at least one failure because `allowed_ips` does not exist yet.

- [ ] **Step 3: Implement settings support**

Update `ServerSettings` so it stores `allowed_ips` as an immutable tuple and `from_env()` reads `CODEX_OAUTH_API_ALLOWED_IPS` exactly.

- [ ] **Step 4: Run the focused parsing tests again**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result after implementation: parsing-related tests pass, with later access-control tests still pending.

### Task 2: Shared `/v1/*` Access Control

**Files:**
- Modify: `tests/test_openai_compatible_api.py`
- Modify: `src/codex_oauth_api/server.py`

**Interfaces:**
- Consumes: `ServerSettings.allowed_ips`
- Produces: shared access control for every request path beginning with `/v1/`

- [ ] **Step 1: Add failing tests for endpoint coverage**

Add tests proving:

- `/health` succeeds without token and without allowed IP.
- `/v1/models` is rejected with `403` when `allowed_ips` is configured and `request.client.host` is not listed.
- `/v1/chat/completions` is rejected with `403` under the same condition.
- `/v1/models` succeeds when `request.client.host` is listed and no bearer token is configured.
- `/v1/chat/completions` succeeds when `request.client.host` is listed and no bearer token is configured.

- [ ] **Step 2: Run focused endpoint-coverage tests**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result before implementation: `/v1/models` access-control coverage fails.

- [ ] **Step 3: Implement shared `/v1/*` access control**

Move access control out of the chat-completions route into shared request handling that applies before `/v1/*` route execution.

- [ ] **Step 4: Run focused endpoint-coverage tests again**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result after implementation: endpoint-coverage tests pass.

### Task 3: Token Ordering and Debug Safety

**Files:**
- Modify: `tests/test_openai_compatible_api.py`
- Modify: `src/codex_oauth_api/server.py`

**Interfaces:**
- Consumes: shared `/v1/*` access control
- Produces: stable `403` before `401` ordering and preserved debug-log behavior

- [ ] **Step 1: Add failing tests for ordering and token enforcement**

Add tests proving:

- When IP and bearer token both fail, the response is `403`.
- When IP passes but the configured bearer token is missing, the response is `401`.
- When IP passes but the configured bearer token is wrong, the response is `401`.
- When IP passes and the exact bearer token is present, the request succeeds.
- Debug output for access-control rejection does not include request headers or secret values.

- [ ] **Step 2: Run focused access-policy tests**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result before implementation: ordering and shared token checks fail where current route-local logic is insufficient.

- [ ] **Step 3: Complete access-policy behavior**

Ensure the shared access-control boundary returns `403` for forbidden IPs before evaluating the bearer token, and preserves the existing `401` invalid API key behavior for token failures.

- [ ] **Step 4: Run focused access-policy tests again**

Run: `uv run pytest tests/test_openai_compatible_api.py -q`

Expected result after implementation: all access-policy tests pass.

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Verify: repository test suite and whitespace checks

**Interfaces:**
- Consumes: final server behavior
- Produces: documented operator-facing configuration contract

- [ ] **Step 1: Update README configuration section**

Document:

- `CODEX_OAUTH_API_ALLOWED_IPS`
- Exact comma-separated value format
- `/v1/*` protected scope
- `/health` public scope
- Existing bearer-token usage with IP allowlisting

- [ ] **Step 2: Run full verification**

Run:

- `uv run pytest -q`
- `git diff --check`

Expected result: all tests pass and whitespace check returns cleanly.

- [ ] **Step 3: Review final diff**

Run:

- `git diff -- src/codex_oauth_api/server.py tests/test_openai_compatible_api.py README.md`
- `git status --short --branch`

Expected result: only the planned implementation files are modified.

- [ ] **Step 4: Commit implementation**

Commit only the implementation and README changes with a focused message after verification succeeds.
