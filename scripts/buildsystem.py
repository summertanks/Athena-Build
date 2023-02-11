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

from scripts.utils import DirectoryListing

Print = print


def container_execute(dir_list: DirectoryListing):
    import docker
    from docker import errors
    try:
        client = docker.from_env()

        # Build an image from a Dockerfile
        image, build_logs = client.images.build(path=dir_list.dir_build, tag='master_image', nocache=True)
        for chunk in build_logs:
            if 'stream' in chunk:
                for line in chunk['stream'].splitlines():
                    Print(line)

        client.images.get("master_image").save("master_image.tar")
        container = client.containers.run("master_image", command="python custom_script.py", detach=True,
                                          volumes={'$PWD': {'bind': '/build', 'mode': 'rw'}})
        with open("output.txt", "w") as f:
            for line in container.logs(stream=True):
                f.write(line.decode("utf-8"))

        # Copy the file from the container to the host file system
        container.copy("/output.txt")

        # Save the file to the host file system
        with open("output.txt", "wb") as f:
            f.write(container.get_archive("/output.txt")[0])

        container.stop()
        container.remove()
    except docker.errors.APIError as e:
        Print(f"Athena Linux Docker: Error{e}")

