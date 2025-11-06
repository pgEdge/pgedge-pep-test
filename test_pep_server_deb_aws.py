#!/usr/bin/env python3
"""SSH test runner for AWS EC2 instance"""

import paramiko
import os

# Configuration
HOSTNAME = "ec2-65-0-18-65.ap-south-1.compute.amazonaws.com"
USERNAME = "rocky"
KEY_PATH = "keys/zaid_key_official.pem"
REPO = os.getenv("REPO", "release")

# Connect to AWS
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
key = paramiko.RSAKey.from_private_key_file(KEY_PATH)
client.connect(hostname=HOSTNAME, username=USERNAME, pkey=key)

print(f"✅ Connected to {HOSTNAME}\n")

# Detect platform
print("--- Detecting platform ---")
stdin, stdout, stderr = client.exec_command("command -v dnf", get_pty=True)
exit_code = stdout.channel.recv_exit_status()

if exit_code == 0:
    platform = "rhel"
else:
    stdin, stdout, stderr = client.exec_command("/bin/bash -c 'command -v apt-get'", get_pty=True)
    exit_code = stdout.channel.recv_exit_status()
    platform = "ubuntu" if exit_code == 0 else None

if not platform:
    print("❌ No supported package manager found")
    client.close()
    exit(1)

print(f"✅ Platform: {platform}\n")

# Install pgedge repo
print(f"--- Installing pgedge repo ({platform}) ---")

if platform == "rhel":
    # Install repo
    repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
    stdin, stdout, stderr = client.exec_command(f"sudo dnf install -y {repo_url}", get_pty=True)

    for line in stdout:
        print(line.strip())

    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        print("❌ Failed to install repo")
        client.close()
        exit(1)

    print("✅ Repository installed")

    # Switch repo if needed
    if REPO in ["staging", "daily"]:
        print(f"\n🔄 Switching to {REPO} repo...")
        stdin, stdout, stderr = client.exec_command(
            f"sudo sed -i 's|release|{REPO}|g' /etc/yum.repos.d/pgedge.repo",
            get_pty=True
        )
        stdout.channel.recv_exit_status()
        print(f"✅ Switched to {REPO} repo")

elif platform == "ubuntu":
    # Install repo
    deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
    install_cmd = f"""curl -sSL {deb_url} -o /tmp/pgedge-release.deb && \
dpkg -i /tmp/pgedge-release.deb && \
rm -f /tmp/pgedge-release.deb"""

    stdin, stdout, stderr = client.exec_command(f"sudo /bin/bash -c \"{install_cmd}\"", get_pty=True)

    for line in stdout:
        print(line.strip())

    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        print("❌ Failed to install repo")
        client.close()
        exit(1)

    print("✅ Repository installed")

    # Switch repo if needed
    if REPO in ["staging", "daily"]:
        print(f"\n🔄 Switching to {REPO} repo...")
        stdin, stdout, stderr = client.exec_command(
            f"sudo sed -i 's|release|{REPO}|g' /etc/apt/sources.list.d/pgedge.sources",
            get_pty=True
        )
        stdout.channel.recv_exit_status()
        print(f"✅ Switched to {REPO} repo")

    # apt-get update
    print("\n🔄 Running apt-get update...")
    stdin, stdout, stderr = client.exec_command("sudo apt-get update", get_pty=True)

    for line in stdout:
        print(line.strip())

    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        print("❌ apt-get update failed")
        client.close()
        exit(1)

    print("✅ apt-get update completed")

print("\n✅ All tests completed successfully!")

# Close connection
client.close()
print("🔌 Connection closed")