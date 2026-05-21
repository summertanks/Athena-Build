
import hashlib
import logging
import os
from typing import Optional, Tuple
import utils
from utils import BuildConfig, version_no_epoch
from package import Source

import docker
import docker.errors  # noqa: F401 — explicit import so `docker.errors.X` resolves under mypy
import tui

logger = logging.getLogger('athena')


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
                tui.console.print("Athena Linux Docker: Couldn't connect to external server, reverting to local")

        if self.client is None:
            try:
                self.client = docker.from_env()
                self.client.ping()
            except docker.errors.APIError as e:
                logger.error(f"Athena Linux Docker: Error {e}")
                tui.console.print(f"Athena Linux Docker: Error {e}")
                raise RuntimeError(f"Cannot connect to local Docker daemon: {e}")

        self._image_tag = f"athenalinux:build-{config.container_release}"
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
                tui.console.print(f"Using Athena Linux Image - {image.tags}")

        except docker.errors.ImageNotFound:
            tui.console.print(f"Image not found — building {_image_tag}")
            _needs_build = True

        except docker.errors.APIError as e:
            logger.error(f"Athena Linux Docker: Error {e}")
            tui.console.print(f"Athena Linux Docker: Error {e}")
            tui.Exit(1)

        if _needs_build:
            try:
                image, build_logs = self.client.images.build(
                    path=config.dir_config, tag=_image_tag,
                    buildargs={'RELEASE': config.container_release},
                    labels={'athena.dockerfile.sha256': dockerfile_hash},
                    nocache=False, rm=True, )

                tui.console.print(f"Athena Linux Image Built - {image.tags}")
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
                    raise RuntimeError(f"Cannot write Docker build log: {e}")
                
            except docker.errors.APIError as e:
                logger.error(f"Athena Linux Docker: Error {e}")
                tui.console.print(f"Athena Linux Docker: Error {e}")
                raise RuntimeError(f"Docker image build failed: {e}")

        self.image = image


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

    @staticmethod
    def _hash_dockerfile(config_dir: str) -> str:
        dockerfile = os.path.join(config_dir, 'Dockerfile')
        try:
            with open(dockerfile, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ''

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

        _plain_deps = []
        _or_cmds = []
        _apt_retry = '-o Acquire::Retries=5 '
        _active_profiles = (frozenset(profiles_override)
                            if profiles_override is not None
                            else self.build_profiles)
        _active_options = (frozenset(options_override)
                           if options_override is not None
                           else self.build_options)

        for _grp in src_pkg.build_depends(self.arch, _active_profiles, cache=self.cache):
            if not _grp:
                continue
            if len(_grp) == 1:
                _plain_deps.append(_grp[0][0])
            else:
                _chain = f' || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {_apt_retry}'.join(alt[0] for alt in _grp)
                _or_cmds.append(f'{{ sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {_apt_retry}{_chain}; }}')
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
        _dep_install = (f'sudo DEBIAN_FRONTEND=noninteractive apt -y {_apt_retry}install {" ".join(_plain_deps)}; ' if _plain_deps else '') + \
                       ('; '.join(_or_cmds) + '; ' if _or_cmds else '')

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
        # from.  Without this the base image's stock sources.list (live
        # mirror) is used, and a security update landing between our cache
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
        _apt_sources = ''.join(
            f'deb [check-valid-until=no] {_m.url} {_m.suite} {_m.component}\n'
            for _m in self.mirrors
        )
        _write_sources = (
            f"sudo tee /etc/apt/sources.list >/dev/null <<'EOF'\n"
            f"{_apt_sources}EOF\n"
        )

        # `-b` (--build=binary) skips the source rebuild step.  No
        # version-bump is performed; the produced .debs ship at the
        # pristine upstream source version.  Post-build, the BuildContainer
        # runs utils.strip_nmu_from_deb on every produced artifact to
        # normalise the Version field + all dep-constraint version
        # references — stripping +bN, +debNuN, ~bpoN+N, +rpiN, etc.
        # so internal cross-refs resolve cleanly inside our repo.
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
                volumes={
                    self.src_path:    {'bind': '/source', 'mode': 'rw'},
                    self.repo_path:   {'bind': '/repo',   'mode': 'rw'},
                    src_patch_path:   {'bind': '/patch',  'mode': 'rw'},
                },
            )
            logger.info(
                f"Build container {container.short_id} started for {src_pkg.package}"
            )

            with open(os.path.join(self.buildlog_path, _filename_prefix), 'w') as fh:
                for line in container.logs(stream=True):
                    fh.write(line.decode("utf-8"))

            _exit_code = container.wait()['StatusCode']

            _build_result = (_exit_code == 0)
            with open(os.path.join(self.buildlog_path, _filename_prefix + '.result'), 'w') as fh:
                fh.write('PASS\n' if _build_result else 'FAIL\n')

            # Record the patch-set content hash alongside .result so
            # `_refresh_patches` (next run) can distinguish header-only
            # edits (same hash → no rebuild) from real diff changes
            # (different hash → invalidate).  Written on every build
            # outcome so deletion-detection works even after a FAIL.
            _patch_dir_for_hash = os.path.join(
                self.patch_path, src_pkg.package,
                version_no_epoch(src_pkg.version),
            )
            _hash_value = utils.patch_set_hash(
                _patch_dir_for_hash, _live_patch_list,
            )
            _hash_file = os.path.join(
                self.buildlog_path, _filename_prefix + '.patchhash',
            )
            try:
                with open(_hash_file, 'w') as fh:
                    fh.write(_hash_value + '\n')
            except OSError as e:
                logger.warning(f"cannot write {_hash_file}: {e}")

            if not _build_result:
                logger.error(
                    f"Build {src_pkg.package} failed in container "
                    f"{container.short_id} (exit {_exit_code})"
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
                self._segregate_built_artifacts(src_pkg)
                # 2. Strip NMU/binNMU/backport suffix from the emitted
                #    .debs (now in their subdirs).  Per-file decision;
                #    only re-packs when residue exists.
                self._strip_nmu_from_built_artifacts(src_pkg)

            return _build_result

        except docker.errors.APIError as e:
            _cid = container.short_id if container is not None else '<not-started>'
            logger.error(
                f"Athena Linux Docker error for {src_pkg.package} "
                f"(container {_cid}): {e}"
            )
            tui.console.print(f"Athena Linux Docker: Error {e}")
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

    def _strip_nmu_from_built_artifacts(self, src_pkg) -> None:
        """Walk repo/ for .deb/.udeb files whose Package: field belongs
        to this source build and run utils.strip_nmu_from_deb on each.

        Identification: read each file's Source: field via DebFile; match
        against src_pkg.package.  Falls back to filename-prefix match
        when Source: is absent (single-binary sources omit Source when
        Source == Package).

        Failures are logged but don't propagate — strip is best-effort
        normalisation; a stripped failure leaves the .deb at upstream-
        layered version, surfaced later by `package audit_nmu`.
        """
        from debian.debfile import DebFile
        _src_name = src_pkg.package
        # Post-segregate, artifacts live in subdirs.  Walk all of them.
        _files: 'list[str]' = []
        for _sub in ('main', 'doc', 'dbgsym', 'tests'):
            _sub_path = os.path.join(self.repo_path, _sub)
            try:
                for _f in os.listdir(_sub_path):
                    if _f.endswith('.deb') or _f.endswith('.udeb'):
                        _files.append(os.path.join(_sub, _f))
            except OSError:
                continue
        _n_rewritten = 0
        for _f in _files:
            _path = os.path.join(self.repo_path, _f)
            # Quick filter by control's Source: field.  Some binaries
            # in repo/ come from other source pkgs and shouldn't be
            # touched here — they'll be stripped when their own source
            # builds (or by the one-time `package strip` backfill).
            try:
                with DebFile(_path) as _deb:
                    _src_field = (_deb.control.debcontrol().get('Source') or '').strip()
                    _pkg_field = (_deb.control.debcontrol().get('Package') or '').strip()
            except Exception:
                continue
            _origin = _src_field.split(' ', 1)[0].strip() if _src_field else _pkg_field
            if _origin != _src_name:
                continue
            try:
                _r = utils.strip_nmu_from_deb(_path)
                if _r['status'] == 'rewritten':
                    _n_rewritten += 1
                    if _r['new_path'] != _path:
                        logger.info(
                            f"strip_nmu: {_f} → "
                            f"{os.path.basename(_r['new_path'])}"
                        )
            except Exception as e:
                logger.warning(f"strip_nmu: {_f} failed: {e}")
        if _n_rewritten:
            logger.info(
                f"strip_nmu: normalised {_n_rewritten} artifact(s) "
                f"from source {_src_name}"
            )

    def _segregate_built_artifacts(self, src_pkg) -> None:
        """After dpkg-buildpackage's `cp *.deb /repo/`, the binaries
        land at repo/ ROOT (not in any subdir).  This pass classifies
        each by name and moves it to the right subdir per
        utils.classify_repo_subdir:

          main    installable binaries + udebs
          dev     -dev side artifacts
          doc     -doc side artifacts
          dbgsym  -dbgsym side artifacts
          tests   -test / -tests side artifacts

        Operates ONLY on the just-emitted files (those at repo/ root) —
        existing subdir contents are left alone.  Skips when no files
        at root (steady state).
        """
        try:
            _files_at_root = [
                _f for _f in os.listdir(self.repo_path)
                if (_f.endswith('.deb') or _f.endswith('.udeb'))
                and os.path.isfile(os.path.join(self.repo_path, _f))
            ]
        except OSError as e:
            logger.warning(
                f"segregate: cannot list {self.repo_path}: {e}"
            )
            return
        if not _files_at_root:
            return
        _moved = 0
        for _f in _files_at_root:
            _sub = utils.classify_repo_subdir(_f)
            _src = os.path.join(self.repo_path, _f)
            _dst_dir = os.path.join(self.repo_path, _sub)
            os.makedirs(_dst_dir, exist_ok=True)
            _dst = os.path.join(_dst_dir, _f)
            try:
                if os.path.exists(_dst):
                    # Collision: a prior build produced the same
                    # filename.  Newer rebuild wins.
                    os.remove(_dst)
                os.rename(_src, _dst)
                _moved += 1
            except OSError as e:
                logger.warning(
                    f"segregate: failed to move {_f} → {_sub}/: {e}"
                )
        if _moved:
            logger.info(
                f"segregate: {_moved} artifact(s) from {src_pkg.package} "
                f"placed in repo/ subdirs"
            )

    def check_build(self, src_pkg: Source,
                    expected_files: 'list[str]') -> bool:
        """Decide whether a previously-built source can skip rebuild.

        Shallow gate — kept fast because this runs on every source
        build call across the full dep tree:

          1. expected_files is non-empty
          2. log/build/<src>.result reads PASS or TUNNELED
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

        result_file = os.path.join(self.buildlog_path, src_pkg.package + '.result')
        try:
            with open(result_file, 'r') as fh:
                if fh.readline().strip() not in ('PASS', 'TUNNELED'):
                    return False
        except OSError:
            return False

        for _file in expected_files:
            # Only main-classified binaries gate rebuild; missing
            # -dev/-doc/-dbgsym/-tests are tolerated.
            _sub = utils.classify_repo_subdir(_file)
            if _sub != 'main':
                continue
            _filename = os.path.join(self.repo_path, 'main', _file)
            if not os.path.isfile(_filename):
                return False
            if not self.is_ar_file(_filename):
                return False

        return True

    def verify_pkg_artifact(self, deb_path: str,
                            expected_filename: str) -> 'Tuple[bool, str]':
        """Deep-verify a single .deb / .udeb against its predicted
        filename + the current cache state.

        Checks (in order, short-circuits on first failure):

          1. File exists at deb_path
          2. Valid ar archive (is_ar_file)
          3. Internal `Package:` field == filename's pkg part
          4. Internal `Version:` field == filename's version part
          5. Internal `Architecture:` field matches filename's arch part
             (also accepts `all` to match any arch suffix — for arch-
             independent .debs whose filename uses the build arch)
          6. Every Depends + Pre-Depends OR-group resolvable via
             self.cache.package_hashtable (at least one alternative in
             each group has a version satisfying its constraint).
             Skipped when self.cache is None (no cache to verify
             against; trust).

        Returns (True, 'ok') on full pass; (False, diagnostic) on
        first failure.  Diagnostic format: short-token:detail for
        machine-friendly logging.

        Used by check_build (skip-rebuild decision) and cmd_source_
        repair (write-PASS decision) so both share the same notion
        of "this artifact is good enough to skip building."
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

        # Depends resolution against cache.  Skip if no cache (some
        # call sites construct BuildContainer without one — the
        # filename + control check still has value).
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

        from debian.deb822 import PkgRelation
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
