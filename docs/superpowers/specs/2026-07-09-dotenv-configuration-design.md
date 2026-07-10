# Dotenv Configuration Design

## Intent

Allow CodexOAuthAPI operators to configure access-control settings from a project-local `.env` file instead of requiring shell-level environment variables.

## Scope

- Load `.env` from the current working directory when server settings are built.
- Preserve existing OS environment variable precedence.
- Keep exact key names:
  - `CODEX_OAUTH_API_KEY`
  - `CODEX_OAUTH_API_ALLOWED_IPS`
  - `CODEX_OAUTH_API_STATE_ROOT`
  - `CODEX_OAUTH_API_DEFAULT_MODEL`
  - `CODEX_OAUTH_API_AUTO_LOGIN`
- Add a committed `.env.example` with placeholder values only.
- Keep real `.env` files untracked.

## Configuration Contract

- `.env` is optional.
- Values already present in `os.environ` take precedence over `.env`.
- `.env` must not introduce alternate or legacy key names.
- `.env` parsing is delegated to `python-dotenv`.

## Documentation Contract

- README documents `.env` usage.
- README shows placeholder token and IP examples only.
- `.env.example` contains safe placeholders and no real credentials.

## Acceptance Criteria

- Settings load from `.env` when OS environment variables are absent.
- OS environment variables override `.env` values.
- Existing environment-only behavior continues to work.
- `.env.example` is committed.
- `.env` is ignored by git.

## Verification

- Run `uv run pytest -q`.
- Run `git diff --check`.
