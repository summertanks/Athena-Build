"""API-01 — HTTP API for the build platform (FastAPI).

``create_app()`` is the single entry point; everything fastapi-flavoured
is imported lazily inside it so the rest of the build system (TUI, CLI,
tests on hosts without python3-fastapi) never pays the dependency.  The
sibling modules ``auth`` and ``readers`` are deliberately stdlib-only.

Design + endpoint contract: docs/plans/api-01-web-api.md.  Launch mode
(`build-system.sh --api`) and the command dispatcher land in later
chunks; this module serves the read-only surface.
"""

from typing import Any

APT_HINT = (
    "python3-fastapi is not installed — the API server needs it.  "
    "Install on the build host with:\n"
    "  sudo apt install python3-fastapi python3-uvicorn python3-httpx"
)


def create_app(*, buildlog_dir: str, flags_path: str,
               api_key_path: str, conf_path: str = '',
               repo_dir: str = '', config_dir: str = '',
               coord_dir: str = '', backend: 'Any' = None) -> 'Any':
    """Build the FastAPI application.

    Read-only surface always; the command-dispatch routes are
    registered ONLY when a live ``ApiBackend`` (the session's console
    facade in ``--api`` mode) is passed — a read-only sidecar server
    physically has no command endpoints.

    Raises RuntimeError with an apt hint when fastapi is unavailable —
    callers surface that string verbatim to the operator.
    """
    try:
        from fastapi import (  # type: ignore[import-not-found]
            Depends, FastAPI, Header, HTTPException, Query,
        )
        from fastapi.responses import (  # type: ignore[import-not-found]
            PlainTextResponse, StreamingResponse,
        )
        from pydantic import BaseModel  # type: ignore[import-not-found]
    except ImportError as _e:  # pragma: no cover — exercised on bare hosts
        raise RuntimeError(APT_HINT) from _e

    import time as _time

    from webapi import auth, readers

    _key = auth.ensure_api_key(api_key_path)

    app = FastAPI(
        title='Athena-Build API',
        version='1.0',
        description=(
            'Key-protected HTTP surface over the Athena build platform: '
            'pipeline state, signed build records and their sidecars, '
            'and (in later revisions) a command dispatcher.  All routes '
            'require the X-Api-Key header; the key lives at '
            'config/api.key on the build host.'
        ),
    )

    def _require_key(x_api_key: str = Header(default='')) -> None:
        if not auth.verify_key(x_api_key, _key):
            raise HTTPException(
                status_code=401,
                detail='invalid or missing X-Api-Key header')

    _authed = [Depends(_require_key)]

    @app.get('/api/v1/state', dependencies=_authed, tags=['state'],
             summary='Pipeline stage gates + record phase counts')
    def state() -> 'dict':
        _doc = readers.read_flags(flags_path)
        _doc['record_phases'] = readers.phase_counts(buildlog_dir)
        return _doc

    @app.get('/api/v1/builds', dependencies=_authed, tags=['builds'],
             summary='Paginated index of per-source build records')
    def builds(phase: str = Query(default=''),
               limit: int = Query(default=100, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)) -> 'dict':
        return readers.list_builds(
            buildlog_dir, phase=phase or None, limit=limit, offset=offset)

    @app.get('/api/v1/builds/{package}', dependencies=_authed,
             tags=['builds'],
             summary='One signed build record + integrity status')
    def build(package: str) -> 'dict':
        if not readers.valid_package_name(package):
            raise HTTPException(status_code=400,
                                detail='invalid package name')
        _doc = readers.get_build(buildlog_dir, package)
        if not _doc['found']:
            raise HTTPException(status_code=404,
                                detail=f'no build record for {package!r}')
        return _doc

    def _artifact(package: str, kind: str, tail: int) -> 'dict':
        if not readers.valid_package_name(package):
            raise HTTPException(status_code=400,
                                detail='invalid package name')
        _doc = readers.read_artifact(buildlog_dir, package, kind, tail=tail)
        if not _doc.get('found'):
            raise HTTPException(
                status_code=404,
                detail=f'no {kind} for {package!r}')
        # a full read past the 4 MB cap returns {'error': ...}
        # with no 'text'; surface it as 413 instead of an empty 200.
        if _doc.get('error'):
            raise HTTPException(status_code=413, detail=_doc['error'])
        return _doc

    @app.get('/api/v1/builds/{package}/buildlog', dependencies=_authed,
             tags=['artifacts'],
             summary='OBS-04 build narrative (actual emissions)')
    def buildlog(package: str) -> 'Any':
        _doc = _artifact(package, 'buildlog', tail=0)
        return PlainTextResponse(_doc.get('text', ''))

    @app.get('/api/v1/builds/{package}/vbuildlog', dependencies=_authed,
             tags=['artifacts'],
             summary='Virtual-build prediction (expected emissions)')
    def vbuildlog(package: str) -> 'Any':
        _doc = _artifact(package, 'vbuildlog', tail=0)
        return PlainTextResponse(_doc.get('text', ''))

    @app.get('/api/v1/builds/{package}/log', dependencies=_authed,
             tags=['artifacts'],
             summary='Container stdout tail (windowed — logs reach 200+ MB)')
    def container_log(package: str,
                      tail: int = Query(default=500, ge=1, le=100000),
                      ) -> 'dict':
        return _artifact(package, 'log', tail=tail)

    @app.get('/api/v1/progress', dependencies=_authed, tags=['state'],
             summary='Live pipeline aggregate: phases, rate/ETA, '
                     'in-flight liveness, unexplained deltas')
    def progress(total: int = Query(default=0, ge=0),
                 window: int = Query(default=1800, ge=60, le=86400),
                 ) -> 'dict':
        return readers.progress(buildlog_dir, total=total, window_s=window)

    if conf_path:
        @app.get('/api/v1/config', dependencies=_authed, tags=['config'],
                 summary='build.conf with secret-bearing values redacted')
        def config() -> 'Any':
            _doc = readers.read_config_redacted(conf_path)
            if not _doc.get('found'):
                raise HTTPException(status_code=404,
                                    detail='config not found')
            return PlainTextResponse(_doc['text'])

    if repo_dir:
        @app.get('/api/v1/repo', dependencies=_authed, tags=['repo'],
                 summary='Pool shape: per-dir artifact counts + bytes')
        def repo() -> 'dict':
            return readers.repo_summary(repo_dir)

    if config_dir:
        @app.get('/api/v1/mirror', dependencies=_authed, tags=['repo'],
                 summary='Mirror states + local coord head (public '
                         'federation metadata only)')
        def mirror() -> 'dict':
            return readers.mirror_state(config_dir, coord_dir)

    # ── command dispatcher — only with a live session backend ──────────
    if backend is not None:
        class CommandIn(BaseModel):
            cmd: str

        @app.post('/api/v1/command', dependencies=_authed,
                  tags=['commands'], status_code=202,
                  summary='Enqueue a command (same dispatcher as the '
                          'REPL); returns a job id')
        def command(payload: CommandIn) -> 'dict':
            _cmd = (payload.cmd or '').strip()
            if not _cmd:
                raise HTTPException(status_code=400, detail='empty cmd')
            if _cmd.split()[0] in ('quit', 'exit', 'help'):
                raise HTTPException(status_code=400,
                                    detail='REPL control tokens are not '
                                           'dispatchable over the API')
            if not backend.known_command(_cmd):
                raise HTTPException(
                    status_code=400,
                    detail=f'unknown command {_cmd.split()[0]!r}')
            _job = backend.submit(_cmd)
            return {'job_id': _job.id, 'state': _job.state}

        @app.get('/api/v1/jobs', dependencies=_authed, tags=['commands'],
                 summary='All jobs this session (oldest first)')
        def jobs() -> 'dict':
            return {'jobs': backend.list_jobs()}

        @app.get('/api/v1/jobs/{job_id}', dependencies=_authed,
                 tags=['commands'], summary='One job: state + output')
        def job(job_id: str) -> 'dict':
            _job = backend.get_job(job_id)
            if _job is None:
                raise HTTPException(status_code=404, detail='no such job')
            return _job.as_dict()

        @app.get('/api/v1/jobs/{job_id}/stream', dependencies=_authed,
                 tags=['commands'],
                 summary='SSE live output until the job finishes')
        def stream(job_id: str) -> 'Any':
            _job = backend.get_job(job_id)
            if _job is None:
                raise HTTPException(status_code=404, detail='no such job')

            def _events() -> 'Any':
                _sent = 0
                while True:
                    _out = _job.output
                    while _sent < len(_out):
                        yield f"data: {_out[_sent]}\n\n"
                        _sent += 1
                    if _job.state in ('done', 'error'):
                        yield f"event: end\ndata: {_job.state}\n\n"
                        return
                    _time.sleep(0.3)

            return StreamingResponse(_events(),
                                     media_type='text/event-stream')

    return app
