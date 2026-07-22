# Response Latency Improvement Plan

## Intent

Reduce fixed per-request startup latency and make streaming requests visibly start before the first model token, while adding enough timing data to identify future latency regressions.

## Scope

- Keep one initialized Codex runtime for the FastAPI application lifetime.
- Create an ephemeral Codex thread for each completion request on the shared runtime.
- Start an SSE response before waiting for the first model delta.
- Record request timestamps and phase durations for runtime startup, first token, model completion, and total request handling.
- Preserve the existing API key, IP filtering, OAuth retry, model selection, and OpenAI-compatible response shapes.
- Upgrade the Codex Python SDK and bundled CLI runtime from the beta dependency to `0.144.4`.
- Accept an opt-in `service_tier: "fast"` request field and pass it to the Codex SDK.
- Pre-create four standard-tier threads for the configured default model and replenish each consumed thread in the background.

## Exclusions

- Do not add request or model-call timeouts.

## Contracts

- Application startup initializes one shared Codex runtime and application shutdown closes it.
- Concurrent request turns remain independently routed by the SDK.
- Pooled threads are single-use, standard-tier threads; Fast mode and non-default models bypass the pool.
- The default pool size is four and can be changed with `CODEX_OAUTH_API_THREAD_POOL_SIZE`.
- Injected test client factories continue to work without starting a real Codex runtime.
- Streaming returns the role chunk immediately, then model content chunks, the stop chunk, and `[DONE]`.
- Authentication failures discovered after an SSE response starts are represented as an SSE error payload.
- Timing fields use monotonic elapsed milliseconds; timestamps use UTC ISO 8601 text.
- Secrets and authorization headers remain absent from logs.

## Sequence

1. Add regression tests for application runtime reuse and shutdown.
2. Add a streaming test proving response iteration starts before the model yields its first delta.
3. Add logging tests for timestamp and latency phase fields.
4. Implement application lifespan ownership of the shared Codex runtime.
5. Move the first model-delta wait inside the streaming generator.
6. Add phase timing records and update operator documentation.
7. Add request-level Fast mode propagation without changing the default tier.
8. Add a standard-tier, default-model thread pool with asynchronous replenishment.

## Acceptance Criteria

- Repeated requests use the same Codex process and create separate ephemeral threads.
- The shared process is closed exactly once during application shutdown.
- Streaming response headers and the role chunk are available before the first model delta.
- Debug logs identify request time, runtime startup, first-token latency, model duration, and total duration where applicable.
- All existing and new tests pass.
- The installed `openai-codex` and `openai-codex-cli-bin` versions are both `0.144.4`.
- `service_tier: "fast"` reaches `Codex.thread_start()` and never consumes a pooled thread.
- Four standard-tier threads for the default model are available when application startup completes.
- A consumed pooled thread is replaced without delaying its request's model turn.
- A local smoke test shows lower warm-request fixed overhead than the prior per-request process lifecycle.

## Verification

- Run focused tests for `tests/test_isolated_codex.py` and `tests/test_openai_compatible_api.py`.
- Run the complete pytest suite.
- Send two local completion requests and compare first-transfer and total durations.
