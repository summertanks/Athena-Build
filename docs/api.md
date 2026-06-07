# HTTP API (API-01) — operator guide

A key-protected FastAPI surface over the build platform: pipeline state,
signed build records and every sidecar, redacted configuration,
repo/mirror state, and a thin command dispatcher driving the same
noun-verb commands as the TUI/CLI.  Design + decisions:
[`docs/plans/api-01-web-api.md`](plans/api-01-web-api.md).

## Prerequisites (build host only)

```bash
sudo apt install python3-fastapi python3-uvicorn python3-httpx
```

Nothing here ships in the distro; these are toolchain-host packages like
docker or ruff.

## Launching

```bash
./build-system.sh --api               # default port 8765
./build-system.sh --api --api-port 9000
```

This starts the one-and-only `BuildSession` with the API as its
frontend (the third backend besides the TUI and `--headless` CLI).
Commands POSTed to the API run serially on the session's main thread —
the single-writer model is preserved by construction.  **Do not** run
`--api` at the same time as another TUI/CLI session in the same workdir.

On first start the server generates `config/api.key` (mode 0600) and
prints the listen URL.  It binds **127.0.0.1 only**; for remote access
use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 host`) or a TLS reverse
proxy.  Plain-HTTP LAN exposure is deliberately not a supported mode.

## Authentication

Every `/api/v1/*` route requires the header `X-Api-Key` matching
`config/api.key`:

```bash
KEY=$(cat config/api.key)
curl -s -H "X-Api-Key: $KEY" http://127.0.0.1:8765/api/v1/state | jq .
```

Interactive docs: `http://127.0.0.1:8765/docs` (Swagger UI), machine
spec at `/openapi.json` — the contract the separate web-UI repository
consumes (TypeScript client codegen recommended).

## Read endpoints

| Route | Returns |
|---|---|
| `GET /api/v1/state` | stage gates (`buildflags`) + record phase counts |
| `GET /api/v1/progress?total=985&window=1800` | phases, windowed rate + ETA, in-flight sources with container-log liveness, failed list, unexplained `.buildlog`-vs-`.vbuildlog` delta residue |
| `GET /api/v1/builds?phase=&limit=&offset=` | paginated build-record index |
| `GET /api/v1/builds/{pkg}` | full signed record; `found` + `verified` (a tampered record reports `verified:false`, it is never hidden) |
| `GET /api/v1/builds/{pkg}/buildlog` | OBS-04 narrative (text) |
| `GET /api/v1/builds/{pkg}/vbuildlog` | virtual prediction (text) |
| `GET /api/v1/builds/{pkg}/log?tail=500` | container stdout tail — windowed reads only; full reads are capped (kernel logs exceed 200 MB) |
| `GET /api/v1/config` | `build.conf` with secret-bearing values `[REDACTED]` |
| `GET /api/v1/repo` | per-dir artifact counts + bytes under `repo/dists` |
| `GET /api/v1/mirror` | mirror `.state` files + local coord head (public federation metadata only) |

## Command dispatch

```bash
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"cmd": "source build attr"}' \
     http://127.0.0.1:8765/api/v1/command
# → {"job_id": "3f2a…", "state": "queued"}

curl -s -H "X-Api-Key: $KEY" http://127.0.0.1:8765/api/v1/jobs/3f2a… | jq .
curl -N  -H "X-Api-Key: $KEY" http://127.0.0.1:8765/api/v1/jobs/3f2a…/stream
```

- The command string is fed to the **same dispatcher as the REPL** —
  anything you can type at the prompt works; nothing else does.
  Unknown verbs are rejected at submit (400).
- Jobs run **one at a time** in submission order.
- **Prompts never transit HTTP.**  A command that needs interactive
  input fails with a `PromptRequired` error in the job output.  For
  sudo-needing commands set `ATHENA_SUDO_PASSWORD` in the server's
  environment (UX-05b — it is popped from the environment on read).
  Start the server with `--yes` to auto-accept informational prompts.
- `quit` / `exit` / `help` are REPL control tokens, not API commands.

## Security model (summary)

- Fail-closed key check (missing key file ⇒ nothing authenticates).
- Package-name path params validated against Debian name grammar before
  any filesystem access — traversal is structurally impossible.
- `gnupg/`, `config/*.key`, `coord/identity` are never served; the
  config endpoint redacts by option name, deliberately over-broad.
- Localhost bind; TLS and remote exposure are the proxy's job.
