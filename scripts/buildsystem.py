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
import docker
from docker import errors
from utils import DirectoryListing

Print = print


class BuildContainer:

    def __init__(self, dir_list: DirectoryListing):
        import os
        self.build_path = dir_list.dir_repo
        self.src_path = dir_list.dir_download
        self.log_path = dir_list.dir_log
        self.buildlog_path = os.path.join(dir_list.dir_log, 'build')
        self.conf_path = dir_list.dir_config

        try:
            self.client = docker.from_env()
            # Confirm function
            self.client.ping()

            # Build an image from a Dockerfile
            try:
                image = self.client.images.get("athenalinux:build")
                Print(f"Using Athena Linux Image - {image.tags}")
            except docker.errors.ImageNotFound as e:
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

    def container_execute(self, command: str):

        try:
            cmd_str = 'mkdir /build; cd /source; ls -al; apt -y install debhelper-compat zlib1g-dev mawk;' \
                      ' dpkg-source -x libpng1.6_1.6.37-3.dsc /build/libpng; cd /build/libpng; dpkg-checkbuilddeps;' \
                      ' dpkg-buildpackage -a amd64 -us -uc -J; cd /build; cp *.deb /source/'
            container = self.client.containers.run("athenalinux:build", command=f"/bin/bash -c '{cmd_str}'",
                                                   detach=True, auto_remove=False,
                                                   volumes={self.src_path: {'bind': '/source', 'mode': 'rw'}})
            for line in container.logs(stream=True):
                Print(line.decode("utf-8"), end="")

            # for line in container.logs(stream=True):
            #    Print(line.decode("utf-8"))


            # Save the file to the host file system
            # with open("output.txt", "wb") as f:
            #    f.write(container.get_archive("/output.txt")[0])
            container.wait()
            container.stop()
            container.remove()
        except docker.errors.APIError as e:
            Print(f"Athena Linux Docker: Error{e}")
            exit(1)
