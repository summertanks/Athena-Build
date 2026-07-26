
import logging
import os
import shlex
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Optional, Tuple
import base_rootfs
import local_mirror
import utils
from utils import BuildConfig, version_no_epoch
from buildlog import BuildLog, human_size, safe_size
from package import Source

import docker
import docker.errors  # noqa: F401 — explicit import so `docker.errors.X` resolves under mypy
import requests.exceptions as _req_exc
import urllib3.exceptions as _u3_exc
import tui

# Docker client read-timeout.  docker-py's default is 60s, which is the
# MAX silence on a single blocking read — and `container.wait()` returns
# nothing until the container exits while `container.logs(stream=True)`
# returns nothing during a quiet build phase.  On a multi-hour build
# (linux ~5h, libreoffice ~9h, webkit ~5h) that 60s is exceeded and the
# worker saw a spurious ReadTimeout 'failure' though the container was
# alive and ultimately succeeded (thor1 rebuild, 2026-06-08).  Raise it
# so normal quiet phases don't trip it AND live log-tailing survives;
# _wait_for_exit's keep-polling makes the exact value non-critical (it
# only sets poll granularity).  Override via [Build] DockerTimeout.
_DOCKER_TIMEOUT_DEFAULT = 1800   # 30 min

# docker-py raises these on a read/connection timeout; we treat them as
# 'container still running, keep polling', never as a build failure.  A
# genuinely-gone container raises docker.errors.NotFound (reap_all_live)
# which deliberately propagates.
#
# CRITICAL: include the RAW urllib3 variants, not just the requests-layer
# wrappers.  docker-py's low-level log-stream (`container.logs(stream=True)`)
# and `container.wait()` can surface `urllib3.exceptions.ReadTimeoutError`
# / `ConnectTimeoutError` (both subclass urllib3's `TimeoutError`) and
# `ProtocolError` (dropped keep-alive) WITHOUT wrapping them in
# requests.exceptions — and those are NOT subclasses of `requests.Timeout`.
# Without them, a quiet build phase longer than DockerTimeout escaped the
# keep-polling in _stream_and_wait/_wait_for_exit, propagated past build()'s
# `except docker.errors.APIError`, and the finally force-removed a HEALTHY
# multi-hour build (firefox-esr at -j1, ~3-5h, killed at 30 min — 2026-06-21).
_DOCKER_TRANSIENT = (
    _req_exc.Timeout, _req_exc.ConnectionError,
    _u3_exc.TimeoutError, _u3_exc.ProtocolError,
)

