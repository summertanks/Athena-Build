# API-01 — HTTP API for the build platform

*Filed 2026-06-07.  Decisions taken with the operator during the thor1
full rebuild; implementation chunked alongside it.*

## Goal

A documented, key-protected HTTP API **in this repo** exposing everything
the platform knows — pipeline state, build records and their sidecars
(`.build.json`, `.buildlog`, `.vbuildlog`, container logs), configuration,
repo/mirror state — plus a **thin command dispatcher** that drives the
existing noun-verb command surface.  A **separate git project** consumes
this API to build the web UI; the two evolve independently against the
OpenAPI contract.

## Decisions (operator-confirmed)

| Decision | Choice | Rationale |
|---|---|---|
| Stack | **FastAPI + uvicorn** (build-host apt deps: `python3-fastapi python3-uvicorn python3-httpx`) | auto-generated OpenAPI + `/docs` Swagger UI satisfies the "well documented" requirement for free; typed models; SSE.  Build-host-only — nothing ships in the distro. |
| Exposure | **localhost-only default** (`127.0.0.1`); `0.0.0.0` opt-in via `[Api]` config; TLS via reverse proxy, never in-app | root-adjacent controller; remote access = SSH tunnel or caddy/nginx |
| Auth | `X-Api-Key` header; key auto-generated `0600` at `config/api.key` on first start; constant-time compare | same pattern as the OBS-01 HMAC key |
| Command model | **string-in dispatcher**: `POST /api/v1/command {"cmd": "source build attr"}` feeds the same dispatcher the REPL uses | zero per-command API code; surface can never drift from the CLI |
| Concurrency | the API server **is** the session (`build-system.sh --api`), commands run on a single job queue | single-writer invariant preserved by construction |
| Web UI | separate repository, consumes `/openapi.json` (TS client codegen) | clean separation, parallel development |

## Architecture

Third console-facade backend, sibling to `Tui` and `Cli` (UX-05 proved
the seam: 291 `console.print` callsites, 10 `Prompt`s, widget API all go
through `tui.console`).  `build-system.sh --api` starts `BuildSession`
with the Api backend; read endpoints are pure disk reads and need no
session.

```
scripts/webapi/
  __init__.py      lazy fastapi import + create_app(workdir) factory
  auth.py          key generation / verification (no fastapi needed)
  readers.py       disk-read helpers (records, sidecars, flags, progress)
  routes_info.py   GET state / builds / artifacts / progress / config / repo / mirror
  routes_cmd.py    POST command, GET jobs/{id}, SSE stream
  backend.py       Api console backend (facade) + job queue
```

## Endpoint surface (v1)

Read (disk):
- `GET /api/v1/state` — BuildFlags, mode, snapshot pin, counts
- `GET /api/v1/progress` — phases, rate, ETA, in-flight + log-growth
  liveness, classified deltas (the monitoring-worker aggregate)
- `GET /api/v1/builds?phase=&limit=&offset=` — record index
- `GET /api/v1/builds/{pkg}` — full record + HMAC verify status
- `GET /api/v1/builds/{pkg}/buildlog` · `/vbuildlog` · `/log?tail=N`
- `GET /api/v1/config` — build.conf **redacted** + pkg lists
- `GET /api/v1/repo` — dists tree summary
- `GET /api/v1/mirror` — mirror states, manifest summary, coord head

Command:
- `POST /api/v1/command` `{"cmd": "..."}` → `{job_id}` (409 when busy is
  wrong — queue instead; 400 on unknown verb)
- `GET /api/v1/jobs/{id}` → `{state, output[], started, elapsed}`
- `GET /api/v1/jobs/{id}/stream` — SSE live output

Docs: `/docs` (Swagger UI), `/openapi.json` — generated; `docs/api.md`
operator guide (auth, launch, exposure, sudo note).

## Security invariants

1. Never serve `gnupg/`, `config/*.key`, `coord/identity` — hard
   denylist checked before any file response; config endpoint redacts
   secret-bearing keys rather than trusting field names ad hoc.
2. Path traversal: package-name path params validated against the
   build-record index, not used to join paths directly.
3. Prompts never transit HTTP: commands that would `Prompt()` fail fast
   with `{error: "prompt_required", prompt: …}`; `--yes`-eligible
   informational prompts honour the existing `--yes` semantics; sudo
   flows use the already-shipped `ATHENA_SUDO_PASSWORD` env (UX-05b) set
   on the server process, never a request field.
4. Constant-time key compare; 401 without timing oracle; no key → 401
   for every route except `/docs` + `/openapi.json` (config-gated).

## Chunks

1. Skeleton: package layout, auth, `state` + `builds` endpoints, tests
   (skip cleanly when fastapi absent so the suite stays green pre-install).
2. Artifact endpoints with tail-windowing (linux's container log is
   215 MB — no naive reads) + signature status.
3. `progress` aggregate (port of the rebuild monitoring logic).
4. Command dispatcher: Api backend, job queue, SSE, `--api` launch,
   `[Api]` config block, `docs/api.md`, README link.

Per-chunk: ruff + mypy + full test suite.  Nothing imports webapi from
the existing pipeline modules — additive only; a missing fastapi must
never break the TUI/CLI paths.
