# Edge IP and Token Access Control Design

## Intent

Restrict CodexOAuthAPI usage so only requests from approved edge machine IPs with the configured bearer token can use OpenAI-compatible API endpoints.

## Scope

- Protect all `/v1/*` endpoints.
- Keep `/health` public for service health checks.
- Reuse the existing single bearer token setting.
- Add an exact allowed-IP list setting.
- Use the direct client address from FastAPI request state.

## Configuration Contract

- `CODEX_OAUTH_API_KEY`: optional bearer token required by `/v1/*` when configured.
- `CODEX_OAUTH_API_ALLOWED_IPS`: optional comma-separated list of allowed direct client IP addresses.

Each configured IP entry is trimmed. Empty entries are ignored. Matching is exact string equality against `request.client.host`.

## Request Contract

- `/health` returns its current public response without token or IP checks.
- `/v1/models` requires the configured access policy.
- `/v1/chat/completions` requires the configured access policy for both streaming and non-streaming responses.

## Access Policy

- If `CODEX_OAUTH_API_ALLOWED_IPS` is unset, IP filtering is disabled.
- If `CODEX_OAUTH_API_ALLOWED_IPS` is set and the direct client IP is absent from the list, return `403`.
- If `CODEX_OAUTH_API_KEY` is unset, bearer-token filtering is disabled.
- If `CODEX_OAUTH_API_KEY` is set and the request does not include the exact bearer token, return `401`.
- IP filtering runs before bearer-token filtering so blocked source IPs receive `403`.

## Error Contract

- IP rejection uses `403` with a clear API error code for forbidden client IP.
- Token rejection keeps the existing `401` invalid API key behavior.
- Debug logging continues to exclude request headers and secret values.

## Implementation Sequence

- Add allowed-IP parsing to `ServerSettings.from_env`.
- Move `/v1/*` access checks into shared request handling so both `/v1/models` and `/v1/chat/completions` are protected consistently.
- Keep existing chat-completion behavior unchanged after access checks pass.
- Update README configuration and request examples.

## Acceptance Criteria

- Requests to `/health` succeed without token or allowed IP.
- Requests to `/v1/models` and `/v1/chat/completions` fail with `403` when the direct client IP is not allowed.
- Requests from an allowed direct client IP fail with `401` when the configured bearer token is missing or wrong.
- Requests from an allowed direct client IP with the exact bearer token succeed.
- Existing OpenAI-compatible response and streaming tests continue to pass.

## Verification

- Run `uv run pytest -q`.
- Run `git diff --check`.
