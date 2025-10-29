import docker

client = docker.from_env()

def ensure_prereqs_installed(container_name):
    container = client.containers.get(container_name)

    # Step 1: Detect OS and version
    exit_code, os_release = container.exec_run("cat /etc/os-release", user="root")
    if exit_code != 0:
        raise RuntimeError("Failed to read /etc/os-release")

    os_info = parse_os_release(os_release.decode())

    os_id = os_info.get("ID", "").lower()
    version_id = os_info.get("VERSION_ID", "").split('.')[0]  # Major version only

    print(f"Detected OS: {os_id} {version_id}")

    # Step 2: Check if EPEL is already installed
    epel_check_cmd = "dnf repolist enabled | grep epel"
    exit_code, output = container.exec_run(epel_check_cmd, user="root")
    epel_installed = (exit_code == 0)

    # Step 3: Determine and run missing pre-reqs
    commands = []

    if not epel_installed:
        if os_id in ["rhel", "ol"]:
            commands.append(f"dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-{version_id}.noarch.rpm")
        elif os_id in ["almalinux", "rocky"]:
            commands.append("dnf install -y epel-release")

    # Add codeready-builder or equivalent
    if os_id == "rhel":
        if version_id == "10":
            commands.append("subscription-manager repos --enable codeready-builder-for-rhel-10-x86_64-rpms")
        elif version_id == "9":
            commands.append("dnf config-manager --set-enabled codeready-builder-for-rhel-9-rhui-rpms")
    elif os_id == "ol":
        commands.append(f"dnf config-manager --set-enabled ol{version_id}_codeready_builder")
    elif os_id in ["almalinux", "rocky"]:
        commands.append("dnf config-manager --set-enabled crb")

    # Step 4: Run the commands
    for cmd in commands:
        print(f"Running in {container_name}: {cmd}")
        exit_code, output = container.exec_run(cmd, user="root")
        if exit_code != 0:
            print(f"❌ Command failed: {cmd}")
            print(output.decode(errors='replace'))
        else:
            print(f"✅ Command succeeded: {cmd}")

def parse_os_release(content):
    """Parses /etc/os-release into a dict"""
    info = {}
    for line in content.strip().splitlines():
        if "=" in line:
            k, v = line.strip().split("=", 1)
            info[k] = v.strip('"')
    return info
