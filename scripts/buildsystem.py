# Installing docker
# apt-get remove docker docker-engine docker.io containerd runc
# apt-get install ca-certificates curl gnupg lsb-release
# mkdir -m 0755 -p /etc/apt/keyrings
# curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
# echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg]
# https://download.docker.com/linux/debian $(lsb_release -cs) stable" |
# sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
# apt-get update
# apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
# usermod -aG docker $USER
import os
import docker
from docker import errors

from source import Source
from utils import DirectoryListing

Print = print


class BuildContainer:

    def __init__(self, dir_list: DirectoryListing, docker_server=None):
        self.build_path = dir_list.dir_repo
        self.src_path = dir_list.dir_source
        self.log_path = dir_list.dir_log
        self.repo_path = dir_list.dir_repo
        self.buildlog_path = os.path.join(dir_list.dir_log, 'build')
        self.conf_path = dir_list.dir_config

        if docker_server is not None:
            try:
                self.client = docker.DockerClient(base_url=docker_server)
                self.client.ping()
            except docker.errors.APIError:
                Print(f"Athena Linux Docker: Couldn't connect to external server, reverting to local")

        try:
            # self.client = docker.from_env()
            # Confirm function
            self.client.ping()

            # Build an image from a Dockerfile
            try:
                image = self.client.images.get("athenalinux:build")
                Print(f"Using Athena Linux Image - {image.tags}")
            except docker.errors.ImageNotFound:
                Print("Image not found, Building AthenaLinux Image...")
                image, build_logs = self.client.images.build(path=dir_list.dir_config, tag='athenalinux:build',
                                                             nocache=True, rm=True)
                Print(f"Athena Linux Image Built - {image.tags}")
                try:
                    with open(os.path.join(self.log_path, 'docker_build.log'), 'w') as fh:
                        for chunk in build_logs:
                            if 'stream' in chunk:
                                for line in chunk['stream'].splitlines():
                                    fh.write(line + '\n')
                except (FileNotFoundError, PermissionError) as e:
                    Print(f"Error: {e}")
                    exit(1)
            self.image = image

        except docker.errors.APIError as e:
            Print(f"Athena Linux Docker: Error{e}")
            exit(1)

    def build(self, src_pkg: Source) -> bool:
        # Check if build is already there
        if self.check_build(src_pkg):
            return True

        # list of dependencies
        _dep_str = src_pkg.build_depends
        # source files are usually in form of <packagename_version.extension>
        _filename_prefix = src_pkg.package
        # dsc file
        _dsc_file = ''
        try:
            _dsc_file = [file for file in src_pkg.files if file.endswith('.dsc')][0]
        except IndexError:
            Print(f"DSC not found for {src_pkg.package}")
            return False

        assert _dsc_file != '', f"DSC not found for {src_pkg.package}"

        cmd_str = f'set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'apt -y install {_dep_str}; ' \
                  f'su -c ' \
                  f'" set -e; set -o errexit; set -o nounset; set -o pipefail; ' \
                  f'whoami; pwd; cd /home/athena; pwd; ' \
                  f'cp /source/{_filename_prefix}* .; ' \
                  f'dpkg-source -x {_dsc_file} {_filename_prefix}; cd {_filename_prefix}; ' \
                  f'dpkg-checkbuilddeps; dpkg-buildpackage -a amd64 -us -uc; cd ..;' \
                  f'cp *.deb /repo/ 2>/dev/null || true; cp *.udeb /repo/ 2>/dev/null || true ;' \
                  f'" athena'

        try:
            container = self.client.containers.run("athenalinux:build", command=f"/bin/bash -c '{cmd_str}'",
                                                   detach=True, auto_remove=False,
                                                   volumes={self.src_path: {'bind': '/source', 'mode': 'rw'},
                                                            self.repo_path: {'bind': '/repo', 'mode': 'rw'}})
            with open(os.path.join(self.buildlog_path, _filename_prefix), 'w') as fh:
                for line in container.logs(stream=True):
                    # Print(line.decode("utf-8"), end="")
                    fh.write(line.decode("utf-8"))

            _exit_code = container.wait()['StatusCode']
            container.stop()
            container.remove()
            return _exit_code == 0
        except docker.errors.APIError as e:
            Print(f"Athena Linux Docker: Error{e}")
            exit(1)

    def check_build(self, src_pkg: Source) -> bool:

        for _file in src_pkg.pkgs:
            _filename = os.path.join(self.repo_path, _file)
            # Check is file exists first
            if not os.path.isfile(_filename):
                return False

            if not self.is_ar_file(_filename):
                return False

        return True

    @staticmethod
    def is_ar_file(filename: str):
        _filelist: [] = []
        with open(filename, 'rb') as f:
            try:
                # Read the file header
                header = f.read(8)
                if header != b'!<arch>\n':
                    return False

                # Loop through the file entries
                while True:
                    # Read the entry header
                    entry_header = f.read(60)
                    if not entry_header:
                        # End of file
                        break

                    # Parse the entry header
                    name = entry_header[:16].decode().rstrip()
                    if not name:
                        # End of archive marker
                        break
                    # Saving filenames
                    _filelist.append(name)

                    # Read the entry content
                    size = int(entry_header[48:58].decode().rstrip())
                    content = f.read(size)
                    if len(content) != size:
                        # Entry content is incomplete
                        return False

                    # Continue to the next entry
            except Exception as e:
                # Exception occurred while reading the file
                print(f"Error reading file: {str(e)}")
                return False

        # If we made it here, the file is a valid ar file
        # Checking if its a valid deb file
        _required_files = ['debian-binary', 'control.tar.xz', 'data.tar.xz']
        for _file in _required_files:
            if _file not in _filelist:
                return False

        return True
