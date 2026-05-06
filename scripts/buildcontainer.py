
import hashlib
import os
from typing import List, Dict
from utils import BuildConfig
from package import Source

import docker
import tui

class BuildContainer:

    def __init__(self, config: BuildConfig, docker_server=None) -> bool:

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
        self.mirrors = config.mirrors

        self.client = None

        if docker_server is not None:
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
                tui.console.error(f"Athena Linux Docker: Error {e}")
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
            tui.console.error(f"Athena Linux Docker: Error {e}")
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
                            if 'stream' in chunk:
                                for line in chunk['stream'].splitlines():
                                    fh.write(line + '\n')

                except (FileNotFoundError, PermissionError) as e:
                    tui.console.error(f"Error writing docker build log: {e}")
                    tui.console.print(f"Error writing docker build log: {e}")
                    raise RuntimeError(f"Cannot write Docker build log: {e}")
                
            except docker.errors.APIError as e:
                tui.console.error(f"Athena Linux Docker: Error {e}")
                tui.console.print(f"Athena Linux Docker: Error {e}")
                raise RuntimeError(f"Docker image build failed: {e}")

        self.image = image


    @staticmethod
    def _hash_dockerfile(config_dir: str) -> str:
        dockerfile = os.path.join(config_dir, 'Dockerfile')
        try:
            with open(dockerfile, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ''

    def build(self, src_pkg: Source) -> bool:

        _plain_deps = []
        _or_cmds = []
        _apt_retry = '-o Acquire::Retries=5 '
        _active_profiles = self.build_profiles

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
            tui.console.error(f"DSC not found for {src_pkg.package}")
            return False

        # TODO: support per-package extra DEB_BUILD_OPTIONS (e.g. noudeb, nostrip) via build.conf
        # _deb_build_opts = 'nodoc'
        # if src_pkg.skip_test:
        #     _deb_build_opts += ' nocheck'
        _deb_build_opts = ' '.join(sorted(_active_profiles))
        deb_build_env = f'DEB_BUILD_OPTIONS="{_deb_build_opts}" DEB_BUILD_PROFILES="{_deb_build_opts}" '

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

            with open(os.path.join(self.buildlog_path, _filename_prefix), 'w') as fh:
                for line in container.logs(stream=True):
                    fh.write(line.decode("utf-8"))

            _exit_code = container.wait()['StatusCode']
            container.remove()

            _build_result = (_exit_code == 0)
            with open(os.path.join(self.buildlog_path, _filename_prefix + '.result'), 'w') as fh:
                fh.write('PASS\n' if _build_result else 'FAIL\n')

            return _build_result

        except docker.errors.APIError as e:
            tui.console.error(f"Athena Linux Docker: Error {e}")
            tui.console.print(f"Athena Linux Docker: Error {e}")
            return False

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
    def is_ar_file(filename: str):

        _filelist: list = []
        try:
            with open(filename, 'rb') as f:
                header = f.read(8)
                if header != b'!<arch>\n':
                    return False

                while True:
                    entry_header = f.read(60)
                    if not entry_header:
                        break

                    name = entry_header[:16].decode().rstrip()
                    if not name:
                        break
                    _filelist.append(name)

                    size = int(entry_header[48:58].decode().rstrip(), 10)
                    content = f.read(size)
                    if len(content) != size:
                        return False

                    if f.tell() % 2 != 0:
                        f.seek(1, os.SEEK_CUR)

        except Exception as e:
            tui.console.error(f"Error reading file: {str(e)}")
            return False

        _compressions = ['.xz', '.gz', '.bz2', '.lmza', '.zst']
        _required_files = ['control.tar', 'data.tar']

        _parsed_filelist = {}
        for _file in _filelist:
            _filename, _ext = os.path.splitext(_file)
            _parsed_filelist[_filename] = _ext

        if 'debian-binary' not in _parsed_filelist:
            return False

        for _file in _required_files:
            if _file not in _parsed_filelist:
                return False
            if _parsed_filelist[_file] not in _compressions:
                return False

        return True
