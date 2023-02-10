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

def container_execute(self):
    import docker

    client = docker.from_env()

    # Build an image from a Dockerfile
    response = client.images.build(path='.', tag='master_image')
    for line in response:
        print(line)

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
