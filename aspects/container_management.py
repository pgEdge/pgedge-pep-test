"""
Container Management Module

This module provides functions to manage Docker containers for testing,
including creating, starting, and ensuring containers are running.

When AWS_MODE=true the client is an AWSInstanceClient and containers are
live EC2 instances — no create/start logic is needed in that case.
"""

import docker
import time


# Container image mapping based on OS identifier
CONTAINER_IMAGES = {
    "rocky9": "rockylinux:9",
    "rocky10": "rockylinux:10",
    "rocky8": "rockylinux:8",
    "alma9": "almalinux:9",
    "alma10": "almalinux:10",
    "alma8": "almalinux:8",
    "oel9": "oraclelinux:9",
    "oel10": "oraclelinux:10",
    "oel8": "oraclelinux:8",
    "debian11": "debian:11",
    "debian12": "debian:12",
    "debian13": "debian:13",
    "ubuntu2204": "ubuntu:22.04",
    "ubuntu2404": "ubuntu:24.04",
    "ubuntu2604": "ubuntu:26.04",
}


def get_base_image_for_container(container_name, container_type):
    """
    Determine the base Docker image based on container name.

    Args:
        container_name: Name of the container (e.g., "auto-rocky9-arm")
        container_type: Type of container ("rhel" or "deb")

    Returns:
        str: Docker image name
    """
    # Extract OS identifier from container name
    name_lower = container_name.lower()

    # Try to match against known image keys
    for key, image in CONTAINER_IMAGES.items():
        if key in name_lower:
            return image

    # Fallback to default images based on container type
    if container_type == "rhel":
        return "rockylinux:9"
    else:  # deb
        return "debian:12"


def ensure_container_running(client, container_name, container_type, force_recreate=False):
    """
    Ensure a container exists and is running. Create and start if necessary.

    Args:
        client: Docker client object (or AWSInstanceClient in AWS_MODE)
        container_name: Name of the container / AWS instance
        container_type: Type of container ("rhel" or "deb")
        force_recreate: If True, remove any existing container and create a fresh one.
                        Prevents disk-space accumulation from prior test runs.

    Returns:
        tuple: (container, created, message)
            - container: Docker container object or SSHExecutor
            - created: Boolean indicating if container was newly created
            - message: Status message describing what was done
    """
    # AWS_MODE: instances are already running — just return the SSH executor.
    from aspects.aws_client import AWSInstanceClient
    if isinstance(client, AWSInstanceClient):
        executor = client.containers.get(container_name)
        return executor, False, f"AWS instance '{container_name}' connected ({executor._host})"

    created = False
    message = ""

    try:
        # Try to get existing container
        container = client.containers.get(container_name)

        if force_recreate:
            print(f"force_recreate=True: removing existing container '{container_name}' (status: {container.status}) for a clean slate...")
            container.remove(force=True)
            # Prune dangling volumes to reclaim disk space
            try:
                client.volumes.prune()
                print("Pruned dangling volumes to reclaim disk space")
            except docker.errors.APIError:
                pass
            raise docker.errors.NotFound(container_name)

        # Check container status
        if container.status == "running":
            message = f"Container '{container_name}' is already running"
            return container, created, message

        # Container exists but not running - start it
        print(f"Container '{container_name}' found but not running (status: {container.status})")
        print(f"Starting container '{container_name}'...")
        container.start()

        # Wait for container to be ready
        time.sleep(2)
        container.reload()

        message = f"Container '{container_name}' started successfully"
        return container, created, message

    except docker.errors.NotFound:
        # Container doesn't exist - create it
        print(f"Container '{container_name}' not found. Creating new container...")

        base_image = get_base_image_for_container(container_name, container_type)
        print(f"Using base image: {base_image}")

        # Detect platform from container name
        platform = None
        name_lower = container_name.lower()
        if "-arm" in name_lower or "arm64" in name_lower:
            platform = "linux/arm64"
        elif "-amd" in name_lower or "amd64" in name_lower or "x86" in name_lower:
            platform = "linux/amd64"

        if platform:
            print(f"Detected platform: {platform}")

        try:
            # Pull image if not present locally
            print(f"Ensuring image '{base_image}' is available...")
            try:
                if platform:
                    client.images.pull(base_image, platform=platform)
                else:
                    client.images.pull(base_image)
                print(f"✓ Image '{base_image}' pulled successfully")
            except docker.errors.APIError as e:
                print(f"Warning: Could not pull image: {e}")
                print("Attempting to use local image if available...")

            # Use /bin/bash to keep container running (matches manual docker run command)
            command = "/bin/bash"

            # Create container with appropriate configuration
            # Matches: docker run -dit --name <name> --platform <platform> <image> /bin/bash
            create_kwargs = {
                "image": base_image,
                "name": container_name,
                "detach": True,
                "privileged": True,
                "tty": True,
                "stdin_open": True,
                "command": command,
            }

            # Add platform if detected
            if platform:
                create_kwargs["platform"] = platform

            container = client.containers.create(**create_kwargs)

            # Start the container
            container.start()
            created = True

            # Wait for container to initialize
            print(f"Waiting for container '{container_name}' to initialize...")
            time.sleep(3)
            container.reload()

            message = f"Container '{container_name}' created and started successfully using image '{base_image}'"
            if platform:
                message += f" on platform {platform}"
            return container, created, message

        except docker.errors.ImageNotFound:
            raise Exception(f"Base image '{base_image}' not found and could not be pulled")
        except docker.errors.APIError as e:
            raise Exception(f"Failed to create container '{container_name}': {str(e)}")

    except docker.errors.APIError as e:
        raise Exception(f"Docker API error while managing container '{container_name}': {str(e)}")


def stop_container(client, container_name):
    """
    Stop a running container.

    Args:
        client: Docker client object
        container_name: Name of the container

    Returns:
        tuple: (success, message)
    """
    try:
        container = client.containers.get(container_name)

        if container.status == "running":
            container.stop()
            return True, f"Container '{container_name}' stopped successfully"
        else:
            return True, f"Container '{container_name}' is not running (status: {container.status})"

    except docker.errors.NotFound:
        return False, f"Container '{container_name}' not found"
    except docker.errors.APIError as e:
        return False, f"Failed to stop container '{container_name}': {str(e)}"


def remove_container(client, container_name, force=False):
    """
    Remove a container.

    Args:
        client: Docker client object
        container_name: Name of the container
        force: Force removal even if container is running

    Returns:
        tuple: (success, message)
    """
    try:
        container = client.containers.get(container_name)
        container.remove(force=force)
        return True, f"Container '{container_name}' removed successfully"

    except docker.errors.NotFound:
        return False, f"Container '{container_name}' not found"
    except docker.errors.APIError as e:
        return False, f"Failed to remove container '{container_name}': {str(e)}"