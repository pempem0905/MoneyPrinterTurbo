# Our Video Engine — Batch 1 Checkpoint

Date: 2026-09-11

Base upstream/fork commit:

`436b0e9cc830ef7639e33388917e389cf30d3307`

Branch:

`our-video-engine`

## Batch 1 scope

- Preserve the upstream generation pipeline unchanged.
- Add an API-only runtime entrypoint (`engine.py`).
- Require an environment-provided engine API key by default.
- Keep browser CORS closed unless explicitly configured.
- Add an API-only Docker Compose profile bound to host loopback by default.
- Add a healthcheck using the existing `/ping` endpoint.
- Keep provider configuration in the existing upstream `config.toml` model for now.
- Ignore local `.env.engine` secrets.
- Add CI compilation/lint coverage for `engine.py` and unit coverage for runtime overrides.

## Explicit non-goals

This checkpoint does not change script generation, TTS, subtitles, material selection/generation, FFmpeg rendering, publishing providers, or the task queue implementation.

## CI retrigger note

A documentation-only commit was pushed on 2026-09-11 to retrigger the pull-request workflow after the fork initially reported no workflow runs. No runtime or generation logic was changed by this retrigger.

## Next batch after CI passes

1. Add a stable app-facing adapter contract so our app does not depend directly on every upstream request field.
2. Add a minimal `/engine/*` facade for create/status/result if needed.
3. Add durable job/storage design before scaling beyond a single engine instance.
4. Add deployment secrets and reverse-proxy/VPN configuration only when choosing the target host.