# docker-py's `DockerException` is the BASE class; `APIError` is a
# subclass, so `except docker.errors.APIError` MISSES a raw
# DockerException — which is exactly what `DockerClient()` / `from_env()`
# raise against an unreachable daemon (the server-version probe wraps the
# requests ConnectionError in a DockerException).  Connect paths must
# catch the base + the requests-layer transients.
_DOCKER_CONNECT_ERRORS = (docker.errors.DockerException,) + _DOCKER_TRANSIENT
# A best-effort `container.reload()` / `container.logs()` issued during the
# SAME daemon hiccup that produced a transient can itself raise a transient
# (it's another HTTP GET), NOT an APIError — tolerate both so the
# keep-polling loop / OOM probe / post-exit log read never escapes and
# records a still-running (or already-finished) build as failed.
_DOCKER_RELOAD_ERRORS = (docker.errors.APIError,) + _DOCKER_TRANSIENT


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` currently exists (signal-0 probe).
    PermissionError ⇒ the process exists but is owned by another user.
    Used by the startup orphan-reap so it never force-removes a container
    a DIFFERENT live Athena process (a concurrent session) owns."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # unknown — be conservative, don't reap
    return True

if TYPE_CHECKING:
    # Type-only import — verify_pkg_artifact takes an optional RepoState
    # parameter, resolved at call time via a lazy `import repo_audit`
    # inside the function body so the runtime dep stays optional.
    from repo_audit import RepoState

logger = logging.getLogger('athena.build')

# Environment for every container we run.  docker does NOT populate
# USER/LOGNAME from the image's `USER athena` (config/Dockerfile), and
# package test suites read them — curl's runtests.pl dies under
# fatal-warnings on undefined $USER (its sshd setup) instead of
# skipping; a real buildd always has both set.  Keep in step with the
# Dockerfile's useradd.
_CONTAINER_ENV = {'USER': 'athena', 'LOGNAME': 'athena'}

# Buildd parity: docker's default seccomp profile answers filtered
# syscalls with EPERM where the bare kernel would return EINVAL etc. —
# glibc's tst-personality/tst-clock2 and keyutils' keyctl tests assert
# KERNEL error semantics and fail under the filter (2026-07-06 run).
# AppArmor's docker-default profile is the second half of the same
# problem: with seccomp lifted, glibc's support/test-container tests get
# past unshare() for the first time (they used to report UNSUPPORTED,
# invisibly) and then die on the profile's mount() denial — 32 hard
# FAILs (2026-07-08 run).  Real Debian buildds run with no LSM
# confinement of the build; the container already grants the build
# passwordless sudo and is treated as a throwaway sandbox (MAT-04
# covers the host-facing mounts), so drop both filters.
_CONTAINER_SECURITY_OPT = ['seccomp=unconfined', 'apparmor=unconfined']

# serialises segregate's per-file moves from
# every worker's scratch dir into the shared repo subdirs.  Held
# only for the duration of one source's move loop (microseconds);
# isolates the os.makedirs / collision-check / os.rename triad so
# two workers can't race on the same dest filename or component dir.
_REPO_DEST_LOCK = threading.Lock()


def _container_cpu_pct(stats: dict) -> 'Optional[float]':
    """Container CPU% from a single `container.stats(stream=False)`
    reading — the cpu_stats-vs-precpu_stats delta docker provides per call.
    Returns None when the deltas aren't computable yet (first read, idle
    interval, or a malformed payload)."""
    try:
        _cpu = stats['cpu_stats']
        _pre = stats['precpu_stats']
        _cd = (_cpu['cpu_usage']['total_usage']
               - _pre['cpu_usage']['total_usage'])
        _sd = _cpu['system_cpu_usage'] - _pre['system_cpu_usage']
        if _sd <= 0 or _cd < 0:
            return None
        _ncpu = (_cpu.get('online_cpus')
                 or len(_cpu['cpu_usage'].get('percpu_usage') or [])
                 or 1)
        return (_cd / _sd) * _ncpu * 100.0
    except (KeyError, TypeError, ZeroDivisionError):
        return None


class BuildContainer:

    def __init__(self, config: BuildConfig, docker_server=None, cache=None,
                 connect: bool = True):

        # Cache (optional) — when wired, `build()` passes it to
        # `Source.build_depends(cache=…)` so that multi-provider virtual
        # build-deps (e.g. `libcurl4-dev`, `libsdl-dev`) get expanded to
        # alternative chains the container's apt-install loop can fall
        # back across.  Pre-fix, those failed non-interactively with
        # "Package X has no installation candidate".  None = legacy
        # behaviour (no expansion); the only production caller in
        # build.py wires it through, but tests that construct a
        # BuildContainer without a cache continue to work unchanged.
        self.cache = cache
        # keep the full config reference so segregate
        # can route .deb / .udeb destinations via config.deb_dest_for_
        # filename (which knows about the new unified apt-repo layout).
        # Originally the code only needed the path strings below.
        self.config = config

        self.src_path = config.dir_source
        self.log_path = config.dir_log
        self.repo_path = config.dir_repo
        self.arch = config.arch
        # Docker client read-timeout (see _DOCKER_TIMEOUT_DEFAULT) — a
        # generous floor so multi-hour builds' quiet phases don't trip
        # the wait/log-stream reads; keep-polling makes it non-critical.
        try:
            self._docker_timeout = int(getattr(
                config, 'docker_timeout', _DOCKER_TIMEOUT_DEFAULT))
        except (TypeError, ValueError):
            self._docker_timeout = _DOCKER_TIMEOUT_DEFAULT
        # Three-layer identity (see memory/project_three_layer_identity.md):
        #   build_distribution — display name ("Asgard"), substituted as
        #     @DISTRIBUTION@ in fork content before dpkg-buildpackage
        #   build_base_id      — lowercase ("asgard"), substituted as
        #     @BASE_ID@
        #   codename           — release codename ("thor"), substituted as
        #     @CODENAME@ and written into the changelog stanza
        #     distribution field by _changelog_bump.  Also exposed in
        #     the build container as ATHENA_CODENAME for any debian/rules
        #     that wants to read it directly (legacy path).
        self.build_distribution = config.build_distribution
        self.build_base_id      = config.build_base_id
        self.codename           = config.build_codename

        self.buildlog_path = os.path.join(config.dir_log, 'build')
        self.conf_path = config.dir_config

        self.patch_path = config.dir_patch_source
        self.patch_empty = config.dir_patch_empty
        # when true, build() runs a `apt-get install --simulate`
        # preview against build-deps in a transient container and prompts
        # the operator to proceed.  Off by default.
        self.audit_build_deps = config.audit_build_deps

        # Apply snapshot pinning to mirrors so the container's apt fetches
        # build-deps from the same archive snapshot the cache was built from.
        # resolve_snapshot_timestamp is memoised — if Cache already resolved,
        # this is a dict lookup, not a network call.
        from utils import resolve_snapshot_timestamp
        try:
            self.snapshot_ts = resolve_snapshot_timestamp(config)
        except (RuntimeError, ValueError) as e:
            logger.error(f"BuildContainer: snapshot resolution failed: {e}")
            raise
        self.mirrors = [
            m.with_snapshot(self.snapshot_ts, baseurl=config.snapshot_baseurl)
            for m in config.mirrors
        ]
        # Local build mirror: active only when enabled AND a valid mirror exists
        # for THIS snapshot.  When active, per-build containers add it as their
        # first apt source (bind-mounted at /localmirror) so build-deps come
        # from local disk instead of snapshot.debian.org.
        self._localmirror_active = bool(
            getattr(config, 'create_local_mirror', False)
            and local_mirror.is_valid_for(
                config.dir_localmirror, self.snapshot_ts))

        # image tag is config-derived — set unconditionally so a recipe-only
        # container (connect=False) can compose the recipe without a local
        # Docker connection or image build.
        # Arch-qualified (COMP-04 B1): BS1/BS3 share a host + snapshot
        # pin; without the arch in the tag the i386 checkout would
        # silently reuse the amd64 rootfs image (the reuse check hashes
        # only the arch-identical Dockerfile).
        self._image_tag = (
            f"athenalinux:build-{config.container_release}-{self.arch}-"
            f"{self.snapshot_ts}")

        self.client = None

        # recipe-only mode (used by `source remotebuild`): everything
        # compose_recipe() and the post-build pipeline (_segregate / _normalize
        # / _record_phase) need is already set above; skip the LOCAL Docker
        # connect + image build — the build runs on a REMOTE host.
        if not connect:
            return

        if docker_server is not None:
            # Refuse to silently talk to an unsafely-exposed Docker daemon.
            # The build container runs as a privileged sandbox (Dockerfile
            # grants its `athena` user passwordless sudo, see docs/security.md);
            # any code with reach to the daemon socket can mount the host fs
            # via `docker run -v /:/host` and become root on the host.
            #
            # Loopback (127.0.0.1, ::1, localhost) and unix:// sockets are
            # under operator control on the build machine.  Anything else —
            # tcp:// to a remote host without TLS — is the failure mode:
            # a network-reachable daemon is a privilege-escalation
            # primitive.  Require either loopback /
            # unix sockets, or an explicit tls=true marker in the URL
            # confirming the operator has set up cert auth.
            self._guard_docker_server(docker_server)
            try:
                _client = docker.DockerClient(
                    base_url=docker_server, timeout=self._docker_timeout)
                _client.ping()
                self.client = _client
            except _DOCKER_CONNECT_ERRORS:
                # catch the base DockerException + transients, not
                # just APIError, so an unreachable external daemon actually
                # falls back to local instead of escaping __init__.
                tui.console.print("Athena Build Docker: Couldn't connect to external server, reverting to local")

        if self.client is None:
            try:
                self.client = docker.from_env(timeout=self._docker_timeout)
                self.client.ping()
            except _DOCKER_CONNECT_ERRORS as e:
                # the most common failure (daemon not running) raises
                # a bare DockerException — wrap ALL connect failures in
                # RuntimeError so cmd_init_container's handler shows the
                # designed message instead of a raw traceback.
                logger.error(f"Athena Build Docker: Error {e}")
                tui.console.print(f"Athena Build Docker: Error {e}")
                raise RuntimeError(f"Cannot connect to local Docker daemon: {e}") from e

        # Report which Docker endpoint we connected to + the daemon's identity,
        # so on WSL / remote setups the operator can confirm at a glance whether
        # it's the host Docker Desktop daemon (shared into the distro via WSL
        # integration) or a local dockerd — and that we are NOT nesting daemons.
        # Best-effort: a version() hiccup must never block container bring-up.
        try:
            _ver = self.client.version()
            _endpoint = (docker_server
                         or getattr(self.client.api, 'base_url', '')
                         or 'default socket')
            tui.console.print(
                f"Docker: {_endpoint} — engine {_ver.get('Version', '?')} "
                f"({_ver.get('Os', '?')}/{_ver.get('Arch', '?')}, "
                f"API {_ver.get('ApiVersion', '?')})",
                tui.COLOR_INFO)
            logger.info(
                f"Docker endpoint={_endpoint} engine={_ver.get('Version')} "
                f"os={_ver.get('Os')} arch={_ver.get('Arch')} "
                f"api={_ver.get('ApiVersion')}")
        except Exception as _e:
            logger.debug(f"Docker version probe failed: {_e}")

        # bake the snapshot TS into the image tag so a snapshot
        # advance ([Snapshot] Timestamp change in build.conf) invalidates
        # the image cache automatically — `docker images` lookup misses
        # the old tag, we build a fresh image against the new snapshot.
        # Earlier the tag was `athenalinux:build-<release>`; an
        # operator's `[Snapshot] Timestamp` change wouldn't invalidate
        # the existing image even though its toolchain layer was now
        # against an older snapshot.
        _image_tag = self._image_tag
        dockerfile_hash = self._hash_dockerfile(config.dir_config)
        _needs_build = False

        try:
            image = self.client.images.get(_image_tag)
            stored_hash = image.labels.get('athena.dockerfile.sha256', '')
            stored_arch = image.labels.get('athena.arch', '')

            if stored_hash != dockerfile_hash:
                tui.console.print(f"Dockerfile changed — rebuilding {_image_tag}")
                _needs_build = True
            elif stored_arch != self.arch:
                # Belt-and-braces behind the arch-qualified tag: a
                # pre-COMP-04 image or a hand-tagged one must never be
                # reused for the wrong arch (B1).
                tui.console.print(
                    f"Image arch label {stored_arch!r} != target "
                    f"{self.arch!r} — rebuilding {_image_tag}")
                _needs_build = True
            else:
                tui.console.print(f"Using Athena Build Image - {image.tags}")

        except docker.errors.ImageNotFound:
            tui.console.print(f"Image not found — building {_image_tag}")
            _needs_build = True

        except docker.errors.APIError as e:
            logger.error(f"Athena Build Docker: Error {e}")
            tui.console.print(f"Athena Build Docker: Error {e}")
            tui.Exit(1)

        if _needs_build:
            _ctx = ''
            try:
                # The Dockerfile is FROM scratch + ADD base-rootfs.tar —
                # the base is OUR mmdebstrap buildd bootstrap from the
                # pinned snapshot (base_rootfs.py), not a Docker Hub pull.
                # Assemble a minimal build context (Dockerfile + tar) in a
                # staging dir: config/ itself must not carry the ~250MB
                # tar, and docker would otherwise ship the whole config
                # dir as context.  Hard-link the cached tar when possible.
                _rootfs = base_rootfs.ensure_base_rootfs(
                    config, self.snapshot_ts)
                # MAT-02 D4b: the transpose pipeline compensates for
                # building Debian-versioned inputs in a DEBIAN container;
                # a NATIVE base (our own distribution's rootfs — inputs
                # already +asg) must not re-enter it.  Refuse loudly on a
                # mode mismatch instead of silently double-processing;
                # native building arrives with the self-hosting milestone.
                self._assert_nonnative_base(_rootfs, config)
                _ctx = tempfile.mkdtemp(
                    prefix='imagectx-', dir=config.dir_temp)
                shutil.copy(os.path.join(config.dir_config, 'Dockerfile'),
                            os.path.join(_ctx, 'Dockerfile'))
                _ctx_tar = os.path.join(_ctx, base_rootfs.ROOTFS_BASENAME)
                try:
                    os.link(_rootfs, _ctx_tar)
                except OSError:
                    shutil.copy(_rootfs, _ctx_tar)
                # pass the snapshot triplet as build-args so the
                # Dockerfile's sources.list write pins the toolchain
                # layer to OUR snapshot (the bootstrapped base already
                # is; the explicit write keeps the image's apt view
                # independent of the bootstrap tool's leftovers).
                image, build_logs = self.client.images.build(
                    path=_ctx, tag=_image_tag,
                    buildargs={
                        'RELEASE':              config.container_release,
                        'SNAPSHOT_BASEURL':     config.snapshot_baseurl,
                        # The toolchain Dockerfile writes three sources
                        # (main + -updates + -security) so packages that
                        # have moved to a security pocket / been pulled
                        # from main resolve correctly — mirrors
                        # _write_snapshot_sources_cmd's per-build shape.
                        # Without -updates/-security, packages absent
                        # from the release suite at a given TS surface as
                        # `apt-get install` "Unable to locate package …"
                        # (e.g. debhelper, python3, autoconf at 20260529
                        # — Architecture: all packages in flux on the
                        # bookworm stable suite).
                        'ARCHIVE_NAME':          'debian',
                        'SECURITY_ARCHIVE_NAME': 'debian-security',
                        'SNAPSHOT_TS':          self.snapshot_ts,
                    },
                    labels={'athena.dockerfile.sha256': dockerfile_hash,
                            'athena.arch': self.arch},
                    nocache=False, rm=True, )

                tui.console.print(f"Athena Build Image Built - {image.tags}")
                try:
                    with open(os.path.join(self.log_path, 'docker_build.log'), 'w') as fh:
                        for chunk in build_logs:
                            # Docker SDK's images.build() yields a heterogeneous
                            # stream of dicts; only `stream` entries carry the
                            # human-readable lines we want in the log file.
                            if not isinstance(chunk, dict):
                                continue
                            _stream = chunk.get('stream')
                            if isinstance(_stream, str):
                                for line in _stream.splitlines():
                                    fh.write(line + '\n')

                except (FileNotFoundError, PermissionError) as e:
                    logger.error(f"Error writing docker build log: {e}")
                    tui.console.print(f"Error writing docker build log: {e}")
                    raise RuntimeError(f"Cannot write Docker build log: {e}") from e

            except docker.errors.BuildError as e:
                # BuildError carries the streamed build_log iterator —
                # drain it into log/docker_build.log so the operator can
                # see what apt actually said, and print the tail so the
                # cause surfaces inline (the SDK's default `e.msg` is
                # just "command returned non-zero code: N" and drops the
                # streamed stderr that actually explains why).
                _log_path = os.path.join(self.log_path, 'docker_build.log')
                _tail: 'list[str]' = []
                try:
                    with open(_log_path, 'w') as fh:
                        # docker-py types build_log as Iterator[JSON]; mypy's
                        # `JSON` recursive alias doesn't unify with a dict
                        # comprehension assignment.  isinstance guards the
                        # access at runtime, so the ignore is safe.
                        for chunk in e.build_log or iter(()):  # type: ignore[assignment]
                            if not isinstance(chunk, dict):
                                continue
                            for _key in ('stream', 'error'):
                                _val = chunk.get(_key)
                                if isinstance(_val, str):
                                    for line in _val.splitlines():
                                        fh.write(line + '\n')
                                        _tail.append(line)
                except OSError:
                    pass
                logger.error(
                    f"Docker image build FAILED: {e.msg}; tail={_tail[-25:]}")
                tui.console.print(f"Docker image build FAILED: {e.msg}")
                if _tail:
                    tui.console.print(
                        f"  last {min(len(_tail), 25)} line(s) of streamed "
                        f"build output (full log: {_log_path}):",
                        tui.COLOR_ERROR)
                    for _line in _tail[-25:]:
                        tui.console.print(f"    {_line}", tui.COLOR_ERROR)
                raise RuntimeError(f"Docker image build failed: {e.msg}") from e
            except docker.errors.APIError as e:
                logger.error(f"Athena Build Docker: Error {e}")
                tui.console.print(f"Athena Build Docker: Error {e}")
                raise RuntimeError(f"Docker image build failed: {e}") from e
            finally:
                # the staging context (Dockerfile + rootfs hard-link) is
                # consumed by the build — never leave the ~250MB copy
                # behind in tmp/ on either exit path.
                if _ctx:
                    shutil.rmtree(_ctx, ignore_errors=True)

        self.image = image

        # live-container registry.  Every container
        # spawned by this BuildContainer (build / preview
        # grub-mkrescue) registers here on `containers.run()` return and
        # deregisters in its finally block, so Phase 3's reap_all_live()
        # has an in-process list to force-remove on SIGINT.
        # Lock guards the dict against concurrent register/deregister
        # under the parallel ThreadPoolExecutor (Phase 4).
        self._live: 'dict[str, docker.models.containers.Container]' = {}
        self._live_lock = threading.Lock()
        # shutdown_event is set by request_shutdown()
        # (which also reaps all live containers).  Parallel workers
        # (Phase 4) consult it between scheduled jobs to bail out
        # without starting new builds; SIGINT-driven cleanup flips it
        # so reap_all_live() unblocks workers stuck in container.wait()
        # / container.logs(stream=True).
        self.shutdown_event = threading.Event()
        # The label every spawned container carries — used by the
        # startup orphan reap below + by the `docker ps` filter
        # operators can run from another terminal during a build.
        # com.athena.pid disambiguates which python process owns each.
        self._container_labels = {
            'com.athena.build': '1',
            'com.athena.pid': str(os.getpid()),
        }
        # sweep any per-worker scratch dirs left over
        # from a prior run (kill -9 / OOM / docker-daemon crash skipped
        # the finally-block rmtree).  Best-effort — a permission error
        # here is non-fatal: this run's per-worker dirs use fresh uuid4
        # names so they won't collide with survivors.
        _stage_root = config.dir_build_stage
        try:
            _survivors = [
                _d for _d in os.listdir(_stage_root)
                if os.path.isdir(os.path.join(_stage_root, _d))
            ]
            for _d in _survivors:
                shutil.rmtree(
                    os.path.join(_stage_root, _d), ignore_errors=True)
            if _survivors:
                tui.console.print(
                    f"BuildContainer: swept {len(_survivors)} leftover "
                    f"build-stage dir(s) from previous run")
        except OSError:
            pass

        # sweep leftover docker containers from a prior run
        # that didn't reach its build()/run_grub_mkrescue/capture
        # finally-block (kill -9 / SIGSEGV / daemon restart). reap
        # ONLY containers whose owner (com.athena.pid) is GONE — never one a
        # DIFFERENT live Athena process owns.  The old code force-removed
        # every com.athena.build=1 container, so a second session (or an
        # --api instance) killed the first session's in-flight multi-hour
        # builds, which then recorded as failed.  (Containers tagged with
        # OUR pid can only be leftovers — this process hasn't spawned any
        # yet — so they're reapable; same for missing/unparseable labels.)
        try:
            assert self.client is not None
            _my_pid = os.getpid()
            _all = self.client.containers.list(
                all=True, filters={'label': 'com.athena.build=1'})
            _reaped = 0
            _skipped = 0
            for _c in _all:
                _owner = (getattr(_c, 'labels', None) or {}).get(
                    'com.athena.pid', '')
                try:
                    _owner_pid: 'Optional[int]' = int(_owner)
                except (TypeError, ValueError):
                    _owner_pid = None
                if (_owner_pid is not None and _owner_pid != _my_pid
                        and _pid_alive(_owner_pid)):
                    _skipped += 1
                    continue  # owned by a live concurrent session — leave it
                try:
                    _c.remove(force=True)
                    _reaped += 1
                except docker.errors.APIError as e:
                    logger.warning(
                        f"orphan-reap: cannot remove {_c.short_id}: {e}")
            if _reaped:
                tui.console.print(
                    f"BuildContainer: reaped {_reaped} orphan "
                    f"container(s) from previous run")
            if _skipped:
                logger.info(
                    f"orphan-reap: left {_skipped} container(s) owned by a "
                    f"live concurrent Athena process untouched")
        except docker.errors.APIError as e:
            logger.warning(f"orphan-reap: docker error: {e}")


    @staticmethod
    def _guard_docker_server(docker_server: str) -> None:
        """Refuse to talk to a daemon that's reachable from the network
        without TLS.  See docs/security.md.

        Acceptable targets:
          - unix:///path/to/socket  — same host, filesystem-protected
          - tcp://127.0.0.1:PORT    — loopback, same host
          - tcp://[::1]:PORT        — loopback, same host
          - tcp://localhost:PORT    — loopback, same host
          - https://...             — TLS-protected (the docker SDK
                                      enforces server-cert validation)
          - any URL containing      — operator has explicitly set up TLS
            'tls=true' or '?tls=1'    + client-cert auth out of band

        Anything else (a bare tcp://192.168.x.y:2375) raises so the
        operator confronts the privilege-escalation primitive before
        the build container runs anything against it.
        """
        _scheme = urlparse(docker_server).scheme.lower()
        _host   = urlparse(docker_server).hostname or ''
        _safe_loopback = _host in ('127.0.0.1', '::1', 'localhost', '')

        if _scheme in ('unix', 'https'):
            return
        if _scheme in ('tcp', 'http') and _safe_loopback:
            return
        if 'tls=true' in docker_server.lower() or 'tls=1' in docker_server.lower():
            return

        raise RuntimeError(
            f"DOCKER_SERVER={docker_server!r} points at a network-reachable "
            f"daemon without TLS — see docs/security.md.  Build container has "
            f"passwordless sudo so a daemon-reachable attacker can mount the "
            f"host filesystem and become root.  Use unix:// or loopback "
            f"tcp://127.0.0.1, or set up TLS and add 'tls=true' to the URL."
        )

    def _container_command(self, cmd_str: str) -> 'list[str]':
        """The containers.run() command for a build shell line, wrapped
        in the target arch's linux personality when the profile demands
        one (COMP-04 B2): an i386 build on an amd64 kernel must run
        under `setarch linux32` so in-container `uname -m` reports i686
        — exactly what Debian's own i386 buildds do.  Without it,
        autotools/cmake configure against x86_64 and mis-target.
        setarch ships in util-linux (present in the buildd base)."""
        import arch_profile
        _personality = arch_profile.profile(self.arch).linux_personality
        _cmd = ["/bin/bash", "-c", cmd_str]
        if _personality:
            return ["setarch", _personality, "--"] + _cmd
        return _cmd

    def _resource_kwargs(self) -> dict:
        """Per-container CPU + RAM caps for
        containers.run().  Translates BuildConfig's BuildCpus /
        BuildMemory into docker-py's nano_cpus / mem_limit kwargs.
        Returns {} when both knobs are unset (current uncapped
        behaviour preserved); applied uniformly under serial and
        parallel paths (harmless under serial; protective under
        parallel where N concurrent dpkg-buildpackage runs would
        otherwise oversubscribe the host).

        nano_cpus is docker's billionth-of-a-CPU quota — passing
        nano_cpus=3_500_000_000 caps the container at 3.5 CPUs.
        mem_limit accepts docker's size strings ('8g', '512m'); a
        container exceeding it is OOM-killed (exit 137), detected
        post-wait() with an operator-friendly hint.
        """
        _kwargs: dict = {}
        if self.config.build_cpus > 0:
            _kwargs['nano_cpus'] = int(self.config.build_cpus * 1_000_000_000)
        if self.config.build_memory:
            _kwargs['mem_limit'] = self.config.build_memory
        return _kwargs

    def _register_live(self, container) -> None:
        """Track a freshly-started container in the
        in-process registry.  Phase 3's reap_all_live() iterates this
        registry under _live_lock to force-remove every owned
        container on SIGINT.  Idempotent (re-registering an existing
        short_id is a no-op).
        """
        with self._live_lock:
            self._live[container.short_id] = container

    def _deregister_live(self, container) -> None:
        """Drop a container from the registry after
        its build()/preview/grub-mkrescue finally-block reaches the
        normal cleanup path.  Idempotent (a missing key is fine —
        reap_all_live() may have removed it concurrently).
        """
        with self._live_lock:
            self._live.pop(container.short_id, None)

    def _sample_resources(self, container, acc: dict,
                          stop: 'threading.Event') -> None:
        """Poll thread: every ~2s sample the container's peak RSS and
        CPU% into `acc` until `stop` is set.  Strictly best-effort — a stats()
        hiccup (container gone, daemon blip) is swallowed; observability must
        never disturb the build.  `stop.wait()` is an interruptible sleep so
        the thread exits promptly when the build ends."""
        while not stop.is_set():
            try:
                _s = container.stats(stream=False)
                _mem = _s.get('memory_stats', {}) or {}
                _usage = _mem.get('usage')
                if isinstance(_usage, (int, float)):
                    acc['peak_rss_bytes'] = max(acc['peak_rss_bytes'], int(_usage))
                _lim = _mem.get('limit')
                if isinstance(_lim, (int, float)) and _lim:
                    acc['mem_limit_bytes'] = int(_lim)
                _cpu = _container_cpu_pct(_s)
                if _cpu is not None:
                    acc['peak_cpu_pct'] = max(acc['peak_cpu_pct'], _cpu)
                acc['samples'] += 1
            except Exception as _e:
                logger.debug(f"resource sample failed: {_e}")
            stop.wait(2.0)

    @staticmethod
    def _assert_nonnative_base(rootfs_tar: str, config) -> None:
        """MAT-02 D4b: read /etc/os-release from the base rootfs tar and
        refuse when its ID is the NATIVE distribution — this pipeline
        transposes (non-native mode), and running it against a native base
        would re-process already-+asg inputs.  Unreadable/absent os-release
        is tolerated (the mmdebstrap buildd base always carries one; don't
        brick exotic bases on the guard's account)."""
        import tarfile
        import source_emit as _se
        _text = ''
        try:
            with tarfile.open(rootfs_tar) as _t:
                for _name in ('./etc/os-release', 'etc/os-release',
                              './usr/lib/os-release', 'usr/lib/os-release'):
                    try:
                        _m = _t.extractfile(_name)
                    except KeyError:
                        continue
                    if _m is not None:
                        _text = _m.read().decode('utf-8', errors='replace')
                        break
        except (OSError, tarfile.TarError) as _e:
            logger.warning(f"D4b base check: unreadable rootfs tar: {_e}")
            return
        if not _text:
            logger.warning("D4b base check: no os-release in base rootfs")
            return
        _env = _se.environment_id(_text)
        _native = str(getattr(config, 'build_base_id', '') or '').lower()
        if _native and not _se.transpose_applies(_env, _native):
            raise RuntimeError(
                f"build container base is NATIVE ({_env}) but this pipeline "
                "runs in transpose (non-native) mode — building native "
                "sources here would double-process +asg inputs.  Native "
                "container builds arrive with the self-hosting milestone "
                "(IncludeBuildClosure); use a Debian-based container base.")
        logger.info(f"D4b base check: container base ID={_env or '?'} — "
                    "non-native, transpose mode confirmed")

    def _federation_claims_for(self, package: str) -> 'list[dict]':
        """Raw federation claim lines for *package* from every fetched
        coord tree (cache/mirror/*/fetched/claims/*.jsonl).  UNVERIFIED
        read — B7 uses only (intended_version, patch_set_hash) as a
        convergence cross-check, never as a trust root; the verified
        path stays on the publish/audit side.  Missing trees → []."""
        import glob as _glob
        import json as _json
        _out: 'list[dict]' = []
        _pat = os.path.join(
            self.config.dir_cache, 'mirror', '*', 'fetched',
            'claims', '*.jsonl')
        for _p in sorted(_glob.glob(_pat)):
            try:
                with open(_p, 'r', encoding='utf-8') as _fh:
                    for _line in _fh:
                        _line = _line.strip()
                        if not _line or f'"{package}"' not in _line:
                            continue
                        try:
                            _c = _json.loads(_line)
                        except ValueError:
                            continue
                        if isinstance(_c, dict) \
                                and _c.get('package') == package:
                            _out.append(_c)
            except OSError:
                continue
        return _out

    def _record_phase(self, package: str, *, initial: 'Optional[dict]' = None,
                      **fields: object) -> None:
        """Phase transition.  Best-effort: a record-write OSError
        must never mask a build result, so we swallow and log.

        Pass `initial=<dict>` to write the entry-phase record (creates
        the file).  Subsequent transitions pass only `fields` (read the
        existing record, merge, re-sign, atomic-replace).
        """
        try:
            if initial is not None:
                # the entry-phase write RECREATES the record;
                # carry the lifecycle layer (selection/history/...) the
                # parse stamped on the prior record through the rewrite.
                _prior = utils.read_build_record(self.buildlog_path, package)
                # archive the PRIOR completed run to the journal before
                # this build overwrites build.json — each run lives in exactly
                # one place (current in build.json, all prior in the journal),
                # so there is no duplication.  Best-effort inside utils.
                if _prior and _prior.get('phase') in ('done', 'failed'):
                    utils.archive_build_record(self.buildlog_path, _prior)
                initial = utils.preserve_lifecycle(_prior, initial)
                # TRANSPOSE scheme: decide P (our patch level on this source
                # version) against the prior record — reset on a version change,
                # ++ when our patch_set changed, reuse otherwise.  bn_bump_count
                # stays 0 on a normal build (the force-build path sets it).
                # COMP-04 B7: a NON-PRIMARY builder adopts the
                # federation's +pP for this base+patch-set instead of
                # minting from its own checkout history — independent
                # ledgers converge on identical patches with different
                # P and reference source versions the primary never
                # published.  Hash divergence raises (sync patch/
                # first); no comparable claim falls back to the local
                # ledger rule.
                _fed_p = None
                _cfg_b7 = getattr(self, 'config', None)
                if not getattr(_cfg_b7, 'arch_all_owner', True):
                    _fed_p = utils.federated_patch_level(
                        self._federation_claims_for(package),
                        str(initial.get('intended_version', '')),
                        str(initial.get('patch_set_hash', '')))
                initial['patch_bump_count'] = (
                    _fed_p if _fed_p is not None
                    else utils.decide_patch_bump_count(
                        _prior, str(initial.get('intended_version', '')),
                        str(initial.get('patch_set_hash', ''))))
                initial['bn_bump_count'] = 0
                utils.write_build_record(self.buildlog_path, initial)
            else:
                utils.update_build_record(self.buildlog_path, package, **fields)
        except (OSError, FileNotFoundError) as _e:
            logger.warning(
                f"build-record write failed for {package} "
                f"({fields.get('phase', 'initial')}): {_e}")

    def _write_buildlog(self, src_pkg, *, kind: str, status: str,
                        active_profiles, active_options,
                        plain_deps, or_groups,
                        container_name: str,
                        exit_code, oom_killed, elapsed,
                        emitted_scan: 'list[tuple[str, int]]',
                        seg_events: 'list[tuple]',
                        final_paths: 'list[str]',
                        output_hashes: 'dict[str, str]') -> None:
        """Compose + write the verbose per-package build narrative
        to ``log/build/<pkg>.buildlog``.

        Strictly observability.  The entire body is wrapped so a formatting
        or IO failure here can NEVER reach the build path — the worst case
        is a missing/partial .buildlog, never a failed or skipped build.
        """
        try:
            _blog = BuildLog(self.buildlog_path, src_pkg.package, kind=kind)
            _blog.header(
                status=status,
                intended_version=str(getattr(src_pkg, 'version', '')),
                arch=self.arch,
                profiles=' '.join(sorted(active_profiles)) or '(none)',
                options=' '.join(sorted(active_options)) or '(none)',
                container=container_name or '(unknown)',
            )

            _blog.section('BUILD-DEPENDS (resolved, post-profile filter)')
            if plain_deps or or_groups:
                for _d in sorted(plain_deps):
                    _blog.bullet(_d)
                for _g in or_groups:
                    _blog.bullet('(OR) ' + ' | '.join(_g))
            else:
                _blog.empty()

            _declared = sorted(getattr(src_pkg, 'binary', []) or [])
            _blog.section(
                f"EXPECTED (Package-List declared: {len(_declared)})")
            if _declared:
                for _b in _declared:
                    _blog.bullet(_b)
            else:
                _blog.empty('(no Binary: list on source)')

            _blog.section('CONTAINER RESULT')
            _blog.kv('exit_code', exit_code)
            _blog.kv('oom_killed', oom_killed)
            _blog.kv('elapsed', f"{elapsed}s" if elapsed is not None else '?')

            _blog.section(
                f"EMITTED (scratch scan, pre-segregate: {len(emitted_scan)})")
            if emitted_scan:
                for _name, _size in sorted(emitted_scan):
                    _blog.file(_name, size=_size)
            else:
                _blog.empty()

            _relocs = [e for e in seg_events if e and e[0] == 'relocate']
            _purges = [e for e in seg_events if e and e[0] == 'purge']
            _strips = [e for e in seg_events if e and e[0] == 'strip']
            _stamps = [e for e in seg_events if e and e[0] == 'stamp']

            _blog.section(f"RELOCATED (segregate: {len(_relocs)})")
            if _relocs:
                for _, _name, _dst in sorted(_relocs):
                    _blog.relocation(_name, _dst)
            else:
                _blog.empty()

            _blog.section(f"PURGED ({len(_purges)})")
            if _purges:
                for _, _name, _reason in sorted(_purges):
                    _blog.bullet(f"{_name}  ({_reason})")
            else:
                _blog.empty()

            _blog.section(f"NMU STRIP ({len(_strips)})")
            if _strips:
                for _, _old, _new in sorted(_strips):
                    _blog.bullet(f"{_old}  →  {_new}")
            else:
                _blog.empty()

            _blog.section(f"ASG STAMP ({len(_stamps)})")
            if _stamps:
                for _e in sorted(_stamps):
                    _blog.bullet(f"{_e[1]}  →  {_e[2]}  ({_e[3]})")
            else:
                _blog.empty()

            _blog.section(
                f"FINAL ARTIFACTS (post-normalize, on disk: "
                f"{len(final_paths)})")
            _total = 0
            if final_paths:
                for _p in sorted(final_paths):
                    _name = os.path.basename(_p)
                    _sz = safe_size(_p)
                    if _sz >= 0:
                        _total += _sz
                    _blog.file(_name, size=_sz,
                               sha256=output_hashes.get(_name, ''))
            else:
                _blog.empty()

            # Delta of declared (expected) vs scratch-emitted binary NAMES —
            # the at-a-glance "did the build produce what the metadata says".
            # Binary name = first '_'-delimited segment of the filename.
            _declared_set = set(_declared)
            _emitted_names = {_n.split('_', 1)[0] for _n, _ in emitted_scan}
            _extra = sorted(_emitted_names - _declared_set)
            _missing = sorted(_declared_set - _emitted_names)
            _blog.section('DELTA (declared Package-List vs scratch-emitted)')
            _blog.bullet(
                'emitted-not-declared: '
                + (', '.join(_extra) if _extra else '(none)'))
            _blog.bullet(
                'declared-not-emitted: '
                + (', '.join(_missing) if _missing else '(none)'))

            _blog.footer(
                status=status,
                files=len(final_paths),
                size=human_size(_total),
                elapsed=f"{elapsed}s" if elapsed is not None else '?')
            _blog.write()
        except Exception as _e:
            logger.warning(
                f"buildlog compose for "
                f"{getattr(src_pkg, 'package', '?')}: {_e}")

    def reap_all_live(self) -> int:
        """Force-remove every container currently in
        the live registry.  Iterates a snapshot of the registry (taken
        under _live_lock) so workers can keep deregistering as they
        notice their containers vanish.  Returns the count of reaped
        containers — caller logs the number.

        Idempotent: calling twice with no new containers in between
        is a no-op (the second pass sees an empty snapshot).

        Force-removing a container unblocks worker threads stuck
        inside `container.wait()` (raises docker.errors.NotFound) and
        the `container.logs(stream=True)` generator (StopIteration as
        the daemon's chunked stream ends).  Both bubble up to the
        worker's finally block, which calls _deregister_live() again
        and then container.remove(force=True) — the latter then
        raises NotFound which the existing except APIError catches.
        """
        with self._live_lock:
            _snapshot = list(self._live.values())
        _reaped = 0
        for _c in _snapshot:
            try:
                _c.remove(force=True)
                _reaped += 1
            except docker.errors.APIError as e:
                # NotFound is fine — race with the worker's own
                # finally block that already removed the container.
                logger.warning(
                    f"reap_all_live: {_c.short_id}: {e}")
        return _reaped

    def request_shutdown(self) -> int:
        """Signal every parallel worker to stop and
        force-reap every container they have in flight.  Sets
        self.shutdown_event (workers check between jobs) and calls
        reap_all_live() (force-removes in-flight containers so workers
        unblock from container.wait/logs immediately).

        Returns the number of containers reaped, for the caller's log
        message.  Idempotent — calling twice just resets nothing
        and reap_all_live's second pass sees an empty registry.

        Wired into the SIGINT chain by cmd_source_build's scoped hook
        (Phase 4): on Ctrl+C during a parallel pool, the main thread
        calls request_shutdown() → reap_all_live unblocks the workers
        → the ThreadPoolExecutor drains within ~100ms instead of
        waiting for 90-minute builds to finish.
        """
        self.shutdown_event.set()
        return self.reap_all_live()

    @staticmethod
    def _hash_dockerfile(config_dir: str) -> str:
        # route through utils.get_sha256 (chunked file_digest, '' on
        # missing/unreadable).  use_cache=False so no `.verified` sidecar is
        # dropped next to the operator's Dockerfile in config/.
        return utils.get_sha256(
            os.path.join(config_dir, 'Dockerfile'), use_cache=False)

    def _write_snapshot_sources_cmd(
            self, localmirror: 'Optional[bool]' = None) -> str:
        """Shell snippet that REPLACES the container's apt sources with
        our snapshot-pinned mirror list AND writes an apt preferences
        pin forcing snapshot versions to win over anything pre-installed
        in the base image.

        Three steps:

          1. rm every apt source file the base image might have shipped:
             old-style `/etc/apt/sources.list` + new-style deb822
             `/etc/apt/sources.list.d/*.{sources,list}`.  Without this,
             `debian:bookworm-slim`'s default deb822 file at
             `/etc/apt/sources.list.d/debian.sources` continues to feed
             apt with live deb.debian.org URLs and our snapshot pin is
             a no-op.

          2. Write our snapshot-mirror list to `/etc/apt/sources.list`
             in the legacy one-line format.

          3. Write `/etc/apt/preferences.d/athena-snapshot` pinning
             every package from `snapshot.debian.org` at Pin-Priority
             1001.  Priority > 1000 is the apt-pinning threshold that
             FORCES downgrade — without it, apt's default policy keeps
             the base image's newer installed version even when our
             snapshot advertises a lower one.  Drove the 2026-05-23
             wpa→libcrypto3-udeb (>= 3.0.20) loop: bookworm-slim's
             libssl3 was at 3.0.20-1~deb12u1, our snapshot's max is
             3.0.19-1~deb12u2, and `apt-get upgrade --allow-downgrades`
             refused to act because Pin-Priority 500 (default for
             explicit sources) doesn't outrank "installed is newer."
             At 1001 apt picks snapshot as Candidate and dist-upgrade
             downgrades the installed set.
        """
        # Local build mirror FIRST (when active): identical snapshot versions
        # served from a bind-mounted file:// repo, pinned ABOVE the snapshot
        # origin (1002 > 1001) via its Release `Origin: AthenaLocalMirror` so
        # apt fetches build-deps from local disk; snapshot.debian.org stays the
        # fallback for anything the mirror missed.  [trusted=yes] skips the GPG
        # check (the repo is host-owned and bind-mounted read-only).
        # `localmirror` override (per-remote builds pass the target remote's
        # flag); None falls back to this container's own _localmirror_active.
        _lm_on = (getattr(self, '_localmirror_active', False)
                  if localmirror is None else bool(localmirror))
        _local_src = ''
        _local_pin = ''
        if _lm_on:
            _local_src = 'deb [trusted=yes] file:///localmirror ./\n'
            _local_pin = (
                "Package: *\n"
                f"Pin: release o={local_mirror.LOCAL_ORIGIN}\n"
                "Pin-Priority: 1002\n\n"
            )
        _apt_sources = _local_src + ''.join(
            f'deb [check-valid-until=no] {_m.url} {_m.suite} {_m.component}\n'
            for _m in self.mirrors
        )
        # The pin's `origin` is the HOST of the snapshot mirror URL — derive
        # it from [Snapshot] BaseUrl (the same host the sources above use, via
        # `_m.url = {baseurl}/{baseid}`) so a CUSTOM snapshot mirror still
        # pins.  Hardcoding snapshot.debian.org silently dropped the pin to
        # Priority 500 on any non-default mirror, re-opening the 2026-05-23
        # downgrade-refusal loop.  Distinct from the Origin: header
        # inside InRelease (`Origin: Debian` / `Origin: Debian-Security`).
        # This pin is broad — every pkg from that host wins.
        _snap_host = (
            urlparse(self.config.snapshot_baseurl).hostname
            or 'snapshot.debian.org'
        )
        _apt_pin = _local_pin + (
            "Package: *\n"
            f"Pin: origin {_snap_host}\n"
            "Pin-Priority: 1001\n"
        )
        return (
            "sudo rm -f /etc/apt/sources.list "
            "/etc/apt/sources.list.d/*.sources "
            "/etc/apt/sources.list.d/*.list; "
            f"sudo tee /etc/apt/sources.list >/dev/null <<'EOF'\n"
            f"{_apt_sources}EOF\n"
            "sudo tee /etc/apt/preferences.d/athena-snapshot "
            ">/dev/null <<'EOF'\n"
            f"{_apt_pin}EOF\n"
        )

    def _wait_for_exit(self, container) -> dict:
        """``container.wait()`` resilient to docker-py read timeouts.

        On a multi-hour build a quiet phase exceeds the client read
        timeout and ``wait()`` raises a requests-layer Timeout though the
        container is alive — treat that as 'still running, keep polling',
        NOT a failure: reload the container's state and re-wait until it
        actually exits.  A genuinely-gone container (``NotFound``, raised
        by reap_all_live's force-remove) propagates so the caller's
        existing teardown runs.
        """
        while True:
            try:
                return container.wait()
            except docker.errors.NotFound:
                raise
            except _DOCKER_TRANSIENT as _e:
                try:
                    container.reload()
                except _DOCKER_RELOAD_ERRORS:
                    # reload() is another HTTP GET — during the same
                    # hiccup it can raise a transient, NOT an APIError.
                    # Tolerate both so the keep-polling loop never escapes
                    # and records a still-running build as failed.
                    pass
                _state = container.attrs.get('State', {}) or {}
                if (_state.get('Status') in ('exited', 'dead')
                        or _state.get('Running') is False):
                    return {'StatusCode': _state.get('ExitCode', 1)}
                logger.debug(
                    f"container {getattr(container, 'short_id', '?')} still "
                    f"running after wait timeout ({type(_e).__name__}) — "
                    f"continuing to poll")

    def _stream_and_wait(self, container, log_path: str) -> dict:
        """Stream a container's stdout/stderr to ``log_path`` and return
        its exit info, resilient to docker-py read timeouts.

        Log streaming is best-effort operator visibility: a read timeout
        during a quiet build phase NEVER fails the build — we stop
        tailing, poll the container to exit via _wait_for_exit, then dump
        the COMPLETE logs (non-streaming) so nothing is lost.  Only a
        real non-zero container exit is a failure.
        """
        _streamed = False
        try:
            with open(log_path, 'w') as _fh:
                for _line in container.logs(stream=True):
                    _fh.write(_line.decode('utf-8', errors='replace'))
            _streamed = True   # stream closed naturally → container exited
        except _DOCKER_TRANSIENT as _e:
            logger.info(
                f"build log stream for "
                f"{getattr(container, 'short_id', '?')} interrupted "
                f"({type(_e).__name__}) during a quiet phase — not a "
                f"failure; logs re-dumped at exit")
        except docker.errors.NotFound:
            pass   # reaped externally; _wait_for_exit surfaces it
        _exit = self._wait_for_exit(container)
        if not _streamed:
            try:
                with open(log_path, 'w') as _fh:
                    _fh.write(container.logs(stream=False).decode(
                        'utf-8', errors='replace'))
            except Exception as _e:
                logger.warning(
                    f"final log dump for "
                    f"{getattr(container, 'short_id', '?')}: {_e}")
        return _exit

    def _image_build_args(self) -> dict:
        """Docker build-args for the toolchain image — the single source for
        both the local image build (__init__) and the remote recipe
        (compose_recipe).  The Dockerfile pins apt to the snapshot from these."""
        return {
            'RELEASE':               self.config.container_release,
            'SNAPSHOT_BASEURL':      self.config.snapshot_baseurl,
            'ARCHIVE_NAME':          'debian',
            'SECURITY_ARCHIVE_NAME': 'debian-security',
            'SNAPSHOT_TS':           self.snapshot_ts,
        }

    def compose_recipe(self, src_pkg: Source, *,
                       profiles_override=None,
                       options_override=None,
                       localmirror: 'Optional[bool]' = None) -> 'Optional[dict]':
        """Assemble the full build recipe for one source package WITHOUT running
        anything — image tag + build-args, the container `cmd_str`, and the
        per-package input descriptors (dsc, source-file prefix, patch dir +
        list, patch_set_hash, component, effective profiles/options).

        Pure: no container, no build-record writes, no prompts.  Both the local
        builder (build(), which bind-mounts + runs here) and the remote
        orchestrator (`source remotebuild`, which ships the bundle to another
        host) call this, so a remote build is byte-identical to a local one.

        Returns None when the source has no .dsc — caller treats as a hard skip
        (mirrors build()'s prior behaviour).
        """
        _active_profiles = (frozenset(profiles_override)
                            if profiles_override is not None
                            else self.config.build_profiles_for(src_pkg.package))
        _active_options = (frozenset(options_override)
                           if options_override is not None
                           else self.config.build_options_for(src_pkg.package))

        _plain_deps: 'list[str]' = []
        _or_groups: 'list[list[str]]' = []
        for _grp in src_pkg.build_depends(self.arch, _active_profiles,
                                          cache=self.cache):
            if not _grp:
                continue
            if len(_grp) == 1:
                _plain_deps.append(_grp[0][0])
            else:
                _or_groups.append([alt[0] for alt in _grp])

        _filename_prefix = src_pkg.package
        try:
            _dsc_file = [f for f in src_pkg.files if f.endswith('.dsc')][0]
        except IndexError:
            logger.error(f"DSC not found for {src_pkg.package}")
            return None

        _deb_build_opts = ' '.join(sorted(_active_options))
        _deb_build_profiles = ' '.join(sorted(_active_profiles))
        deb_build_env = (
            f'DEB_BUILD_OPTIONS="{_deb_build_opts}" '
            f'DEB_BUILD_PROFILES="{_deb_build_profiles}" '
            f'ATHENA_CODENAME="{self.codename}" '
        )

        # Read the patch list fresh from disk so patches added after the last
        # `dep parse` are still picked up.
        _live_patch_dir = os.path.join(
            self.patch_path, src_pkg.package, version_no_epoch(src_pkg.version),
        )
        try:
            _live_patch_list = sorted(
                _f for _f in os.listdir(_live_patch_dir)
                if _f.endswith('.patch')
            )
        except (FileNotFoundError, OSError):
            _live_patch_list = []
        patch_cmd = (
            f'for PATCH in {" ".join(shlex.quote(_p) for _p in _live_patch_list)}; '
            f'do patch -p1 < /patch/"$PATCH"; done; '
            if _live_patch_list else ''
        )
        # Optional version-independent prebuild script — package-specific
        # build environment (exports, setup) that must not land in the image
        # where it would affect every build.  Sourced into the build shell
        # (so exports persist) after unpack+patches, before dpkg-buildpackage;
        # under the recipe's set -e a script error FAILS the build loudly.
        _prebuild_src = utils.prebuild_script_path(
            self.patch_path, src_pkg.package)
        if not os.path.isfile(_prebuild_src):
            _prebuild_src = ''
        prebuild_cmd = (
            'echo "prebuild: sourcing prebuild.sh"; . /prebuild.sh; '
            if _prebuild_src else ''
        )
        _patch_set_hash = utils.patch_set_hash(
            _live_patch_dir, _live_patch_list,
            prebuild_path=_prebuild_src or None)
        _comp = getattr(
            getattr(src_pkg, '_mirror', None), 'component', '') or 'main'
        # patch dir to mount (local) or ship (remote); empty fallback.
        _src_patch_path = (_live_patch_dir if os.path.exists(_live_patch_dir)
                           else self.patch_empty)

        patches_applied_cmd = (
            'if [ -f debian/patches-applied/series ]; then '
            'echo "Applying debian/patches-applied/ series"; '
            'while IFS= read -r p; do '
            '[ -z "$p" ] && continue; '
            '[ "${p#\\#}" != "$p" ] && continue; '
            'p="${p% }"; '
            'patch -p1 -N -i "debian/patches-applied/$p"; '
            'done < debian/patches-applied/series; '
            'fi; '
        )
        _dep_install = self._render_install_cmd(
            _plain_deps, _or_groups, simulate=False)
        _token_subst = (
            '{ find debian -type f 2>/dev/null; '
            'if [ -d data ]; then find data -type f; fi; '
            'if [ -d tasks ]; then find tasks -type f; fi; } '
            "| (xargs -d '\\n' -r grep -lE '@(DISTRIBUTION|BASE_ID|CODENAME)@' "
            '2>/dev/null || true) '
            "| xargs -d '\\n' -r sed -i "
            f"-e 's|@DISTRIBUTION@|{self.build_distribution}|g' "
            f"-e 's|@BASE_ID@|{self.build_base_id}|g' "
            f"-e 's|@CODENAME@|{self.codename}|g'; "
        )
        _write_sources = self._write_snapshot_sources_cmd(localmirror=localmirror)
        # shlex.quote the source-derived names (MAT-04): the package name and
        # .dsc filename come from the (potentially untrusted) source and are
        # interpolated into a root shell.  No-op for legit names; the trailing
        # `*` in the cp glob stays OUTSIDE the quotes so globbing still works.
        _q_prefix = shlex.quote(_filename_prefix)
        _q_dsc = shlex.quote(_dsc_file)
        cmd_str = f'set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'{_write_sources}' \
                  f'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq; ' \
                  f'{_dep_install}' \
                  f'cd /home/athena; cp /source/{_q_prefix}* .; ' \
                  f'dpkg-source -x {_q_dsc} {_q_prefix}; ' \
                  f'cd {_q_prefix}; ' \
                  f'{patches_applied_cmd}' \
                  f'{patch_cmd}' \
                  f'{_token_subst}' \
                  f'{prebuild_cmd}' \
                  f'{deb_build_env} dpkg-checkbuilddeps; {deb_build_env} dpkg-buildpackage -a {self.arch} -b -us -uc -nc; cd ..;' \
                  f'cp *.deb /repo/ 2>/dev/null || true; cp *.udeb /repo/ 2>/dev/null || true ;'

        return {
            'active_profiles': _active_profiles,
            'active_options':  _active_options,
            'plain_deps':      _plain_deps,
            'or_groups':       _or_groups,
            'filename_prefix': _filename_prefix,
            'dsc_file':        _dsc_file,
            'patch_dir':       _src_patch_path,
            'patch_list':      _live_patch_list,
            'patch_set_hash':  _patch_set_hash,
            'prebuild':        _prebuild_src,
            'component':       _comp,
            'cmd_str':         cmd_str,
            'image_tag':       self._image_tag,
            'build_args':      self._image_build_args(),
        }

    def build(self, src_pkg: Source, *,
              profiles_override=None, options_override=None) -> bool:
        """Build a single source package inside the container.

        profiles_override / options_override (keyword-only) replace the
        per-package config defaults (config.build_profiles_for /
        build_options_for) for THIS invocation only.
        Pass an iterable of profile/option names; pass an empty iterable
        for "no profiles/options at all" (the most permissive build,
        includes docs and runs tests).  None means "use the configured
        defaults" (today's behaviour, no override).

        Used by `source_build <pkg> [profiles]` to rebuild a package
        under different profiles than the build.conf default — e.g. drop
        nodoc to actually produce -doc binaries that the default build
        would skip.
        """
        _recipe = self.compose_recipe(
            src_pkg, profiles_override=profiles_override,
            options_override=options_override)
        if _recipe is None:
            return False                       # no .dsc — hard skip
        _active_profiles = _recipe['active_profiles']
        _active_options = _recipe['active_options']
        _plain_deps = _recipe['plain_deps']
        _or_groups = _recipe['or_groups']
        _filename_prefix = _recipe['filename_prefix']
        _live_patch_list = _recipe['patch_list']
        cmd_str = _recipe['cmd_str']
        # Log the EFFECTIVE values (post-precedence), not the raw override args.
        logger.info(
            f"build {src_pkg.package} v{src_pkg.version} "
            f"(profiles={' '.join(sorted(_active_profiles)) or '(none)'}"
            f"{' [override]' if profiles_override is not None else ''}, "
            f"options={' '.join(sorted(_active_options)) or '(none)'}"
            f"{' [override]' if options_override is not None else ''})"
        )

        # opt-in build-dep audit gate.  Runs an apt-get install
        # --simulate preview in a transient container before the real
        # install, shows the resolved set + versions, prompts y/n.
        # Caller gets back False on operator-decline so the build pipeline
        # skips this source.  No-op when [Security] AuditBuildDeps = false
        # (default).
        if self.audit_build_deps:
            if not self._audit_build_deps_gate(
                    src_pkg, _plain_deps, _or_groups):
                return False
        # phase=entry record is the canonical build state from here — a crash
        # before the next phase write classifies as 'interrupted' (rebuild me).
        _t_start = time.monotonic()
        _entry_record = utils.new_build_record(
            package=src_pkg.package,
            intended_version=str(src_pkg.version),
            patch_set_hash=_recipe['patch_set_hash'],
            component=_recipe['component'],
        )
        self._record_phase(src_pkg.package, initial=_entry_record)

        # `container` is initialised to None so the finally block can tell
        # whether containers.run() actually produced a container (failure
        # before that point leaves nothing to clean up).  All exception
        # paths below — APIError, OSError on log writes, KeyboardInterrupt
        # mid-build — flow through the finally so a leftover container can
        # never accumulate in `docker ps -a` between runs.
        container = None
        _res_stop = None     # resource sampler stop event (set in finally)
        # per-worker scratch repo dir.  The container's
        # final `cp *.deb /repo/` writes here, NOT into the shared
        # self.repo_path — so concurrent workers can't race on the
        # `os.listdir(repo_path)` scan inside _segregate_built_artifacts.
        # Segregate moves files from this dir into the real repo
        # subdirs (under _REPO_DEST_LOCK).  Created here, removed in the
        # finally block regardless of build outcome.
        _scratch_dir = os.path.join(
            self.config.dir_build_stage, uuid.uuid4().hex)
        os.makedirs(_scratch_dir, exist_ok=True)
        # observability accumulators — populated through the build
        # and consumed by _write_buildlog at the terminal.  Initialised
        # here so they're in scope on every exit path.
        _seg_events: 'list[tuple]' = []
        _container_name = ''
        _emitted_scan: 'list[tuple[str, int]]' = []
        try:
            src_patch_path = _recipe['patch_dir']

            # Client is non-None by the time build() is called — __init__
            # raises if both the configured and local daemon paths fail.
            assert self.client is not None
            # /source and /patch are READ-ONLY: the build copies the source out
            # (`cp /source/<prefix>* .`) and reads patches (`patch < /patch/…`),
            # never writing back.  A passwordless-root `debian/rules` must not be
            # able to corrupt the host's source/patch trees through the mount
            # (MAT-04).  /repo is the build OUTPUT and stays rw.
            _volumes = {
                self.src_path:    {'bind': '/source', 'mode': 'ro'},
                _scratch_dir:     {'bind': '/repo',   'mode': 'rw'},
                src_patch_path:   {'bind': '/patch',  'mode': 'ro'},
            }
            # The local build mirror is consumed (read-only) by apt inside the
            # container — matches the `file:///localmirror` source written by
            # _write_snapshot_sources_cmd.
            if self._localmirror_active:
                _volumes[self.config.dir_localmirror] = {
                    'bind': '/localmirror', 'mode': 'ro'}
            # Version-independent prebuild script (single-file ro bind) —
            # mount tracks recipe presence: compose_recipe only emits the
            # `. /prebuild.sh` step when the file existed at compose time.
            if _recipe.get('prebuild'):
                _volumes[_recipe['prebuild']] = {
                    'bind': '/prebuild.sh', 'mode': 'ro'}
            container = self.client.containers.run(
                self._image_tag, command=self._container_command(cmd_str),
                detach=True, auto_remove=False,
                labels=self._container_labels,
                environment=_CONTAINER_ENV,
                security_opt=_CONTAINER_SECURITY_OPT,
                volumes=_volumes,
                **self._resource_kwargs(),
            )
            self._register_live(container)
            # sample container resource usage (peak RSS + CPU%) while it
            # runs; the peaks are stamped into the build.json record after wait().
            _res_acc = {'peak_rss_bytes': 0, 'mem_limit_bytes': 0,
                        'peak_cpu_pct': 0.0, 'samples': 0}
            _res_stop = threading.Event()
            threading.Thread(
                target=self._sample_resources,
                args=(container, _res_acc, _res_stop),
                daemon=True, name=f"obs03-{src_pkg.package}").start()
            try:
                _container_name = (
                    f"{container.name} ({container.short_id})")
            except Exception:
                _container_name = ''
            logger.info(
                f"Build container {container.short_id} started for {src_pkg.package}"
            )

            _exit_code = self._stream_and_wait(
                container,
                os.path.join(self.buildlog_path, _filename_prefix),
            )['StatusCode']

            # container exited — stop the sampler and snapshot the peaks.
            if _res_stop is not None:
                _res_stop.set()
            _resources = {
                'peak_rss_bytes': _res_acc['peak_rss_bytes'],
                'peak_rss_mb': round(_res_acc['peak_rss_bytes'] / 1e6, 1),
                'mem_limit_bytes': _res_acc['mem_limit_bytes'] or None,
                'peak_cpu_pct': round(_res_acc['peak_cpu_pct'], 1),
                'samples': _res_acc['samples'],
            }

            # refresh container attrs so OOMKilled is current,
            # then capture both signals.  Docker exposes the OOM-killed
            # flag distinctly from exit code 137 — a real cgroup-OOM has
            # OOMKilled=True; our reap_all_live SIGKILL also produces
            # 137 but with OOMKilled=False. Storing both lets
            # history disambiguate retroactively.
            _oom_killed = False
            try:
                container.reload()
                _oom_killed = bool(
                    container.attrs.get('State', {}).get('OOMKilled', False))
            except _DOCKER_RELOAD_ERRORS as _e:
                # a transient here must not escape — the build is
                # already done; we're only reading the OOM flag.
                logger.warning(
                    f"container.reload failed for {src_pkg.package}: {_e}")

            # exit code 137 = SIGKILL, which docker
            # produces both when the container hits its mem_limit (the
            # cgroup OOM killer fires) and when we force-remove it
            # externally (Phase 3 reap_all_live).  Surface the OOM
            # hint loudly so the operator knows to raise BuildMemory.
            # captures OOMKilled separately so the audit can
            # still tell the two apart even when both produce 137.
            if _exit_code == 137:
                logger.error(
                    f"Build {src_pkg.package} container exited 137 "
                    f"(SIGKILL — likely OOMKilled).  Raise [Build] "
                    f"BuildMemory in config/build.conf if memory is "
                    f"the constraint, or check log/build/{src_pkg.package} "
                    f"for the build's final output")
                tui.console.print(
                    f"  {src_pkg.package}: exit 137 — likely OOMKilled; "
                    f"raise [Build] BuildMemory",
                    tui.COLOR_WARNING)

            _build_result = (_exit_code == 0)

            # phase=container_exited: stamp wall-clock and the
            # container's verdict before any post-processing.  If the
            # process is killed between here and phase=done, the audit
            # sees 'interrupted at container_exited' — strictly more
            # information than just "record missing" = "unknown".
            _elapsed = round(time.monotonic() - _t_start, 3)
            self._record_phase(
                src_pkg.package, phase='container_exited',
                exit_code=_exit_code, oom_killed=_oom_killed,
                finished=utils._utc_now_iso(), elapsed_seconds=_elapsed,
                resources=_resources,
            )

            if not _build_result:
                logger.error(
                    f"Build {src_pkg.package} failed in container "
                    f"{container.short_id} (exit {_exit_code})"
                )
                # Terminal phase=failed for FAIL builds — no segregate
                # or normalize will run.
                self._record_phase(
                    src_pkg.package, phase='failed', output_count=0,
                )
                self._write_buildlog(
                    src_pkg, kind='build', status='FAIL',
                    active_profiles=_active_profiles,
                    active_options=_active_options,
                    plain_deps=_plain_deps, or_groups=_or_groups,
                    container_name=_container_name,
                    exit_code=_exit_code, oom_killed=_oom_killed,
                    elapsed=_elapsed,
                    emitted_scan=_emitted_scan, seg_events=_seg_events,
                    final_paths=[], output_hashes={},
                )

            # On successful build: strip NMU/binNMU/backport suffixes
            # from every .deb/.udeb this build produced, in place.  This
            # normalises both the binary's own Version field and every
            # version constraint in its Depends/Pre-Depends/Recommends/
            # Suggests/Enhances/Provides/Conflicts/Breaks/Replaces fields
            # to the pristine source version.  Result: a corpus where
            # internal cross-refs are by source-version only — no carry-
            # over of Debian's release-cycle metadata into our archive.
            # See utils.strip_nmu_from_deb for the suffix patterns.
            if _build_result:
                # 1. Classify each just-emitted .deb/.udeb at repo/ root
                #    and move it into the right subdir (main/dev/doc/
                #    dbgsym/tests).  See utils.classify_repo_subdir for
                #    the suffix rule.  Done BEFORE strip so the strip's
                #    in-place rewrites land at the final location.
                #    Returns post-move absolute paths — fed to strip so
                #    we don't rescan the whole repo.
                # snapshot what dpkg-buildpackage actually emitted
                # into scratch BEFORE segregate moves it out — the ground
                # truth of "files emitted", with sizes.  Best-effort.
                try:
                    for _ef in os.listdir(_scratch_dir):
                        if _ef.endswith(('.deb', '.udeb')):
                            _emitted_scan.append(
                                (_ef, safe_size(
                                    os.path.join(_scratch_dir, _ef))))
                except OSError as _e:
                    logger.warning(
                        f"buildlog: scratch scan {src_pkg.package}: {_e}")
                _emitted = self._segregate_built_artifacts(
                    src_pkg, _scratch_dir, events=_seg_events)
                # phase=segregated: outputs are now at their
                # final paths; record filenames (basenames — full paths
                # are noise across machines).
                _output_names = sorted(os.path.basename(_p) for _p in _emitted)
                self._record_phase(
                    src_pkg.package, phase='segregated',
                    output_count=len(_output_names), outputs=_output_names,
                )
                # 2. Normalise the emitted .debs (now in their subdirs):
                #    strip NMU → pristine, then stamp +asg<R>u<N> when this
                #    is a delta build AND a remote ledger is loaded.
                #    was_patched feeds the delta decision.  Returns the
                #    POST-NORMALIZE paths — strip + asg-stamp rename the
                #    files in place, so `_emitted` (pre-normalize paths)
                #    no longer points at real files post-normalize.
                # key off the ACTUAL applied .patch files, not the
                # patch-dir's mere existence — an empty dir (operator removed
                # the last .patch but left the dir, or a stray README) would
                # else set _was_delta=True and asg-stamp byte-pristine
                # artifacts, violating Position-X and diverging from the three
                # bool(patch_list) sites (bump.compute_post_build_versions,
                # cmd_source validate, virtual_build) — so `virtual validate`
                # would flag the real build as drift.
                _was_patched = bool(_live_patch_list)
                _final_paths = self._normalize_built_artifacts(
                    src_pkg, _emitted, _was_patched, events=_seg_events)
                if not _final_paths:
                    _final_paths = list(_emitted)
                # Post-strip pristine version is the canonical
                # built_version.  intended_version vs built_version
                # drift becomes visible at this point.
                _built_version = utils.strip_nmu_suffix(str(src_pkg.version))
                # Hash every output AFTER normalize, against the
                # post-rename paths the normalise pass returned.  The
                # prior version iterated `_emitted` (pre-normalize) —
                # for any source whose strip / asg-stamp ACTUALLY
                # renamed files, those paths were stale and
                # get_sha256 returned '' for every output, leaving
                # output_hashes = {} on the build record.  That broke
                # `coord.publish.generate_pending_claims` (which
                # requires a non-empty sha to emit a claim), so
                # ~880 of 985 successfully-built sources never
                # appeared in athena-primary.jsonl.  Caught 2026-06-05.
                # use_cache=False because the files were just written;
                # we don't want a stale (size, mtime) sidecar to lie.
                _output_hashes: 'dict[str, str]' = {}
                _final_basenames: 'list[str]' = []
                for _p in _final_paths:
                    _b = os.path.basename(_p)
                    _final_basenames.append(_b)
                    _h = utils.get_sha256(_p, use_cache=False)
                    if _h:
                        _output_hashes[_b] = _h
                # Also refresh the `outputs` list to the post-normalize
                # filenames — the segregated-phase write captured
                # pre-strip names which no longer exist on disk.
                # resolve the prior-build stash first — when the
                # new built_version supersedes the old (snapshot move /
                # +asg bump) the old episode rolls into history as
                # 'obsolete'; a same-version rebuild just drops the stash.
                try:
                    utils.roll_prior_build_history(
                        self.buildlog_path, src_pkg.package, _built_version)
                except OSError as _e:
                    logger.warning(
                        f"prior-build history roll failed for "
                        f"{src_pkg.package}: {_e}")
                self._record_phase(
                    src_pkg.package, phase='done',
                    built_version=_built_version,
                    output_count=len(_final_basenames),
                    outputs=sorted(_final_basenames),
                    output_hashes=_output_hashes,
                )
                self._write_buildlog(
                    src_pkg, kind='build', status='PASS',
                    active_profiles=_active_profiles,
                    active_options=_active_options,
                    plain_deps=_plain_deps, or_groups=_or_groups,
                    container_name=_container_name,
                    exit_code=_exit_code, oom_killed=_oom_killed,
                    elapsed=_elapsed,
                    emitted_scan=_emitted_scan, seg_events=_seg_events,
                    final_paths=_final_paths, output_hashes=_output_hashes,
                )

            return _build_result

        except _DOCKER_RELOAD_ERRORS as e:
            # _DOCKER_RELOAD_ERRORS = APIError + the RAW urllib3/requests
            # transient timeouts.  A bare `docker.errors.APIError` here would
            # let a urllib3 ReadTimeoutError (NOT an APIError) escape the
            # worker mid-build and crash the thread instead of recording a
            # clean phase=failed — so the audit would see 'interrupted' and
            # silently rebuild.  Catch the transient too and fail cleanly.
            _cid = container.short_id if container is not None else '<not-started>'
            logger.error(
                f"Athena Build Docker error for {src_pkg.package} "
                f"(container {_cid}): {e}"
            )
            tui.console.print(f"Athena Build Docker: Error {e}")
            # flush a terminal phase=failed record so the audit
            # doesn't classify this build as 'interrupted' (which would
            # trigger a silent rebuild on next session).  exit_code=-1
            # is the sentinel for "container died before wait()".
            self._record_phase(
                src_pkg.package, phase='failed', exit_code=-1,
                finished=utils._utc_now_iso(),
                elapsed_seconds=round(time.monotonic() - _t_start, 3),
            )
            return False

        finally:
            # force=True so a still-running container (e.g. interrupted by
            # KeyboardInterrupt or an OSError on the log file) is killed
            # before removal — a non-force remove on a running container
            # raises and would re-leak it.
            if container is not None:
                try:
                    container.remove(force=True)
                except _DOCKER_RELOAD_ERRORS as e:
                    # Cleanup failure is non-fatal — surface but do not
                    # mask the original exception or build result.  CRITICAL:
                    # catch the RAW urllib3/requests transient timeouts too,
                    # not just docker.errors.APIError.  When a heavy container
                    # (e.g. libreoffice) is saturating dockerd, a sibling
                    # build's force-remove can block past the client read
                    # timeout and surface a urllib3 ReadTimeoutError.  The
                    # build itself already SUCCEEDED (phase=done written
                    # above); a bare APIError catch would let that timeout
                    # escape the finally, mask the successful return, and the
                    # worker would be tallied as FAILED.  Swallow it — the
                    # container is left for reap_all_live to sweep.
                    logger.warning(
                        f"Failed to remove container {container.short_id} "
                        f"for {src_pkg.package}: {e}"
                    )
                # drop from registry AFTER the remove
                # attempt so reap_all_live (Phase 3) can still find the
                # container if it fires before we get here.  Deregister
                # always — even on remove failure, the container is no
                # longer being managed by this worker.
                self._deregister_live(container)
            # ensure the resource sampler can't outlive the container
            # (it's a daemon thread, but stop it promptly on every exit path).
            if _res_stop is not None:
                _res_stop.set()
            # rmtree the per-worker scratch dir even on
            # failure paths (the container may have copied .debs in
            # before crashing).  ignore_errors so a fs issue here can't
            # mask the original exception or build result.
            shutil.rmtree(_scratch_dir, ignore_errors=True)

    @staticmethod
    def _render_install_cmd(plain_deps: 'list[str]',
                            or_groups: 'list[list[str]]', *,
                            simulate: bool) -> str:
        """Render the apt install shell command(s) for a build's build-deps.

        Single source of truth for both the real install (build() main
        container script) and the simulate preview. `simulate=True`
        inserts `--simulate` into every apt-get install invocation; the
        rendered command shape (plain deps + OR-group fallback chains) is
        otherwise identical.
        """
        _retry = '-o Acquire::Retries=5 '
        _flag = '--simulate ' if simulate else ''
        # --no-install-recommends: a build environment installs ONLY the
        # declared Build-Depends + their hard Depends — exactly what Debian's
        # sbuild/pbuilder do.  Without it apt also pulls each build-dep's
        # Recommends (e.g. imagemagick → fonts-texgyre, dvisvgm, java/perl doc
        # libs), which compute_build_closure (Depends + Pre-Depends only) does
        # NOT predict → those land OUTSIDE the localmirror and fall back to
        # snapshot.  Pinning the build to the Depends closure both leans the env
        # AND lets the localmirror cover it completely.
        _norec = '--no-install-recommends '
        # shlex.quote every build-dep NAME (MAT-04): package names come from the
        # source's debian/control and are interpolated into a root shell — a
        # no-op for legit Debian names (their charset has no shell metachars),
        # defense-in-depth against a malicious/untrusted source name.
        _plain = ''
        if plain_deps:
            _names = ' '.join(shlex.quote(_d) for _d in plain_deps)
            _plain = (
                f'sudo DEBIAN_FRONTEND=noninteractive apt -y {_norec}{_flag}'
                f'{_retry}install {_names}; '
            )
        _ors: 'list[str]' = []
        for _grp in or_groups:
            _chain = (
                f' || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
                f'{_norec}{_flag}{_retry}').join(
                    shlex.quote(_alt) for _alt in _grp)
            _ors.append(
                f'{{ sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
                f'{_norec}{_flag}{_retry}{_chain}; }}'
            )
        return _plain + ('; '.join(_ors) + '; ' if _ors else '')

    def _audit_build_deps_gate(
        self, src_pkg: Source,
        plain_deps: 'list[str]',
        or_groups: 'list[list[str]]',
    ) -> bool:
        """Gate: run `apt-get install --simulate` in a transient
        container, print the captured output, prompt operator y/n.

        Returns True to proceed with the real build, False to skip this
        source (operator declined OR preview infrastructure failed —
        either way we don't run the real install with un-audited deps).

        When there are no build-deps to install, returns True without
        showing a prompt (nothing to audit).
        """
        if not plain_deps and not or_groups:
            return True

        _preview_text = self._capture_apt_simulate(
            src_pkg, plain_deps, or_groups)
        if _preview_text is None:
            tui.console.print(
                f"build-dep preview FAILED for {src_pkg.package} "
                f"— skipping build per audit-gate policy.  Set "
                f"[Security] AuditBuildDeps = false to bypass.",
                tui.COLOR_ERROR)
            return False

        tui.console.print(
            f"\n=== build-dep preview: {src_pkg.package} ==="
            f"\n  ({len(plain_deps)} plain dep(s), {len(or_groups)} "
            f"OR-group(s) — apt -y --simulate output below)\n"
            f"{_preview_text}"
            f"=== end preview ===\n")

        from tui import Prompt as _Prompt, PROMPT_YESNO as _YN
        _resp = _Prompt(
            _YN,
            f"Proceed with build of {src_pkg.package}?  "
            f"(operator-gated per [Security] AuditBuildDeps)"
        ).get_response()
        if _resp.lower() not in ('y', 'yes'):
            tui.console.print(
                f"operator declined — skipping {src_pkg.package}.",
                tui.COLOR_WARNING)
            logger.warning(
                f"build of {src_pkg.package} skipped by operator")
            return False
        return True

    def _capture_apt_simulate(
        self, src_pkg: Source,
        plain_deps: 'list[str]',
        or_groups: 'list[list[str]]',
    ) -> 'Optional[str]':
        """Helper for the gate. Runs a transient container that
        writes the snapshot-pinned sources.list, runs `apt-get update`,
        then `apt-get install -y --simulate <deps>`.  Captures container
        stdout/stderr and returns the combined text.  Returns None if the
        preview container fails to start or the simulate exits non-zero
        with no useful output (caller treats None as a hard skip).
        """
        _write_sources = self._write_snapshot_sources_cmd()
        _simulate_cmd = self._render_install_cmd(
            plain_deps, or_groups, simulate=True)
        cmd_str = (
            f'set -e; '
            f'{_write_sources}'
            f'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq; '
            f'{_simulate_cmd}'
        )
        container = None
        try:
            assert self.client is not None
            container = self.client.containers.run(
                self._image_tag, command=self._container_command(cmd_str),
                detach=True, auto_remove=False,
                labels=self._container_labels,
                environment=_CONTAINER_ENV,
                security_opt=_CONTAINER_SECURITY_OPT,
            )
            self._register_live(container)
            _buf: bytes = b''
            try:
                for _line in container.logs(stream=True):
                    _buf += _line
            except _DOCKER_TRANSIENT:
                pass   # short audit-gate op; fall through to the wait poll
            _exit = self._wait_for_exit(container)['StatusCode']
            if not _buf:
                try:
                    _buf = container.logs(stream=False)
                except Exception:
                    _buf = b''
            _text = _buf.decode('utf-8', errors='replace')
            if _exit != 0:
                logger.warning(
                    f"build-dep preview {src_pkg.package}: container exited "
                    f"{_exit}; tail: {_text[-400:]}")
                # honour the docstring contract — a non-zero exit
                # with no useful output returns None (caller treats as a hard
                # skip).  Non-empty output is still surfaced for diagnosis.
                if not _text.strip():
                    return None
            return _text
        except docker.errors.APIError as e:
            logger.error(
                f"build-dep preview {src_pkg.package}: docker error: {e}")
            return None
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.APIError:
                    pass
                self._deregister_live(container)

    def run_grub_mkrescue(self, staging_dir: str, output_iso: str,
                            password: str) -> 'tuple[bool, str, str]':
        """Run grub-mkrescue inside the build container so the produced
        ISO embeds BOOKWORM's GRUB toolchain instead of the host's.

        Fix path (b).  Eliminates host-GRUB contamination when
        the build host runs a non-bookworm release (e.g. trixie ships
        GRUB 2.12 vs our pinned 2.06).  The container's apt is already
        pinned to OUR snapshot (see self.mirrors), so
        `apt-get install grub-{common,pc-bin,efi-amd64-bin}` resolves
        to the bookworm versions in the snapshot.  grub-mkrescue then
        produces a hybrid BIOS+EFI image using OUR toolchain, with no
        leakage of the host's GRUB into the produced ISO's bootloader.

        Args:
            staging_dir: ISO source tree (boot/grub/grub.cfg + assets
                         + pool/ + .disk/).  Mounted into the container
                         at /staging.
            output_iso:  Output ISO path on the host.  Its parent dir
                         is mounted at /output rw; grub-mkrescue
                         writes the file there.  Resulting file's
                         owner is normalised back to the invoking
                         host UID via `sudo chown` (grub-mkrescue runs
                         as root inside the container to read any
                         root-owned files in /staging).
            password:    Host sudo password — currently unused inside
                         this method (container has passwordless sudo
                         per Dockerfile) but accepted for symmetry
                         with other ISO helpers that DO need it.

        Returns (ok, stdout, stderr) for the caller to log and surface.
        """
        del password   # noqa: F841 — see Args.password rationale
        logger.info(
            f"run_grub_mkrescue: staging={staging_dir} → {output_iso} (in container)"
        )

        # Path-prep + cmd construction
        _output_dir   = os.path.dirname(os.path.abspath(output_iso))
        _iso_basename = os.path.basename(output_iso)

        # Pin container apt to the same snapshot the cache was built
        # from — re-uses self.mirrors which already carry the snapshot
        # timestamp (set during BuildContainer.__init__).  This is how
        # `apt-get install grub-{common,pc-bin,efi-amd64-bin}` resolves
        # to OUR 2.06 binaries instead of whatever bookworm-archive is
        # currently serving (which might have drifted post-snapshot).
        _write_sources = self._write_snapshot_sources_cmd()

        # apt-get install: GRUB toolchain (mkrescue, mkimage, modules
        # for BIOS + EFI) plus the boot-image assemblers grub-mkrescue
        # invokes internally — xorriso for the ISO, mtools + dosfstools
        # for the FAT-formatted EFI System Partition image.
        import arch_profile as _ap
        _grub_bins = ' '.join(_ap.profile(self.arch).grub_bins)
        _apt_install = (
            'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
            'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
            f'grub-common {_grub_bins} '
            'xorriso mtools dosfstools'
        )

        # grub-mkrescue runs as root: /staging may carry root-owned
        # files from sudo cp steps on the host (kernel, initrd,
        # pool/*.deb extracted from chroot tarballs).  The container's
        # athena user can't read those even though the mount is rw.
        _mkrescue = f'sudo grub-mkrescue -o /output/{_iso_basename} /staging'

        # Normalise the produced ISO's ownership back to the operator's
        # uid so they can read/move it without sudo.  os.getuid/getgid
        # reflects whoever invoked build-system.sh; container's root
        # (uid 0 on host) writes the file, then chowns it.
        _chown_host = (
            f'sudo chown {os.getuid()}:{os.getgid()} /output/{_iso_basename}'
        )

        # set -e — `&&`-chain semantics; any step's failure aborts.
        cmd_str = (
            'set -e; '
            f'{_write_sources}'
            f'{_apt_install} && '
            f'{_mkrescue} && '
            f'{_chown_host}'
        )

        # Same init + finally cleanup pattern as build() for container
        # lifecycle.  Container leaks on KeyboardInterrupt would
        # accumulate in `docker ps -a` between runs otherwise.
        container = None
        try:
            assert self.client is not None
            _grub_volumes = {
                staging_dir: {'bind': '/staging', 'mode': 'rw'},
                _output_dir: {'bind': '/output',  'mode': 'rw'},
            }
            # cmd_str writes the apt sources via _write_snapshot_sources_cmd,
            # which — when the local build mirror is active — emits a
            # `file:///localmirror` source that `apt-get update` reads before
            # installing the grub toolchain.  It MUST be bind-mounted (as
            # build() does) or apt fails "file:/localmirror/./Packages File
            # not found" and grub-mkrescue exits 100 (regression once the
            # local mirror was enabled, 2026-07-10).
            if getattr(self, '_localmirror_active', False):
                _grub_volumes[self.config.dir_localmirror] = {
                    'bind': '/localmirror', 'mode': 'ro'}
            container = self.client.containers.run(
                self._image_tag,
                command=['/bin/bash', '-c', cmd_str],
                detach=True, auto_remove=False,
                labels=self._container_labels,
                environment=_CONTAINER_ENV,
                security_opt=_CONTAINER_SECURITY_OPT,
                volumes=_grub_volumes,
            )
            self._register_live(container)
            logger.info(
                f"grub-mkrescue container {container.short_id} started "
                f"(staging={staging_dir}, output={output_iso})"
            )
            _result = self._wait_for_exit(container)
            _exit_code = _result.get('StatusCode', -1)
            # the container has EXITED (_wait_for_exit is
            # transient-resilient); a hiccup reading its now-complete logs
            # must not turn a finished ISO master into a failure.  Fall
            # back to empty log text — _exit_code is the source of truth.
            try:
                _stdout = container.logs(stdout=True,  stderr=False).decode(
                    'utf-8', errors='replace')
                _stderr = container.logs(stdout=False, stderr=True).decode(
                    'utf-8', errors='replace')
            except _DOCKER_TRANSIENT as _e:
                logger.warning(
                    f"grub-mkrescue: log read hiccup ({_e}); "
                    f"using exit code {_exit_code}")
                _stdout, _stderr = '', ''
            if _exit_code != 0:
                logger.error(
                    f"grub-mkrescue container {container.short_id} "
                    f"exited {_exit_code}"
                )
            return (_exit_code == 0, _stdout, _stderr)
        except docker.errors.APIError as e:
            _cid = container.short_id if container is not None else '<not-started>'
            logger.error(
                f"Docker API error running grub-mkrescue "
                f"(container {_cid}): {e}"
            )
            return (False, '', f'Docker API error: {e}')
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.APIError as e:
                    logger.warning(
                        f"Failed to remove grub-mkrescue container "
                        f"{container.short_id}: {e}"
                    )
                self._deregister_live(container)

    def _normalize_built_artifacts(self, src_pkg,
                                   built_files: 'list[str]',
                                   was_patched: bool = False,
                                   events: 'Optional[list]' = None
                                   ) -> 'list[str]':
        """Normalise every just-emitted artifact by TRANSPOSING its version in
        place: a trailing upstream update marker (`+debNuK` / `~debNuK`) becomes
        our own (`+asg<R>uK` / `~asg<R>uK`), then our patch level (`+pP`) and any
        forced rebuild (`+bN`) are appended.

        `built_files` is the list of post-segregate absolute paths returned by
        _segregate_built_artifacts — the files this source build just produced.

        The update number K is intrinsic to each binary's own upstream version,
        so there is no ledger and no ship-order counter, and a faithful pristine
        rebuild (no trailing marker) stays pristine — no delta/lineage decision.
        K is uniform across a source's binaries; P/force come from the build
        record.  Dependency constraints are transposed the same way and
        same-source sibling pins are restamped to their exact final version,
        all inside `transpose_deb`.

        Failures are logged but don't propagate — best-effort normalisation.

        When ``events`` is a list, this appends ``('stamp', old, new, version)``
        observability tuples — purely additive, never alters control flow.
        """
        if not built_files:
            return []
        # TRANSPOSE scheme (content-order).  Each binary's version is
        # transposed in place — a TRAILING +debNuK token becomes +asg<R>uK
        # (K intrinsic to the upstream version, so no ledger and no ship-order
        # counter), then our patch level P (+pP) and any force-binNMU (+bN) are
        # appended.  A faithful pristine rebuild has no trailing +deb, so
        # transpose is a no-op and it stays pristine — no separate delta/lineage
        # decision is needed (the old ship-order lineage-regression class can't
        # arise because +deb→+asg is deterministic from the upstream version,
        # not from ship history).  Dep constraints transpose the same way and
        # same-source sibling pins are restamped to the exact final version, all
        # inside transpose_deb.  See docs/versioning-mechanics.md.
        try:
            _release = int(str(self.config.build_version).strip('"').strip("'"))
        except (TypeError, ValueError):
            logger.warning(
                "transpose: [Build] VERSION is not an integer "
                f"({self.config.build_version!r}) — shipping pristine for "
                f"{src_pkg.package}")
            return list(built_files)

        # P (patch level) and force-binNMU level come from this source's build
        # record (stamped by _record_phase via decide_patch_bump_count).
        _buildlog = getattr(self, 'buildlog_path', None)
        _record = (utils.read_build_record(_buildlog, src_pkg.package)
                   if _buildlog else None)
        try:
            _patch_level = int((_record or {}).get('patch_bump_count', 0) or 0)
        except (TypeError, ValueError):
            _patch_level = 0
        try:
            _bn_count = int((_record or {}).get('bn_bump_count', 0) or 0)
        except (TypeError, ValueError):
            _bn_count = 0
        _force_bn = _bn_count if _bn_count > 0 else None
        # was_patched is the live signal; if the record's counter is missing but
        # we DID patch, floor P at 1 so patched bytes never ship unmarked.
        if was_patched and _patch_level == 0:
            _patch_level = 1

        # The names of every binary this source just emitted — so transpose_deb
        # can restamp same-source sibling `=` pins to their exact final version,
        # including siblings that carry a different base than this binary.
        _sibling_names = set()
        for _path in built_files:
            _df = utils.parse_deb_filename(os.path.basename(_path))
            if _df is not None:
                _sibling_names.add(_df[0])

        # A rebuilt package's bound can target a TUNNELLED binary, whose
        # shipped version keeps its +bN/backport layer — exempt those bounds
        # from the constraint strip (empty set when no cache is wired).
        _keep_bn = utils.tunneled_binary_names(self.config, self.cache)
        # STA-55: the universe hook for the target-aware Breaks/Conflicts
        # ceiling demotion (None without a cache — standard op).
        _uni = utils.cache_universe_lookup(self.cache)

        _current_paths: 'list[str]' = []
        for _path in built_files:
            _f = os.path.basename(_path)
            try:
                _r = utils.transpose_deb(
                    _path, 'asg', _release,
                    patch_level=_patch_level, force_bn=_force_bn,
                    sibling_names=_sibling_names,
                    keep_binnmu_names=_keep_bn,
                    universe_lookup=_uni)
                _new = _r.get('new_path', _path)
                if _r['status'] == 'rewritten' and _new != _path:
                    logger.info(
                        f"transpose: {_f} → {os.path.basename(_new)}")
                    if events is not None:
                        events.append(('stamp', _f, os.path.basename(_new),
                                       str(_r.get('version', ''))))
                _current_paths.append(_new)
            except Exception as e:
                logger.warning(f"transpose: {_f} failed: {e}")
                _current_paths.append(_path)
        return list(_current_paths)

    def _segregate_built_artifacts(self, src_pkg,
                                   source_dir: str,
                                   events: 'Optional[list]' = None
                                   ) -> 'list[str]':
        """After dpkg-buildpackage's `cp *.deb /repo/`, the binaries
        land in the worker's PER-BUILD scratch dir (`source_dir`,
        mounted as /repo inside the container — see build()).  This
        pass classifies each by name and moves it to the right
        repo subdir per utils.classify_repo_subdir:

          main    installable binaries + udebs
          dev     -dev side artifacts
          doc     -doc side artifacts
          dbgsym  -dbgsym side artifacts
          tests   -test / -tests side artifacts

        Contract: reads ONLY from `source_dir` —
        never the shared repo root.  Concurrent workers see disjoint
        scratch dirs; the destination-side moves are serialised by
        `_REPO_DEST_LOCK` so two workers can't race on the same
        component dir's collision check.  All-or-nothing per source:
        if any move fails, every move already done for THIS source
        is rolled back into source_dir and the function returns [].

        Returns the list of post-move absolute paths so the caller
        (_normalize_built_artifacts) can iterate only the
        just-emitted files (fix — was rescanning the whole
        repo + opening every .deb with DebFile to identify them).
        Returns empty list on no-files-in-scratch (tunneled build,
        empty build, or rolled-back partial failure).

        When ``events`` is a list, this appends observability
        tuples ``('relocate', filename, dest_dir)`` per moved file and
        ``('purge', filename, reason)`` per dropped duplicate — purely
        additive, never alters the move/rollback control flow.
        """
        _moved_paths: 'list[str]' = []
        # Files already present in a published dir on an exact-name (byte-
        # identical) rebuild: kept in place, NOT moved.  Tracked separately
        # from _moved_paths so the rollback loop below never renames a
        # published artifact out of the repo — doing so would DESTROY it,
        # because build()'s finally-block rmtrees source_dir.
        _kept_existing: 'list[str]' = []
        try:
            _files = [
                _f for _f in os.listdir(source_dir)
                if (_f.endswith(('.deb', '.udeb')))
                and os.path.isfile(os.path.join(source_dir, _f))
            ]
        except OSError as e:
            logger.warning(
                f"segregate: cannot list {source_dir}: {e}"
            )
            return _moved_paths
        if not _files:
            return _moved_paths
        # A source's binaries share its apt component (from the origin mirror).
        # Forks / locally-discovered sources have no mirror (or a flat fork
        # mirror) → 'main'.  Tunneled non-main packages don't reach here
        # (_do_tunnel places them directly), but keep this correct/general.
        _comp = getattr(getattr(src_pkg, '_mirror', None), 'component', '') or 'main'
        with _REPO_DEST_LOCK:
            for _f in _files:
                _src = os.path.join(source_dir, _f)
                # config helper routes to the right
                # nested apt-repo dir (e.g. main → dists/<codename>/main/
                # binary-<arch>/, main+.udeb → main/debian-installer/...,
                # dbgsym → dists/<codename>-debug/main/binary-<arch>/).
                _dst_dir = self.config.deb_dest_for_filename(_f, _comp)
                _dst = os.path.join(_dst_dir, _f)
                try:
                    os.makedirs(_dst_dir, exist_ok=True)
                    if os.path.exists(_dst):
                        # append-only invariant: an automatic build
                        # path must NEVER os.remove a file already living in
                        # a published dir (all_deb_dirs()).  With +asg<R>u<N>
                        # a rebuilt delta emits a NEW filename, so an exact-
                        # name collision means a byte-identical rebuild —
                        # keep the existing artifact, drop the freshly-built
                        # dup (which sits in the worker's scratch dir, a
                        # prunable intermediate that the build()
                        # finally-block rmtrees anyway).
                        #
                        # CRITICAL: still record `_dst` in `_kept_existing`
                        # (merged into the return below) so the build flow
                        # (normalize, output_hashes, repo audit downstream)
                        # SEES the artifact.  Without this, the build record's
                        # `outputs` / `output_hashes` are silently incomplete:
                        # the kept-existing file is real and downstream
                        # treats absence-from-record as "wasn't built"
                        # (caught 2026-06-06 — 497 udebs on disk, 0 of
                        # 985 build.json records carried any udeb
                        # reference, breaking virtual validate's
                        # over-prediction analysis).  It must NOT go in
                        # `_moved_paths`: it was never moved out of a
                        # published dir, so the rollback loop must never
                        # rename it back into (and thus delete) it.
                        logger.warning(
                            f"segregate: {_f} already present in "
                            f"{_dst_dir} — keeping existing (append-only), "
                            f"dropping rebuilt dup"
                        )
                        os.remove(_src)
                        _kept_existing.append(_dst)
                        if events is not None:
                            events.append(
                                ('purge', _f,
                                 f"duplicate — kept existing in {_dst_dir}"))
                        continue
                    os.rename(_src, _dst)
                    _moved_paths.append(_dst)
                    if events is not None:
                        events.append(('relocate', _f, _dst_dir))
                except OSError as e:
                    # All-or-nothing per source: roll back every move done
                    # for THIS call so the caller sees a clean empty list
                    # (no partial _normalize / asg-stamp on a half-populated
                    # set, no partial signed-manifest entry for).
                    logger.warning(
                        f"segregate: failed to move {_f} → {_dst_dir}: {e} "
                        f"(rolling back {len(_moved_paths)} prior move(s))"
                    )
                    for _done_dst in _moved_paths:
                        _back = os.path.join(
                            source_dir, os.path.basename(_done_dst))
                        try:
                            os.rename(_done_dst, _back)
                        except OSError as _re:
                            logger.error(
                                f"segregate: rollback of {_done_dst} → "
                                f"{_back} also failed: {_re}"
                            )
                    return []
        if _moved_paths or _kept_existing:
            logger.info(
                f"segregate: {len(_moved_paths)} artifact(s) from "
                f"{src_pkg.package} placed in repo/ subdirs"
                + (f" ({len(_kept_existing)} kept existing)"
                   if _kept_existing else "")
            )
        # Merge kept-existing into the return so downstream output tracking
        # sees every real artifact; rollback above only ever touched
        # genuinely-moved files (_moved_paths), never these.
        return _moved_paths + _kept_existing

    def check_build(self, src_pkg: Source,
                    expected_files: 'list[str]') -> bool:
        """Decide whether a previously-built source can skip rebuild.

        Shallow gate — kept fast because this runs on every source
        build call across the full dep tree:

          1. expected_files is non-empty
          2. log/build/<src>.build.json classifies as 'ok' or 'tunneled'
          3. Every predicted INSTALLABLE binary (classified to main)
             in expected_files exists at repo/main/<file> AND is a
             syntactically valid ar archive (is_ar_file).

        expected_files is the union across both dep_trees of binaries
        this source is predicted to produce — caller passes
        dep_tree.src_pkg_files.get(src) + udeb_dep_tree.src_pkg_files.get(src).
        We can't read them off src_pkg because Source objects are
        shared across trees via cache.source_hashtable; the per-tree
        prediction lives in each DependencyTree.src_pkg_files map.

        Missing -dev/-doc/-dbgsym/-tests artifacts do NOT trigger
        rebuild — they're side artifacts that don't install anywhere,
        and re-running a 30-min source build to repopulate a missing
        -doc file is wasteful.  Only missing installable (main)
        artifacts gate rebuild.

        Does NOT verify internal Version / Depends resolution —
        those checks live in verify_pkg_artifact, exposed as the
        opt-in `source verify` diagnostic command.  History: the
        deeper check was added 2026-05-19 then reverted same day
        because the 12h rebuild cost dominated the false-positive
        risk; deep audit is now opt-in via `source verify`.
        """
        if not expected_files:
            return False

        # the signed build.json record is the sole source of
        # truth for "has this been built".  An interrupted record
        # (non-terminal phase) or missing record is treated as "not
        # built" — the audit's classify_build_record returns 'missing'
        # / 'interrupted' / 'fail' for everything except 'ok' and
        # 'tunneled'.
        _record = utils.read_build_record(self.buildlog_path, src_pkg.package)
        if _record is None:
            return False
        _cls = utils.classify_build_record(_record)
        if _cls not in ('ok', 'tunneled'):
            return False
        # A tunnelled binNMU keeps its upstream +bN on disk (transpose_deb
        # frozen-pin rule) — the pristine prediction only matches with the
        # tunnelled acceptance.  Without it, the package re-tunnels EVERY
        # run and source audit reads stale_pass (ffmpegthumbnailer,
        # 2026-07-12).  Rebuilt records keep the strict gate.
        _allow_bn = (_cls == 'tunneled')

        # Component (from origin mirror) so a non-main package's binaries are
        # located in their component dir — e.g. a TUNNELED firmware package
        # lives in repo/dists/<codename>/non-free-firmware/, not main; without
        # this the presence check would always miss and re-tunnel every run.
        _comp = getattr(getattr(src_pkg, '_mirror', None), 'component', '') or 'main'
        for _file in expected_files:
            # Only main-classified binaries gate rebuild; missing
            # -dev/-doc/-dbgsym/-tests are tolerated. Stage D:
            # deb_dest_for_filename returns the new nested location
            # (main → dists/<codename>/main/binary-<arch>/ for .deb,
            # → debian-installer/binary-<arch>/ for .udeb).
            _sub = utils.classify_repo_subdir(_file)
            if _sub != 'main':
                continue
            # accept the predicted pristine name OR a +asg<R>u<N>
            # stamped variant of it (find_matching_artifact).  The old
            # exact-only os.path.isfile match was the rebuild-loop
            # cause: a stamped/ABI-variant on-disk file never matched, so the
            # source was rebuilt every run.
            _dst_dir = self.config.deb_dest_for_filename(_file, _comp)
            _filename = utils.find_matching_artifact(
                _dst_dir, _file, allow_binnmu=_allow_bn)
            if _filename is None:
                return False
            if not self.is_ar_file(_filename):
                return False

        return True

    def verify_pkg_artifact(self, deb_path: str,
                            expected_filename: str,
                            repo_state: 'Optional[RepoState]' = None,
                            ) -> 'Tuple[bool, str]':
        """Deep-verify a single .deb / .udeb against its predicted
        filename + a target resolution scope (repo or cache).

        Checks (in order, short-circuits on first failure):

          1. File exists at deb_path
          2. Valid ar archive (is_ar_file)
          3. Internal `Package:` field == filename's pkg part
          4. Internal `Version:` field == filename's version part
          5. Internal `Architecture:` field matches filename's arch part
             (also accepts `all` to match any arch suffix — for arch-
             independent .debs whose filename uses the build arch)
          6. Every Depends + Pre-Depends OR-group has at least one
             alternative resolvable in the chosen scope.

        Resolution scope:
          - When `repo_state` is provided (a `repo_audit.RepoState`),
            deps resolve against the repo's post-strip pristine versions
            — which is what apt sees at install time on the installed
            system.  This is the authoritative scope: the cache reflects
            UPSTREAM's NMU-bumped versions (`libfoo 1.0-1+b1`), so a
            strict-equal sibling `Depends: libfoo (= 1.0-1)` on a
            post-strip .deb would falsely flag unsatisfied against
            cache while installing fine against repo.  Honours Provides
            per Debian Policy §7.5 via
            `repo_audit._rel_satisfied_in_scope`.
          - When `repo_state` is None, falls back to
            `self.cache.package_hashtable` / `udeb_hashtable` (cache
            scope).  Skipped when both are None (no scope to verify
            against; returns 'ok-nocache').

        Returns (True, 'ok') on full pass; (False, diagnostic) on
        first failure.  Diagnostic format: short-token:detail for
        machine-friendly logging.

        Used by `cmd_source_verify` (which passes repo_state for the
        authoritative pre-ship check) and historically by check_build /
        cmd_source_repair (which pass cache for cheap availability
        checks).
        """
        if not os.path.isfile(deb_path):
            return (False, 'missing')
        if not self.is_ar_file(deb_path):
            return (False, 'not-ar')

        # Parse expected_filename: pkg_VERSION_arch.{deb,udeb}
        _base = os.path.basename(expected_filename)
        _r = utils.parse_deb_filename(_base)
        if _r is None:
            return (False, f'bad-filename-shape:{_base}')
        _exp_pkg, _exp_ver, _exp_arch, _exp_ext = _r

        # Read .deb's internal control area
        try:
            from debian.debfile import DebFile
            with DebFile(deb_path) as _deb:
                _ctrl = _deb.control.debcontrol()
        except Exception as e:
            return (False, f'unreadable:{type(e).__name__}:{e}')

        _actual_pkg = _ctrl.get('Package', '')
        if _actual_pkg != _exp_pkg:
            return (False, f'pkg-mismatch:{_actual_pkg}!={_exp_pkg}')

        # Version comparison: strip epoch from the .deb's internal
        # Version before comparing to the filename's version part.
        # Filename convention strips epoch (`libc6_2.36-9..._amd64.deb`
        # for a package whose internal Version is `2:2.36-9...`), so
        # raw `==` would false-positive every epoch-bearing pkg as
        # mismatched.  Use the existing version_no_epoch helper to
        # canonicalise.
        _actual_ver = _ctrl.get('Version', '')
        _actual_ver_noepoch = version_no_epoch(_actual_ver)
        if _actual_ver_noepoch != _exp_ver:
            return (False, f'version-mismatch:{_actual_ver}!={_exp_ver}')

        _actual_arch = _ctrl.get('Architecture', '')
        # arch-independent (`all`) .debs can sit under any arch's filename
        if _actual_arch not in (_exp_arch, 'all'):
            return (False, f'arch-mismatch:{_actual_arch}!={_exp_arch}')

        # Dep resolution.  Prefer repo_state (authoritative — matches
        # what apt sees on the installed system); fall back to cache
        # (legacy path; over-reports on the NMU-bump-vs-strict-equal-
        # sibling scenario because the cache reflects upstream's bumped
        # versions while our .debs are at pristine post-strip).
        from debian.deb822 import PkgRelation

        if repo_state is not None:
            from repo_audit import _rel_satisfied_in_scope
            for _field in ('Depends', 'Pre-Depends'):
                _deps_str = _ctrl.get(_field, '')
                if not _deps_str or '${' in _deps_str:
                    continue
                try:
                    _or_groups = PkgRelation.parse_relations(_deps_str)
                except Exception:
                    continue
                for _or_group in _or_groups:
                    if not any(_rel_satisfied_in_scope(repo_state, _r)
                               for _r in _or_group):
                        _names = ' | '.join(_d.get('name', '?')
                                            for _d in _or_group)
                        return (False, f'unsatisfied-{_field}:{_names}')
            return (True, 'ok')

        # Cache-scope fallback — dep availability checked against the
        # apt cache when no RepoState is supplied.  Production passes
        # repo_state (authoritative); unit fixtures exercise this scope.
        if self.cache is None:
            return (True, 'ok-nocache')

        # Pick the right hashtable: .udeb's Depends typically resolve
        # via udeb_hashtable (the d-i parallel namespace).  .deb's
        # via package_hashtable.  Mixing produces false unsatisfied-
        # Depends for udeb-only deps that don't appear in the deb
        # hashtable.
        _is_udeb = expected_filename.endswith('.udeb')
        _lookup_table = (self.cache.udeb_hashtable if _is_udeb
                         else self.cache.package_hashtable)

        for _field in ('Depends', 'Pre-Depends'):
            _deps_str = _ctrl.get(_field, '')
            if not _deps_str:
                continue
            # Skip unresolved substvars (defensive — shouldn't appear
            # in a properly-built .deb, but if dpkg-gencontrol left
            # `${shlibs:Depends}` literal, that's a build bug not a
            # cache-drift bug; don't flag here).
            if '${' in _deps_str:
                continue
            try:
                _or_groups = PkgRelation.parse_relations(_deps_str)
            except Exception:
                # Malformed Depends — be permissive (don't fail the
                # whole artifact verify on a parse glitch).
                continue
            for _or_group in _or_groups:
                if not self._or_group_satisfiable(_or_group, _lookup_table):
                    _names = ' | '.join(_d.get('name', '?')
                                        for _d in _or_group)
                    return (False, f'unsatisfied-{_field}:{_names}')

        return (True, 'ok')

    def _or_group_satisfiable(self, or_group: list,
                              lookup_table: Optional[dict] = None) -> bool:
        """One OR-group (parsed by PkgRelation) is satisfied iff at
        least one alternative has a candidate in cache meeting its
        version constraint.

        lookup_table picks the namespace: pass cache.package_hashtable
        for .deb deps, cache.udeb_hashtable for .udeb deps.  None
        defaults to package_hashtable.
        """
        if not self.cache:
            return True
        if lookup_table is None:
            lookup_table = self.cache.package_hashtable
        for _alt in or_group:
            _name = _alt.get('name', '')
            if not _name:
                continue
            _ver_constraint = _alt.get('version')  # (op, ver) or None
            _bin_table = lookup_table.get(_name, {})
            for _candidate_ver in _bin_table.keys():
                if self._satisfies_version(_candidate_ver, _ver_constraint):
                    return True
        return False

    @staticmethod
    def _satisfies_version(version_str: str,
                           constraint: 'Optional[Tuple[str, str]]') -> bool:
        """version_str matches constraint (op, target_ver) using
        dpkg version semantics.  None constraint means any version
        is OK."""
        if constraint is None:
            return True
        _op, _target = constraint
        if not _target:
            return True
        try:
            from debian.debian_support import Version
            _v = Version(version_str)
            _t = Version(_target)
        except Exception:
            return False
        if _op == '<<': return _v <  _t
        if _op == '<=': return _v <= _t
        if _op in ('=', '=='): return _v == _t
        if _op == '>=': return _v >= _t
        if _op == '>>': return _v >  _t
        return False

    @staticmethod
    def is_ar_file(filename: str) -> bool:
        """Confirm `filename` is a syntactically valid `.deb` archive
        (an `ar` archive containing `debian-binary`, a compressed
        `control.tar.*`, and a compressed `data.tar.*`).

        Used by `check_build` to decide whether a previous run's `.deb`
        on disk can stand in for re-running the build.

        Backed by `python-debian.debfile.DebFile` — a single, audited
        parser shared with apt/dpkg ecosystem tooling, replacing this
        method's previous hand-rolled `ar`-format reader.
        """
        try:
            from debian.debfile import DebFile
            with DebFile(filename):
                return True
        except (FileNotFoundError, PermissionError):
            return False
        except Exception as e:
            # ArError / DebError / unexpected EOFs all bubble up here;
            # surface to the log tab so a malformed .deb can be traced
            # without deeper debugging.
            logger.error(f"is_ar_file({filename}): {type(e).__name__}: {e}")
            return False
