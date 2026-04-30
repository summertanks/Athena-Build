
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

import docker

from utils import BuildConfig
from package import Source

import tui


_NETWORK_NAME   = 'athena-build'
_APT_CACHE_NAME = 'athena-apt-cache'
_APT_CACHE_IMG  = 'sameersbn/apt-cacher-ng'
_APT_CACHE_VOL  = 'athena-apt-cache-data'


class BuildContainer:

    def __init__(self, config: BuildConfig, docker_server=None):

        self.build_path = config.dir_repo
        self.src_path = config.dir_source
        self.log_path = config.dir_log
        self.repo_path = config.dir_repo
        self.arch = config.arch
        self._max_parallel = config.max_parallel_builds

        self.buildlog_path = os.path.join(config.dir_log, 'build')
        self.conf_path = config.dir_config

        self.patch_path = config.dir_patch_source
        self.patch_empty = config.dir_patch_empty

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
                tui.Exit(1)

        dockerfile_hash = self._hash_dockerfile(config.dir_config)

        _needs_build = False
        try:
            image = self.client.images.get("athenalinux:build")
            stored_hash = image.labels.get('athena.dockerfile.sha256', '')
            if stored_hash != dockerfile_hash:
                tui.console.print(f"Dockerfile changed — rebuilding athenalinux:build")
                _needs_build = True
            else:
                tui.console.print(f"Using Athena Linux Image - {image.tags}")
        except docker.errors.ImageNotFound:
            tui.console.print("Image not found — building athenalinux:build")
            _needs_build = True
        except docker.errors.APIError as e:
            tui.console.error(f"Athena Linux Docker: Error {e}")
            tui.console.print(f"Athena Linux Docker: Error {e}")
            tui.Exit(1)

        if _needs_build:
            try:
                image, build_logs = self.client.images.build(
                    path=config.dir_config,
                    tag='athenalinux:build',
                    labels={'athena.dockerfile.sha256': dockerfile_hash},
                    nocache=False,
                    rm=True,
                )
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
                    tui.Exit(1)
            except docker.errors.APIError as e:
                tui.console.error(f"Athena Linux Docker: Error {e}")
                tui.console.print(f"Athena Linux Docker: Error {e}")
                tui.Exit(1)

        self.image = image

        self._ensure_network()
        self._apt_cache_active = self._ensure_apt_cache()

    @staticmethod
    def _hash_dockerfile(config_dir: str) -> str:
        dockerfile = os.path.join(config_dir, 'Dockerfile')
        try:
            with open(dockerfile, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ''

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(_NETWORK_NAME)
        except docker.errors.NotFound:
            self.client.networks.create(_NETWORK_NAME, driver='bridge')
            tui.console.print(f"Created Docker network {_NETWORK_NAME}")
        except docker.errors.APIError as e:
            tui.console.error(f"Could not ensure Docker network {_NETWORK_NAME}: {e}")
            tui.console.print(f"Could not ensure Docker network {_NETWORK_NAME}: {e}")
            tui.Exit(1)

    def _ensure_apt_cache(self) -> bool:
        try:
            container = self.client.containers.get(_APT_CACHE_NAME)
            container.reload()
            network_names = container.attrs.get('NetworkSettings', {}).get('Networks', {})
            if _NETWORK_NAME not in network_names:
                self.client.networks.get(_NETWORK_NAME).connect(container)
            if container.status != 'running':
                container.start()
                tui.console.print(f"Started existing apt-cacher-ng container")
            else:
                tui.console.print(f"apt-cacher-ng already running")
        except docker.errors.NotFound:
            self.client.containers.run(
                _APT_CACHE_IMG,
                name=_APT_CACHE_NAME,
                detach=True,
                restart_policy={'Name': 'unless-stopped'},
                network=_NETWORK_NAME,
                volumes={_APT_CACHE_VOL: {'bind': '/var/cache/apt-cacher-ng', 'mode': 'rw'}},
            )
            tui.console.print(f"Started new apt-cacher-ng container ({_APT_CACHE_NAME})")
        except docker.errors.APIError as e:
            tui.console.warning(f"apt-cacher-ng unavailable, builds will run without cache: {e}")
            return False
        return True

    def build(self, src_pkg: Source) -> bool:
        skip_list = []

        if src_pkg.package in skip_list:
            return False

        if self.check_build(src_pkg):
            return True

        _dep_str = ' '.join(
            grp[0]['name'] for grp in src_pkg.build_depends(self.arch) if grp
        )
        _filename_prefix = src_pkg.package
        _dsc_file = ''
        try:
            _dsc_file = [file for file in src_pkg.files if file.endswith('.dsc')][0]
        except IndexError:
            tui.console.error(f"DSC not found for {src_pkg.package}")
            return False

        skip_build_test = ''
        if src_pkg.skip_test:
            skip_build_test = 'DEB_BUILD_OPTIONS="nocheck" '

        proxy_opt = (
            f'-o Acquire::http::Proxy=http://{_APT_CACHE_NAME}:3142 '
            f'-o Acquire::http::Proxy::security.debian.org=DIRECT '
            f'-o Acquire::http::Proxy::deb.debian.org/debian-security=DIRECT '
        ) if self._apt_cache_active else ''

        # TODO: Apply Build Patches
        patch_list = ' '.join(src_pkg.patch_list)
        cmd_str = f'set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'sudo apt -y {proxy_opt}install {_dep_str}; ' \
                  f'cd /home/athena; cp /source/{_filename_prefix}* .; ' \
                  f'dpkg-source -x {_dsc_file} {_filename_prefix}; ' \
                  f'cd {_filename_prefix}; ' \
                  f'for PATCH in {patch_list}; do patch -p1 < /patch/"$PATCH"; done; ' \
                  f'dpkg-checkbuilddeps; {skip_build_test} dpkg-buildpackage -a amd64 -us -uc; cd ..;' \
                  f'cp *.deb /repo/ 2>/dev/null || true; cp *.udeb /repo/ 2>/dev/null || true ;'

        try:
            src_patch_path = os.path.join(self.patch_path, src_pkg.package, str(src_pkg.version))
            if not os.path.exists(src_patch_path):
                src_patch_path = self.patch_empty

            container = self.client.containers.run(
                "athenalinux:build",
                command=f"/bin/bash -c '{cmd_str}'",
                detach=True,
                auto_remove=False,
                network=_NETWORK_NAME,
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
            return _exit_code == 0
        except docker.errors.APIError as e:
            tui.console.error(f"Athena Linux Docker: Error {e}")
            tui.console.print(f"Athena Linux Docker: Error {e}")
            tui.Exit(1)

    def build_all(self, packages: List[Source], on_done=None) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self._max_parallel) as executor:
            futures = {executor.submit(self.build, pkg): pkg for pkg in packages}
            for future in as_completed(futures):
                pkg = futures[future]
                try:
                    results[pkg.package] = future.result()
                except Exception as e:
                    tui.console.error(f"Build failed for {pkg.package}: {e}")
                    results[pkg.package] = False
                if on_done is not None:
                    on_done(pkg.package, results[pkg.package])
        return results

    def check_build(self, src_pkg: Source) -> bool:

        if not src_pkg.pkgs:
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

        _filelist: [] = []
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
