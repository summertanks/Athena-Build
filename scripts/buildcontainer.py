
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional, Tuple
import utils
from utils import BuildConfig, version_no_epoch
from package import Source

import docker
import docker.errors  # noqa: F401 — explicit import so `docker.errors.X` resolves under mypy
import tui

if TYPE_CHECKING:
    # Type-only import — verify_pkg_artifact takes an optional RepoState
    # parameter, resolved at call time via a lazy `import repo_audit`
    # inside the function body so the runtime dep stays optional.
    from repo_audit import RepoState

logger = logging.getLogger('athena.build')

# COMP-03 Phase 1: serialises segregate's per-file moves from
# every worker's scratch dir into the shared repo subdirs.  Held
# only for the duration of one source's move loop (microseconds);
# isolates the os.makedirs / collision-check / os.rename triad so
# two workers can't race on the same dest filename or component dir.
_REPO_DEST_LOCK = threading.Lock()


class BuildContainer:

    def __init__(self, config: BuildConfig, docker_server=None, cache=None):

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
        # CONF-01 Stage D: keep the full config reference so segregate
        # can route .deb / .udeb destinations via config.deb_dest_for_
        # filename (which knows about the new unified apt-repo layout).
        # Pre-CONF-01 code only needed the path strings below.
        self.config = config

        self.build_path = config.dir_repo
        self.src_path = config.dir_source
        self.log_path = config.dir_log
        self.repo_path = config.dir_repo
        self.arch = config.arch
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
        self.build_profiles = config.build_profiles
        self.build_options  = config.build_options
        # SEC-05: when true, build() runs a `apt-get install --simulate`
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

        self.client = None

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
                _client = docker.DockerClient(base_url=docker_server)
                _client.ping()
                self.client = _client
            except docker.errors.APIError:
                tui.console.print("Athena Build Docker: Couldn't connect to external server, reverting to local")

        if self.client is None:
            try:
                self.client = docker.from_env()
                self.client.ping()
            except docker.errors.APIError as e:
                logger.error(f"Athena Build Docker: Error {e}")
                tui.console.print(f"Athena Build Docker: Error {e}")
                raise RuntimeError(f"Cannot connect to local Docker daemon: {e}") from e

        # CONF-15: bake the snapshot TS into the image tag so a snapshot
        # advance ([Snapshot] Timestamp change in build.conf) invalidates
        # the image cache automatically — `docker images` lookup misses
        # the old tag, we build a fresh image against the new snapshot.
        # Pre-CONF-15 the tag was `athenalinux:build-<release>`; an
        # operator's `[Snapshot] Timestamp` change wouldn't invalidate
        # the existing image even though its toolchain layer was now
        # against an older snapshot.
        self._image_tag = (
            f"athenalinux:build-{config.container_release}-{self.snapshot_ts}")
        _image_tag = self._image_tag
        dockerfile_hash = self._hash_dockerfile(config.dir_config)
        _needs_build = False

        try:
            image = self.client.images.get(_image_tag)
            stored_hash = image.labels.get('athena.dockerfile.sha256', '')

            if stored_hash != dockerfile_hash:
                tui.console.print(f"Dockerfile changed — rebuilding {_image_tag}")
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
            try:
                # CONF-15: pass the snapshot triplet as build-args so the
                # Dockerfile's first RUN can rewrite /etc/apt/sources.list
                # to OUR snapshot BEFORE any `apt-get install` (so even the
                # toolchain layer is pinned, not just per-build steps).
                image, build_logs = self.client.images.build(
                    path=config.dir_config, tag=_image_tag,
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
                    labels={'athena.dockerfile.sha256': dockerfile_hash},
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

        self.image = image

        # COMP-03 Phase 2: live-container registry.  Every container
        # spawned by this BuildContainer (build / SEC-05 preview /
        # grub-mkrescue) registers here on `containers.run()` return and
        # deregisters in its finally block, so Phase 3's reap_all_live()
        # has an in-process list to force-remove on SIGINT.
        # Lock guards the dict against concurrent register/deregister
        # under the parallel ThreadPoolExecutor (Phase 4).
        self._live: 'dict[str, docker.models.containers.Container]' = {}
        self._live_lock = threading.Lock()
        # COMP-03 Phase 3: shutdown_event is set by request_shutdown()
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
        # UPD-01 stamping ledger: {package_name: [asg-stamped versions
        # already published]}.  Set from build.py around _do_update_build
        # before each update-mode build and cleared after; remains None
        # for a plain (non-update) `source build` so the stamper passes
        # through pristine.  Declared here for type narrowing — the
        # runtime setters and the stamper consumer both read this name.
        self.asg_ledger: 'dict[str, list[str]] | None' = None

        # COMP-03 Phase 1: sweep any per-worker scratch dirs left over
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

        # COMP-03 Phase 2: sweep any leftover docker containers from a
        # prior run that didn't reach its build()/run_grub_mkrescue/
        # capture finally-block (kill -9 / SIGSEGV / daemon restart).
        # Filter by our owner label so we never touch unrelated
        # containers running on the host.  Best-effort.
        try:
            assert self.client is not None
            _orphans = self.client.containers.list(
                all=True, filters={'label': 'com.athena.build=1'})
            for _c in _orphans:
                try:
                    _c.remove(force=True)
                except docker.errors.APIError as e:
                    logger.warning(
                        f"orphan-reap: cannot remove {_c.short_id}: {e}")
            if _orphans:
                tui.console.print(
                    f"BuildContainer: reaped {len(_orphans)} orphan "
                    f"container(s) from previous run")
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
        from urllib.parse import urlparse

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

    def _resource_kwargs(self) -> dict:
        """COMP-03 Phase 5: per-container CPU + RAM caps for
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
        """COMP-03 Phase 2: track a freshly-started container in the
        in-process registry.  Phase 3's reap_all_live() iterates this
        registry under _live_lock to force-remove every owned
        container on SIGINT.  Idempotent (re-registering an existing
        short_id is a no-op).
        """
        with self._live_lock:
            self._live[container.short_id] = container

    def _deregister_live(self, container) -> None:
        """COMP-03 Phase 2: drop a container from the registry after
        its build()/preview/grub-mkrescue finally-block reaches the
        normal cleanup path.  Idempotent (a missing key is fine —
        reap_all_live() may have removed it concurrently).
        """
        with self._live_lock:
            self._live.pop(container.short_id, None)

    def _record_phase(self, package: str, *, initial: 'Optional[dict]' = None,
                      **fields: object) -> None:
        """OBS-01 phase transition.  Best-effort: a record-write OSError
        must never mask a build result, so we swallow and log.

        Pass `initial=<dict>` to write the entry-phase record (creates
        the file).  Subsequent transitions pass only `fields` (read the
        existing record, merge, re-sign, atomic-replace).
        """
        try:
            if initial is not None:
                utils.write_build_record(self.buildlog_path, initial)
            else:
                utils.update_build_record(self.buildlog_path, package, **fields)
        except (OSError, FileNotFoundError) as _e:
            logger.warning(
                f"build-record write failed for {package} "
                f"({fields.get('phase', 'initial')}): {_e}")

    def reap_all_live(self) -> int:
        """COMP-03 Phase 3: force-remove every container currently in
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
        """COMP-03 Phase 3: signal every parallel worker to stop and
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
        dockerfile = os.path.join(config_dir, 'Dockerfile')
        try:
            with open(dockerfile, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ''

    def _write_snapshot_sources_cmd(self) -> str:
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
        _apt_sources = ''.join(
            f'deb [check-valid-until=no] {_m.url} {_m.suite} {_m.component}\n'
            for _m in self.mirrors
        )
        # `origin snapshot.debian.org` matches the host of the mirror's
        # URL.  Distinct from the Origin: header inside InRelease (which
        # is `Origin: Debian` / `Origin: Debian-Security`).  This pin is
        # broad — every pkg from a snapshot.debian.org URL wins.
        _apt_pin = (
            "Package: *\n"
            "Pin: origin snapshot.debian.org\n"
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

    def build(self, src_pkg: Source, *,
              profiles_override=None, options_override=None) -> bool:
        """Build a single source package inside the container.

        profiles_override / options_override (keyword-only) replace
        self.build_profiles / self.build_options for THIS invocation only.
        Pass an iterable of profile/option names; pass an empty iterable
        for "no profiles/options at all" (the most permissive build,
        includes docs and runs tests).  None means "use the configured
        defaults" (today's behaviour, no override).

        Used by `source_build <pkg> [profiles]` to rebuild a package
        under different profiles than the build.conf default — e.g. drop
        nodoc to actually produce -doc binaries that the default build
        would skip.
        """
        logger.info(
            f"build {src_pkg.package} v{src_pkg.version} "
            f"(profiles={profiles_override}, options={options_override})"
        )

        _plain_deps: 'list[str]' = []
        _or_groups: 'list[list[str]]' = []
        # ARCH-16: per-pkg override precedence —
        #   per-invocation `[bracket]` token  (profiles_override / options_override)
        #     ↓ falls back to ↓
        #   `[Source.<pkg>]` block in build.conf  (config.build_*_for(pkg))
        #     ↓ falls back to ↓
        #   global `[Source]` block in build.conf  (config.build_*)
        _active_profiles = (frozenset(profiles_override)
                            if profiles_override is not None
                            else self.config.build_profiles_for(src_pkg.package))
        _active_options = (frozenset(options_override)
                           if options_override is not None
                           else self.config.build_options_for(src_pkg.package))

        for _grp in src_pkg.build_depends(self.arch, _active_profiles, cache=self.cache):
            if not _grp:
                continue
            if len(_grp) == 1:
                _plain_deps.append(_grp[0][0])
            else:
                _or_groups.append([alt[0] for alt in _grp])

        # SEC-05: opt-in build-dep audit gate.  Runs an apt-get install
        # --simulate preview in a transient container before the real
        # install, shows the resolved set + versions, prompts y/n.
        # Caller gets back False on operator-decline so the build pipeline
        # skips this source.  No-op when [Security] AuditBuildDeps = false
        # (default).
        if self.audit_build_deps:
            if not self._audit_build_deps_gate(
                    src_pkg, _plain_deps, _or_groups):
                return False
        _filename_prefix = src_pkg.package
        _dsc_file = ''

        try:
            _dsc_file = [file for file in src_pkg.files if file.endswith('.dsc')][0]
        except IndexError:
            logger.error(f"DSC not found for {src_pkg.package}")
            return False

        # DEB_BUILD_OPTIONS and DEB_BUILD_PROFILES are different namespaces:
        # options control build-time behaviour (nodoc, nocheck,
        # parallel=N), profiles activate Build-Depends annotations like
        # `<!nodoc>` and `<!stage1>`.  Source.build_depends has already had
        # `_active_profiles` applied above for build-dep filtering; the env
        # vars below propagate the right values into dpkg-buildpackage.
        _deb_build_opts     = ' '.join(sorted(_active_options))
        _deb_build_profiles = ' '.join(sorted(_active_profiles))
        # ATHENA_CODENAME (FORK-01 Step 4): forks under fork/source/ that
        # need to substitute the distribution codename into shipped files
        # (lsb-release, default-release, debootstrap script symlinks) read
        # this in their debian/rules via $(ATHENA_CODENAME).  Sourced from
        # config/build.conf [Build] CODENAME via BuildConfig.build_codename.
        # Harmless for upstream pkgs that don't reference it.
        deb_build_env = (
            f'DEB_BUILD_OPTIONS="{_deb_build_opts}" '
            f'DEB_BUILD_PROFILES="{_deb_build_profiles}" '
            f'ATHENA_CODENAME="{self.codename}" '
        )

        # Read the patch list fresh from disk at build time so patches
        # added AFTER the last `dep parse` run are still picked up.
        # The cached `src_pkg.patch_list` (set by `_refresh_patches`
        # during dep parse) goes stale the moment an operator drops a
        # new patch file in `patch/source/<pkg>/<ver>/` — and there's
        # no way for them to know they need to re-run `dep parse force`
        # before `source build`.  Symptom: build re-runs with the same
        # error as before, looking like the patch silently didn't
        # apply.  Closes the long-standing TODO that lived here.
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
            f'for PATCH in {" ".join(_live_patch_list)}; do patch -p1 < /patch/"$PATCH"; done; '
            if _live_patch_list else ''
        )

        # OBS-01: compute the patch_set_hash once up front and stamp the
        # entry-phase record.  The build.json file is the canonical
        # build state from this point — any crash before the next phase
        # write leaves the record at phase=entry, which the audit
        # classifies as 'interrupted' (signal: rebuild me).
        _patch_set_hash = utils.patch_set_hash(
            _live_patch_dir, _live_patch_list,
        )
        _t_start = time.monotonic()
        _entry_record = utils.new_build_record(
            package=src_pkg.package,
            intended_version=str(src_pkg.version),
            patch_set_hash=_patch_set_hash,
        )
        self._record_phase(src_pkg.package, initial=_entry_record)

        # A few packages (notably pam) ship a second quilt-managed patch series
        # at debian/patches-applied/series in addition to debian/patches/series.
        # dpkg-source only applies debian/patches/series during 3.0 (quilt)
        # extraction, and dh_quilt_patch in such packages silently no-ops because
        # the .pc/ directory is already pinned to debian/patches/.  The result
        # is a half-patched source tree (e.g. pam without 031_pam_include, which
        # adds @include directive support to libpam → broken /etc/pam.d/login).
        #
        # Apply debian/patches-applied/ ourselves before our custom /patch/
        # patches and before dpkg-buildpackage.  For packages without this
        # directory the [ -f ... ] guard skips the loop entirely.
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

        # Token substitution: replace @DISTRIBUTION@, @BASE_ID@,
        # @CODENAME@ in fork content with values from BuildConfig.
        # This is THE mechanism by which fork packages get branded:
        # their debian/control Description, data/lsb-release strings,
        # data/*.templates fields, tasks/* descriptions, etc. carry
        # tokens; we resolve them here once per build.
        #
        # Scope: debian/, data/, and tasks/ subdirs.  Substitution
        # runs AFTER patches have been applied (so patches can also
        # use tokens) but BEFORE _changelog_bump (so the bump
        # operates on post-substitution source).
        #
        # Selectivity: a grep-first filter finds files that actually
        # contain a token, then xargs sed runs only on those.
        # Upstream packages have no tokens → grep finds nothing →
        # sed never runs → zero-cost no-op.
        #
        # See memory/project_three_layer_identity.md for the model.
        # Two non-obvious shell choices, both because cmd_str runs
        # under `set -e -o pipefail`:
        #
        # 1. `if [ -d X ]; then find X; fi` for the optional data/ +
        #    tasks/ dirs (not `[ -d X ] && find X`).  With `&&`, a
        #    missing dir leaves the brace group's last command exit
        #    at 1; pipefail picks that up and set -e kills the build.
        #    `if`-blocks return 0 when the condition is false.
        #
        # 2. `(grep -lE … || true)` wrapper for the grep filter.
        #    `grep -l` exits 1 when it finds NO matches — true for
        #    every upstream package (none carry @TOKENS@).  Under
        #    pipefail, the pipeline inherits that 1 and set -e kills.
        #    The `|| true` rescues the no-match case while still
        #    letting actual grep errors (filesystem) propagate as
        #    non-zero — but since `|| true` flattens any non-zero to
        #    0, we accept this trade for the much more common no-
        #    match path.
        # Bash brace-group syntax: `{ cmd1; cmd2; }` needs SPACE after
        # the opening `{` and a `;` (or newline) before the closing `}`.
        # Regular (non-f) Python strings here pass literal `{` and `}`
        # through.  An earlier draft wrote `{{ ... }}` thinking Python's
        # f-string escape would collapse to single braces — but these
        # lines are NOT f-strings, so bash saw literal `{{` and exited
        # 127 ("command not found").  Fixed 2026-05-18.
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

        # Pin the container's apt to the exact mirrors our cache was built
        # from.  Without this the base image's stock sources (live mirror)
        # are used, and a security update landing between our cache
        # snapshot and the build run will produce dep version skew between
        # the .deb we build and what the cache thinks.
        #
        # `[check-valid-until=no]` — snapshot.debian.org's InRelease files
        # carry a Valid-Until ~7 days after Date (replay-attack defense in
        # the normal apt flow).  We intentionally pin to a fixed snapshot
        # for reproducibility, so the file naturally goes "expired" as the
        # snapshot ages.  Override is safe here because:
        #   1. The build container only ever talks to our snapshot URLs
        #      (no other apt sources to be tricked into replaying).
        #   2. We pin a content-hashed snapshot by timestamp — the file
        #      is already the trusted artifact, not a stream from a live
        #      mirror.
        # When the snapshot rolls forward, this option silently becomes
        # a no-op (current InRelease is within Valid-Until again).
        _write_sources = self._write_snapshot_sources_cmd()

        # `-b` (--build=binary) skips the source rebuild step.  No
        # version-bump is performed; the produced .debs ship at the
        # pristine upstream source version.  Post-build, the BuildContainer
        # runs utils.strip_nmu_from_deb on every produced artifact to
        # normalise the Version field + all dep-constraint version
        # references — stripping +bN, +debNuN, ~bpoN+N, +rpiN, etc.
        # so internal cross-refs resolve cleanly inside our repo.
        #
        # CONF-15: the per-build `apt-get -y --allow-downgrades dist-upgrade`
        # step that used to live here was removed once the toolchain layer
        # was pinned to the snapshot at image-build time (Dockerfile +
        # __init__ buildargs).  The image's installed packages are already
        # at snapshot's view; per-build sources.list expands to the full
        # mirror set (main + security + updates) at the SAME snapshot TS,
        # so apt-get update + the per-source apt-get install resolve
        # entirely within the snapshot universe with no drift to correct.
        # If a future change reintroduces drift (e.g. moving back to a
        # live-mirror base image), bring this back as `sudo apt-get -y
        # -o Acquire::Retries=5 --allow-downgrades dist-upgrade;`
        # between _write_sources and _dep_install.
        cmd_str = f'set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'{_write_sources}' \
                  f'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq; ' \
                  f'{_dep_install}' \
                  f'cd /home/athena; cp /source/{_filename_prefix}* .; ' \
                  f'dpkg-source -x {_dsc_file} {_filename_prefix}; ' \
                  f'cd {_filename_prefix}; ' \
                  f'{patches_applied_cmd}' \
                  f'{patch_cmd}' \
                  f'{_token_subst}' \
                  f'{deb_build_env} dpkg-checkbuilddeps; {deb_build_env} dpkg-buildpackage -a {self.arch} -b -us -uc -nc; cd ..;' \
                  f'cp *.deb /repo/ 2>/dev/null || true; cp *.udeb /repo/ 2>/dev/null || true ;'

        # `container` is initialised to None so the finally block can tell
        # whether containers.run() actually produced a container (failure
        # before that point leaves nothing to clean up).  All exception
        # paths below — APIError, OSError on log writes, KeyboardInterrupt
        # mid-build — flow through the finally so a leftover container can
        # never accumulate in `docker ps -a` between runs.
        container = None
        # COMP-03 Phase 1: per-worker scratch repo dir.  The container's
        # final `cp *.deb /repo/` writes here, NOT into the shared
        # self.repo_path — so concurrent workers can't race on the
        # `os.listdir(repo_path)` scan inside _segregate_built_artifacts.
        # Segregate moves files from this dir into the real repo
        # subdirs (under _REPO_DEST_LOCK).  Created here, removed in the
        # finally block regardless of build outcome.
        _scratch_dir = os.path.join(
            self.config.dir_build_stage, uuid.uuid4().hex)
        os.makedirs(_scratch_dir, exist_ok=True)
        try:
            src_patch_path = os.path.join(self.patch_path, src_pkg.package, version_no_epoch(src_pkg.version))
            if not os.path.exists(src_patch_path):
                src_patch_path = self.patch_empty

            # Client is non-None by the time build() is called — __init__
            # raises if both the configured and local daemon paths fail.
            assert self.client is not None
            container = self.client.containers.run(
                self._image_tag, command=["/bin/bash", "-c", cmd_str],
                detach=True, auto_remove=False,
                labels=self._container_labels,
                volumes={
                    self.src_path:    {'bind': '/source', 'mode': 'rw'},
                    _scratch_dir:     {'bind': '/repo',   'mode': 'rw'},
                    src_patch_path:   {'bind': '/patch',  'mode': 'rw'},
                },
                **self._resource_kwargs(),
            )
            self._register_live(container)
            logger.info(
                f"Build container {container.short_id} started for {src_pkg.package}"
            )

            with open(os.path.join(self.buildlog_path, _filename_prefix), 'w') as fh:
                for line in container.logs(stream=True):
                    fh.write(line.decode("utf-8"))

            _exit_code = container.wait()['StatusCode']

            # OBS-01: refresh container attrs so OOMKilled is current,
            # then capture both signals.  Docker exposes the OOM-killed
            # flag distinctly from exit code 137 — a real cgroup-OOM has
            # OOMKilled=True; our reap_all_live SIGKILL also produces
            # 137 but with OOMKilled=False.  Storing both lets OBS-02
            # history disambiguate retroactively.
            _oom_killed = False
            try:
                container.reload()
                _oom_killed = bool(
                    container.attrs.get('State', {}).get('OOMKilled', False))
            except docker.errors.APIError as _e:
                logger.warning(
                    f"container.reload failed for {src_pkg.package}: {_e}")

            # COMP-03 Phase 5: exit code 137 = SIGKILL, which docker
            # produces both when the container hits its mem_limit (the
            # cgroup OOM killer fires) and when we force-remove it
            # externally (Phase 3 reap_all_live).  Surface the OOM
            # hint loudly so the operator knows to raise BuildMemory.
            # OBS-01 captures OOMKilled separately so the audit can
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

            # OBS-01 phase=container_exited: stamp wall-clock and the
            # container's verdict before any post-processing.  If the
            # process is killed between here and phase=done, the audit
            # sees 'interrupted at container_exited' — strictly more
            # information than just "record missing" = "unknown".
            _elapsed = round(time.monotonic() - _t_start, 3)
            self._record_phase(
                src_pkg.package, phase='container_exited',
                exit_code=_exit_code, oom_killed=_oom_killed,
                finished=utils._utc_now_iso(), elapsed_seconds=_elapsed,
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
                #    we don't rescan the whole repo (STA-19).
                _emitted = self._segregate_built_artifacts(
                    src_pkg, _scratch_dir)
                # OBS-01 phase=segregated: outputs are now at their
                # final paths; record filenames (basenames — full paths
                # are noise across machines).
                _output_names = sorted(os.path.basename(_p) for _p in _emitted)
                self._record_phase(
                    src_pkg.package, phase='segregated',
                    output_count=len(_output_names), outputs=_output_names,
                )
                # 2. Normalise the emitted .debs (now in their subdirs):
                #    strip NMU → pristine, then stamp +asg<R>u<N> when this
                #    is a delta build AND a remote ledger is loaded (UPD-01).
                #    was_patched feeds the delta decision.
                _was_patched = (src_patch_path != self.patch_empty)
                self._normalize_built_artifacts(src_pkg, _emitted, _was_patched)
                # OBS-01 phase=normalized → done: post-strip pristine
                # version is the canonical built_version (memory
                # `feedback_strip_nmu_at_build`).  intended_version vs
                # built_version drift becomes visible at this point.
                _built_version = utils.strip_nmu_suffix(str(src_pkg.version))
                # COORD-01: hash every output AFTER normalize.  Normalize
                # rewrites the .deb in place (NMU strip + asg-stamp), so
                # the published bytes — and thus the digest the coord
                # claim record pins — are only stable here.  use_cache=
                # False because the file was just written; we don't want
                # a stale (size, mtime) sidecar to lie about content.
                _output_hashes = {}
                for _p in _emitted:
                    _h = utils.get_sha256(_p, use_cache=False)
                    if _h:
                        _output_hashes[os.path.basename(_p)] = _h
                self._record_phase(
                    src_pkg.package, phase='done',
                    built_version=_built_version,
                    output_hashes=_output_hashes,
                )

            return _build_result

        except docker.errors.APIError as e:
            _cid = container.short_id if container is not None else '<not-started>'
            logger.error(
                f"Athena Build Docker error for {src_pkg.package} "
                f"(container {_cid}): {e}"
            )
            tui.console.print(f"Athena Build Docker: Error {e}")
            # OBS-01: flush a terminal phase=failed record so the audit
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
                except docker.errors.APIError as e:
                    # Cleanup failure is non-fatal — surface but do not
                    # mask the original exception or build result.
                    logger.warning(
                        f"Failed to remove container {container.short_id} "
                        f"for {src_pkg.package}: {e}"
                    )
                # COMP-03 Phase 2: drop from registry AFTER the remove
                # attempt so reap_all_live (Phase 3) can still find the
                # container if it fires before we get here.  Deregister
                # always — even on remove failure, the container is no
                # longer being managed by this worker.
                self._deregister_live(container)
            # COMP-03 Phase 1: rmtree the per-worker scratch dir even on
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
        container script) and the SEC-05 simulate preview.  `simulate=True`
        inserts `--simulate` into every apt-get install invocation; the
        rendered command shape (plain deps + OR-group fallback chains) is
        otherwise identical.
        """
        _retry = '-o Acquire::Retries=5 '
        _flag = '--simulate ' if simulate else ''
        _plain = ''
        if plain_deps:
            _plain = (
                f'sudo DEBIAN_FRONTEND=noninteractive apt -y {_flag}{_retry}'
                f'install {" ".join(plain_deps)}; '
            )
        _ors: 'list[str]' = []
        for _grp in or_groups:
            _chain = (
                f' || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
                f'{_flag}{_retry}').join(_grp)
            _ors.append(
                f'{{ sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
                f'{_flag}{_retry}{_chain}; }}'
            )
        return _plain + ('; '.join(_ors) + '; ' if _ors else '')

    def _audit_build_deps_gate(
        self, src_pkg: Source,
        plain_deps: 'list[str]',
        or_groups: 'list[list[str]]',
    ) -> bool:
        """SEC-05 gate: run `apt-get install --simulate` in a transient
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
                f"SEC-05: build-dep preview FAILED for {src_pkg.package} "
                f"— skipping build per audit-gate policy.  Set "
                f"[Security] AuditBuildDeps = false to bypass.",
                tui.COLOR_ERROR)
            return False

        tui.console.print(
            f"\n=== SEC-05 build-dep preview: {src_pkg.package} ==="
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
                f"SEC-05: operator declined — skipping {src_pkg.package}.",
                tui.COLOR_WARNING)
            logger.warning(
                f"SEC-05: build of {src_pkg.package} skipped by operator")
            return False
        return True

    def _capture_apt_simulate(
        self, src_pkg: Source,
        plain_deps: 'list[str]',
        or_groups: 'list[list[str]]',
    ) -> 'Optional[str]':
        """Helper for the SEC-05 gate.  Runs a transient container that
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
                self._image_tag, command=["/bin/bash", "-c", cmd_str],
                detach=True, auto_remove=False,
                labels=self._container_labels,
            )
            self._register_live(container)
            _buf: bytes = b''
            for _line in container.logs(stream=True):
                _buf += _line
            _exit = container.wait()['StatusCode']
            _text = _buf.decode('utf-8', errors='replace')
            if _exit != 0:
                logger.warning(
                    f"SEC-05 preview {src_pkg.package}: container exited "
                    f"{_exit}; tail: {_text[-400:]}")
                # Surface the partial output anyway — operator may still
                # want to see what apt resolved before the failure.
            return _text
        except docker.errors.APIError as e:
            logger.error(
                f"SEC-05 preview {src_pkg.package}: docker error: {e}")
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

        COMP-14 fix path (b).  Eliminates host-GRUB contamination when
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
        _apt_install = (
            'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
            'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y '
            'grub-common grub-pc-bin grub-efi-amd64-bin '
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
            container = self.client.containers.run(
                self._image_tag,
                command=['/bin/bash', '-c', cmd_str],
                detach=True, auto_remove=False,
                labels=self._container_labels,
                volumes={
                    staging_dir: {'bind': '/staging', 'mode': 'rw'},
                    _output_dir: {'bind': '/output',  'mode': 'rw'},
                },
            )
            self._register_live(container)
            logger.info(
                f"grub-mkrescue container {container.short_id} started "
                f"(staging={staging_dir}, output={output_iso})"
            )
            _result = container.wait()
            _exit_code = _result.get('StatusCode', -1)
            _stdout = container.logs(stdout=True,  stderr=False).decode(
                'utf-8', errors='replace')
            _stderr = container.logs(stdout=False, stderr=True).decode(
                'utf-8', errors='replace')
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
                                   was_patched: bool = False) -> None:
        """Normalise every just-emitted artifact: strip upstream NMU layers
        to pristine, then (when this build is a DELTA and a remote ledger is
        available) stamp our +asg<R>u<N> update marker.

        `built_files` is the list of post-segregate absolute paths returned by
        _segregate_built_artifacts — the files this source build just
        produced (STA-19: we trust that list, no full-repo rescan).

        DELTA decision (UPD-01): a build is a delta if the strip rewrote any
        artifact (upstream carried an NMU layer), OR a fork patch was applied
        (`was_patched`), OR the selected source version is itself an NMU
        delta.  Only deltas get stamped — a plain pristine upstream build (and
        the initial full build at the pinned snapshot) ships pristine.

        Stamping requires the ledger (self.asg_ledger: {pkg: [versions]}, the
        LOCAL signed published manifest) so N is monotonic against what's
        ALREADY published — local repo/ is single-snapshot and can't be the
        source of truth.  When no ledger is set (a plain `source build`, not a
        `repo refresh`), we strip only and do NOT stamp.  N is derived PER
        BINARY FILE (each file's own highest published N + 1) — a file updated
        more often than its siblings carries a higher N (e.g. foo at u5 while
        foo-data is at u1).

        Failures are logged but don't propagate — best-effort normalisation;
        a missed strip surfaces later via `repo audit_nmu`.
        """
        if not built_files:
            return
        # --- 1. strip NMU → pristine (track delta + post-strip paths) ---
        _any_stripped = False
        _current_paths: 'list[str]' = []
        for _path in built_files:
            _f = os.path.basename(_path)
            try:
                _r = utils.strip_nmu_from_deb(_path)
                if _r['status'] == 'rewritten':
                    _any_stripped = True
                    if _r['new_path'] != _path:
                        logger.info(
                            f"strip_nmu: {_f} → "
                            f"{os.path.basename(_r['new_path'])}")
                _current_paths.append(_r.get('new_path', _path))
            except Exception as e:
                logger.warning(f"strip_nmu: {_f} failed: {e}")
                _current_paths.append(_path)

        # --- 2. decide delta; stamp +asg<R>u<N> if delta AND ledger present ---
        _ledger = getattr(self, 'asg_ledger', None)
        _src_is_delta = (
            utils.strip_nmu_suffix(str(src_pkg.version)) != str(src_pkg.version))
        _was_delta = _any_stripped or was_patched or _src_is_delta
        if not (_was_delta and _ledger is not None):
            return

        try:
            _release = int(str(self.config.build_version).strip('"').strip("'"))
        except (TypeError, ValueError):
            logger.warning(
                f"asg-stamp: [Build] VERSION is not an integer "
                f"({self.config.build_version!r}) — skipping stamp for "
                f"{src_pkg.package}")
            return

        # Per-file N: each binary gets its own +asg<R>u<N> from its OWN
        # published history (NOT a shared per-source max), so a file updated
        # more often than its siblings carries a higher N.
        _n_stamped = 0
        for _path in _current_paths:
            _b = os.path.basename(_path)
            _name, _ext = os.path.splitext(_b)
            _parts = _name.split('_')
            if len(_parts) != 3:
                continue
            _pkg, _ver, _arch = _parts
            _base = utils.pristine_base(_ver)
            _n = utils.asg_next_n(_ledger.get(_pkg, []), _base, _release)
            try:
                _r = utils.restamp_asg_deb(_path, _release, _n)
                if _r['status'] == 'rewritten':
                    _n_stamped += 1
                    logger.info(
                        f"asg-stamp: {_b} → "
                        f"{os.path.basename(_r['new_path'])} (+asg{_release}u{_n})")
            except Exception as e:
                logger.warning(f"asg-stamp: {_b} failed: {e}")
        if _n_stamped:
            logger.info(
                f"asg-stamp: marked {_n_stamped} artifact(s) from source "
                f"{src_pkg.package} (+asg{_release}, per-file N)")

    def _segregate_built_artifacts(self, src_pkg,
                                   source_dir: str) -> 'list[str]':
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

        COMP-03 Phase 1 contract: reads ONLY from `source_dir` —
        never the shared repo root.  Concurrent workers see disjoint
        scratch dirs; the destination-side moves are serialised by
        `_REPO_DEST_LOCK` so two workers can't race on the same
        component dir's collision check.  All-or-nothing per source:
        if any move fails, every move already done for THIS source
        is rolled back into source_dir and the function returns [].

        Returns the list of post-move absolute paths so the caller
        (_normalize_built_artifacts) can iterate only the
        just-emitted files (STA-19 fix — was rescanning the whole
        repo + opening every .deb with DebFile to identify them).
        Returns empty list on no-files-in-scratch (tunneled build,
        empty build, or rolled-back partial failure).
        """
        _moved_paths: 'list[str]' = []
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
                # CONF-01 Stage D: config helper routes to the right
                # nested apt-repo dir (e.g. main → dists/<codename>/main/
                # binary-<arch>/, main+.udeb → main/debian-installer/...,
                # dbgsym → dists/<codename>-debug/main/binary-<arch>/).
                _dst_dir = self.config.deb_dest_for_filename(_f, _comp)
                _dst = os.path.join(_dst_dir, _f)
                try:
                    os.makedirs(_dst_dir, exist_ok=True)
                    if os.path.exists(_dst):
                        # UPD-01 append-only invariant: an automatic build
                        # path must NEVER os.remove a file already living in
                        # a published dir (all_deb_dirs()).  With +asg<R>u<N>
                        # a rebuilt delta emits a NEW filename, so an exact-
                        # name collision means a byte-identical rebuild —
                        # keep the existing artifact, drop the freshly-built
                        # dup (which sits in the worker's scratch dir, a
                        # prunable intermediate that the build()
                        # finally-block rmtrees anyway).
                        logger.warning(
                            f"segregate: {_f} already present in "
                            f"{_dst_dir} — keeping existing (append-only), "
                            f"dropping rebuilt dup"
                        )
                        os.remove(_src)
                        continue
                    os.rename(_src, _dst)
                    _moved_paths.append(_dst)
                except OSError as e:
                    # All-or-nothing per source: roll back every move done
                    # for THIS call so the caller sees a clean empty list
                    # (no partial _normalize / asg-stamp on a half-populated
                    # set, no partial signed-manifest entry for STA-21).
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
        if _moved_paths:
            logger.info(
                f"segregate: {len(_moved_paths)} artifact(s) from "
                f"{src_pkg.package} placed in repo/ subdirs"
            )
        return _moved_paths

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

        # OBS-01: the signed build.json record is the sole source of
        # truth for "has this been built".  An interrupted record
        # (non-terminal phase) or missing record is treated as "not
        # built" — the audit's classify_build_record returns 'missing'
        # / 'interrupted' / 'fail' for everything except 'ok' and
        # 'tunneled'.
        _record = utils.read_build_record(self.buildlog_path, src_pkg.package)
        if _record is None:
            return False
        if utils.classify_build_record(_record) not in ('ok', 'tunneled'):
            return False

        # Component (from origin mirror) so a non-main package's binaries are
        # located in their component dir — e.g. a TUNNELED firmware package
        # lives in repo/dists/<codename>/non-free-firmware/, not main; without
        # this the presence check would always miss and re-tunnel every run.
        _comp = getattr(getattr(src_pkg, '_mirror', None), 'component', '') or 'main'
        for _file in expected_files:
            # Only main-classified binaries gate rebuild; missing
            # -dev/-doc/-dbgsym/-tests are tolerated.  CONF-01 Stage D:
            # deb_dest_for_filename returns the new nested location
            # (main → dists/<codename>/main/binary-<arch>/ for .deb,
            # → debian-installer/binary-<arch>/ for .udeb).
            _sub = utils.classify_repo_subdir(_file)
            if _sub != 'main':
                continue
            # UPD-01: accept the predicted pristine name OR a +asg<R>u<N>
            # stamped variant of it (find_matching_artifact).  The old
            # exact-only os.path.isfile match was the CONF-13 rebuild-loop
            # cause: a stamped/ABI-variant on-disk file never matched, so the
            # source was rebuilt every run.
            _dst_dir = self.config.deb_dest_for_filename(_file, _comp)
            _filename = utils.find_matching_artifact(_dst_dir, _file)
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
            `self.cache.package_hashtable` / `udeb_hashtable` (legacy
            path).  Skipped when both are None (no scope to verify
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
        for _ext in ('.deb', '.udeb'):
            if _base.endswith(_ext):
                _base = _base[:-len(_ext)]
                break
        _parts = _base.split('_')
        if len(_parts) != 3:
            return (False, f'bad-filename-shape:{_base}')
        _exp_pkg, _exp_ver, _exp_arch = _parts

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

        # Legacy cache-based path — preserved for callers that don't
        # have a RepoState handy (test fixtures + back-compat).
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
        for .deb deps, cache.udeb_hashtable for .udeb deps.  None →
        fall back to package_hashtable (backwards-compat for the few
        callers that don't specify).
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
