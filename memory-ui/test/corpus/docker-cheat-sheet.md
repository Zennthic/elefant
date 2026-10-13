---
tags:
  - docker
---

# Docker Cheat Sheet

## A collection commonly used commands

Note that you may need to `sudo` these, if your username is not a member of the `docker` group.

(hint: `sudo usermod -aG docker ${USER}` then log out/back in 😉 )

|  | **Description** |
| --- | ----------- |
| docker rm -f containerID | If a container gets created and is stuck in a reboot loop, this will forcibly kill and remove it. |
| docker ps | list all running containers |
| docker ps -a | list all containers on the host, running or not |
| docker cp containerName:/path/to/filename.txt . | copy a file from a container (EVEN STOPPED!) to local file system. don't forget trailing dot. |
| sysctl -w vm.max\_map\_count=262144 | If you try to start a java-based container and get something about "max virtual memory areas vm.max\_map\_count [65530] likely too low", this will increase that to 262144 (or whatever you specify) |

## Keep container logs under control

```
  services:
    serviceName: 
      logging:
        options:
          max-size: "10m"
          max-file: "3"
```

## Move a named Docker volume to another host

This is very handy for moving a volume; best that the source container is stopped first, for file consistency. **Note! you do not have to create the volume on the destination host first, this creates it for you!** Since it `tar`s the data, all permissions are properly captured as well

```
docker run --rm -v <SOURCE_DATA_VOLUME_NAME>:/from alpine ash -c "cd /from ; tar -cf - . " | ssh <TARGET_HOST> 'docker run --rm -i -v <TARGET_DATA_VOLUME_NAME>:/to alpine ash -c "cd /to ; tar -xpvf - " '
```
