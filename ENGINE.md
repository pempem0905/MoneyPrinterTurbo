# Our Video Engine

This branch keeps the upstream MoneyPrinterTurbo generation pipeline intact and adds a thin API-only production wrapper for use by our own apps.

## Safety model

- `main` stays aligned with upstream.
- `snapshot-2026-09-11` is the immutable recovery checkpoint.
- `our-video-engine` contains our integration and hardening work.
- Real secrets stay in `config.toml` and `.env.engine`; both are ignored by Git.
- Engine mode requires `MPT_API_KEY` unless explicitly started with `MPT_ALLOW_INSECURE=1` for trusted local development.
- The Docker port is bound to `127.0.0.1` by default. Put a reverse proxy/VPN in front if remote access is needed.

## First local start

```bash
cp config.example.toml config.toml
cp .env.engine.example .env.engine
```

Set a long random `MPT_API_KEY` in `.env.engine`, then configure only the providers you actually use in `config.toml`.

Start the API-only engine:

```bash
docker compose -f docker-compose.engine.yml up -d --build
```

Health check:

```bash
curl http://127.0.0.1:8080/ping
```

Expected response:

```text
"pong"
```

## App integration

Create a video:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/videos \
  -H "Content-Type: application/json" \
  -H "x-api-key: $MPT_API_KEY" \
  -d '{
    "video_subject": "5 mistakes when buying a used iPhone",
    "video_language": "vi-VN",
    "video_aspect": "9:16"
  }'
```

The request returns a `task_id`. Poll task state with:

```bash
curl http://127.0.0.1:8080/api/v1/tasks/TASK_ID \
  -H "x-api-key: $MPT_API_KEY"
```

When the task reaches completion, the task response contains generated video paths/URLs. Downloads under `/tasks/...` require the same `x-api-key` when authentication is enabled.

## Recommended app topology

```text
Browser / PWA
     |
     v
Our App Backend
     |
     | server-to-server + x-api-key
     v
MoneyPrinterTurbo Video Engine
     |
     +--> LLM / TTS / stock or AI media providers
     |
     +--> FFmpeg
     v
Generated MP4
```

Do not expose `MPT_API_KEY` to browser code. The app backend should own all calls to the engine.

## Environment variables added by our wrapper

| Variable | Default | Purpose |
| --- | --- | --- |
| `MPT_API_KEY` | none | Engine API authentication secret |
| `MPT_ALLOW_INSECURE` | `0` | Permit no-auth engine startup only for trusted local development |
| `MPT_LISTEN_HOST` | `0.0.0.0` | Internal engine listener |
| `MPT_LISTEN_PORT` | `8080` | Internal engine port |
| `CORS_ALLOWED_ORIGINS` | empty | Existing upstream browser-origin allowlist |
| `MPT_APP_REDIS_HOST` | `redis` in example env | Existing upstream Redis host override |

## What this batch intentionally does not change

- script generation
- TTS
- subtitle generation
- stock/AI media generation
- FFmpeg editing
- task queue behavior
- publishing providers

Those stay upstream-compatible until separately audited and tested.

## Capability report (Common OS integration)

`GET /api/v1/capabilities` (same `x-api-key` protection as every `/api/v1` route) is a
read-only report for server-to-server integrations. It returns:

- `sources[]`: every material source id (`local`, `pexels`, `pixabay`, `coverr`, `wavespeed`,
  `volcengine_seedance`, `ofox`, `metaso_minimax`, `loomloom`, `openai_image`) with `type`
  (`local` | `stock` | `ai_video` | `ai_image`), `paid`, `configured`, `api_renderable`,
  aspect support, `supports_scene_prompt`, `supports_direct_material` and `cost_warning`.
- `music_providers[]`: paid music generators and whether they are configured.
- `render_options`: the enum values / numeric bounds `POST /api/v1/videos` accepts.
- `script_contract`: a non-empty `video_script` and non-empty `video_terms` are used verbatim
  (no LLM call). `terms_may_be_reordered` is `true` only when TwelveLabs is configured and the
  request leaves `match_materials_to_script=false`.
- `direct_materials`: how explicit materials are honored (see below).

`configured` values are booleans only. The endpoint never returns keys, tokens, provider base
URLs or raw `config.toml` values. Secrets are still managed only through `config.toml`,
environment variables, or the allowlisted `/api/v1/provider-config` route.

### Explicit per-scene materials — exact limitation

`video_materials` is honored **only** with `video_source="local"`. Each entry's `url` must be a
file name inside the engine's `storage/local_videos` directory (upload with
`POST /api/v1/video_materials`; path traversal is rejected). For every other source the engine
searches/generates materials itself from `video_terms` and ignores `video_materials`.

To mix providers per scene (shop media + Pexels + an AI clip), the caller resolves/downloads each
scene's material, uploads it, then renders with `video_source="local"`,
`video_concat_mode="sequential"` and the materials in scene order. In sequential mode each material
contributes at most one `video_clip_duration`-long cut (scaled by `video_clip_speed`); the engine
does not align cuts to per-scene narration timing. `source_info` on each material is persisted in
the task's `script.json` for attribution.

`loomloom` requires an interactive confirmed quote and cannot be started from
`POST /api/v1/videos` (`api_renderable=false`).
