
import hashlib
import logging
import os
from typing import List, Dict
from utils import BuildConfig
from package import Source

import docker
import docker.errors  # noqa: F401 — explicit import so `docker.errors.X` resolves under mypy
import tui

logger = logging.getLogger('athena')


class BuildContainer:

    def __init__(self, config: BuildConfig, docker_server=None):

        self.build_path = config.dir_repo
        self.src_path = config.dir_source
        self.log_path = config.dir_log
        self.repo_path = config.dir_repo
        self.arch = config.arch

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
        self.mirrors = [m.with_snapshot(self.snapshot_ts) for m in config.mirrors]

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

        for _grp in src_pkg.build_depends(self.arch, _active_profiles):
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
        deb_build_env = (
            f'DEB_BUILD_OPTIONS="{_deb_build_opts}" '
            f'DEB_BUILD_PROFILES="{_deb_build_profiles}" '
        )

        # TODO: read patch files from disk here instead of relying on src_pkg.patch_list
        # (patch_list is set during parse_dependency; patches added after that run are missed)
        patch_cmd = (
            f'for PATCH in {" ".join(src_pkg.patch_list)}; do patch -p1 < /patch/"$PATCH"; done; '
            if src_pkg.patch_list else ''
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

        # Pin the container's apt to the exact mirrors our cache was built
        # from.  Without this the base image's stock sources.list (live
        # mirror) is used, and a security update landing between our cache
        # snapshot and the build run will produce dep version skew between
        # the .deb we build and what the cache thinks (caught by
        # _verify_dep_resolution).  Step 2 will swap these URLs for snapshot
        # URLs to make the alignment durable.
        _apt_sources = ''.join(
            f'deb {_m.url} {_m.suite} {_m.component}\n' for _m in self.mirrors
        )
        _write_sources = (
            f"sudo tee /etc/apt/sources.list >/dev/null <<'EOF'\n"
            f"{_apt_sources}EOF\n"
        )

        cmd_str = f'set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'{_write_sources}' \
                  f'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq; ' \
                  f'{_dep_install}' \
                  f'cd /home/athena; cp /source/{_filename_prefix}* .; ' \
                  f'dpkg-source -x {_dsc_file} {_filename_prefix}; ' \
                  f'cd {_filename_prefix}; ' \
                  f'{patches_applied_cmd}' \
                  f'{patch_cmd}' \
                  f'{deb_build_env} dpkg-checkbuilddeps; {deb_build_env} dpkg-buildpackage -a {self.arch} -us -uc -nc; cd ..;' \
                  f'cp *.deb /repo/ 2>/dev/null || true; cp *.udeb /repo/ 2>/dev/null || true ;'

        # `container` is initialised to None so the finally block can tell
        # whether containers.run() actually produced a container (failure
        # before that point leaves nothing to clean up).  All exception
        # paths below — APIError, OSError on log writes, KeyboardInterrupt
        # mid-build — flow through the finally so a leftover container can
        # never accumulate in `docker ps -a` between runs.
        container = None
        try:
            src_patch_path = os.path.join(self.patch_path, src_pkg.package, str(src_pkg.version))
            if not os.path.exists(src_patch_path):
                src_patch_path = self.patch_empty

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

            if not _build_result:
                logger.error(
                    f"Build {src_pkg.package} failed in container "
                    f"{container.short_id} (exit {_exit_code})"
                )

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

    def check_build(self, src_pkg: Source) -> bool:

        if not src_pkg.pkgs:
            return False

        result_file = os.path.join(self.buildlog_path, src_pkg.package + '.result')
        try:
            with open(result_file, 'r') as fh:
                if fh.readline().strip() not in ('PASS', 'TUNNELED'):
                    return False
        except OSError:
            return False

        for _file in src_pkg.pkgs:
            _filename = os.path.join(self.repo_path, _file)
            if not os.path.isfile(_filename):
                return False
            if not self.is_ar_file(_filename):
                return False

        return True

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
