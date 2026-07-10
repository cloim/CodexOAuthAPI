# CodexOAuthAPI

> OpenAI-compatible API server powered by an isolated Codex OAuth session.

[Korean README](README.ko.md)

CodexOAuthAPI gives local tools, scripts, and edge machines an OpenAI-style
`/v1/chat/completions` API while keeping Codex login state isolated inside this
project. Point an OpenAI-compatible client at this server, authenticate with a
local bearer key, and use Codex through a small FastAPI gateway.

## Why Try It?

- **OpenAI-compatible shape**: `POST /v1/chat/completions`, `GET /v1/models`,
  and streaming SSE responses.
- **No global Codex bleed-through**: `CODEX_HOME`, `HOME`, `USERPROFILE`, and
  workspace paths are isolated under the project state directory.
- **OAuth instead of API keys for Codex**: log in once with the Codex device-code
  flow; the server reuses that isolated local state.
- **Edge-friendly access control**: protect `/v1/*` with `Authorization: Bearer`
  and a direct client IP allowlist.
- **Drop-in local config**: use `.env` for local runs, with OS environment
  variables taking precedence.
- **Debuggable without leaking auth**: debug logs include request/response
  bodies and client IP, while auth headers stay out of the log.

## Quick Start

```powershell
uv sync
uv run codex-oauth-api login
uv run codex-oauth-api generate-key
```

Create `.env` from the example:

```env
CODEX_OAUTH_API_KEY=replace-with-generated-key
CODEX_OAUTH_API_ALLOWED_IPS=127.0.0.1
```

Then start the server:

```powershell
uv run codex-oauth-api serve --host 127.0.0.1 --port 8000
```

Try a request:

```powershell
curl.exe http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer replace-with-generated-key" `
  -d '{ "model": "gpt-5.5", "messages": [{ "role": "user", "content": "Hello from CodexOAuthAPI" }] }'
```

## Streaming

Set `stream: true` to receive OpenAI-style SSE chunks:

```powershell
curl.exe -N http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer replace-with-generated-key" `
  -d '{ "model": "gpt-5.5", "stream": true, "messages": [{ "role": "user", "content": "Stream this response" }] }'
```

## Endpoints

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /health` | Public | Health check |
| `GET /v1/models` | Protected | Lists the configured default model |
| `POST /v1/chat/completions` | Protected | OpenAI-compatible chat completions |

Configured access control applies to `/v1/*`. `/health` remains public so
supervisors and health checks can keep working.

## Configuration

CodexOAuthAPI reads a project-local `.env` file from the current working
directory. Values already set in the OS environment take precedence over `.env`.

| Key | Required | Description |
| --- | --- | --- |
| `CODEX_OAUTH_API_KEY` | Optional | Bearer token required for `/v1/*` when set |
| `CODEX_OAUTH_API_ALLOWED_IPS` | Optional | Comma-separated direct client IP allowlist for `/v1/*` |
| `CODEX_OAUTH_API_STATE_ROOT` | Optional | Isolated server state directory |
| `CODEX_OAUTH_API_DEFAULT_MODEL` | Optional | Model listed by `/v1/models` and used when omitted |
| `CODEX_OAUTH_API_AUTO_LOGIN` | Optional | Set to `true` to allow request-triggered device login |

Notes:

- `CODEX_OAUTH_API_ALLOWED_IPS` matches `request.client.host` exactly.
- `X-Forwarded-For` and other forwarded headers are not trusted.
- `CODEX_OAUTH_API_AUTO_LOGIN` defaults to `false` so API requests do not block
  on an interactive login flow.
- Real `.env` files are ignored by git.

## Access Control

Use any strong local secret as `CODEX_OAUTH_API_KEY`, or generate one:

```powershell
uv run codex-oauth-api generate-key
```

The command prints a ready-to-paste `.env` line:

```env
CODEX_OAUTH_API_KEY=...
```

Requests must send the exact bearer value:

```http
Authorization: Bearer <CODEX_OAUTH_API_KEY>
```

If both IP and bearer checks are enabled, IP filtering runs first. A disallowed
client IP receives `403`; a missing or wrong bearer token receives `401`.

## Isolation Contract

Each Codex SDK request uses project-local isolated settings:

- `CODEX_HOME` lives under the configured state root.
- `HOME` and `USERPROFILE` live under the configured state root.
- Codex `cwd` is fixed to the isolated state workspace.
- `OPENAI_API_KEY` is not passed into the Codex SDK environment.
- Codex threads start with `ephemeral=True`.
- Hidden developer instructions and skill instructions are disabled for the SDK call.

Default state root:

```powershell
.codex-oauth-api-state
```

## Debugging

Print request and response bodies while debugging:

```powershell
uv run codex-oauth-api serve --host 127.0.0.1 --port 8000 --debug
```

Debug request logs include `client_ip`, method, path, and body. They do not log
the `Authorization` header or bearer token.

## Tests

```powershell
uv run pytest
```

## Disclaimer

This project is intended for development and research testing of AI API calls.
It is not provided as production infrastructure, and the authors assume no
responsibility for issues, losses, outages, security incidents, billing
problems, data exposure, or service disruption caused by applying it to a real
operating service.

For production or service environments, use an official, supported API provider
service directly. This is strongly recommended for reliability, security,
compliance, support, and operational accountability.
