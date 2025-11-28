#!/usr/bin/env python3
"""
Run PEP server tests across multiple environments and platforms.
Each .env file is loaded in isolation for a clean test run.
"""

import os
import subprocess
from dotenv import dotenv_values

# --- Configuration ---
ENV_DIR = "./configuration"
LOG_DIR = "test-logs"

os.makedirs(LOG_DIR, exist_ok=True)


def prompt_choices(prompt, options):
    """Prompt user for a choice and return normalized selection."""
    print(prompt)
    for i, option in enumerate(options, start=1):
        print(f"{i}) {option}")
    choice = input(f"Enter your choice ({'/'.join(options)}): ").strip().lower()
    if choice in ["all", str(len(options))]:
        return [opt.lower() for opt in options if opt.lower() != "all"]
    return [choice]


def run_pytest(env_file, platform, env_name):
    """Run pytest for the given environment and platform."""
    if platform == "rpm":
        test_file = "test_pep_server_rhel.py"
        report_path = os.path.join(LOG_DIR, f"report-rpm-{env_name}.html")
    elif platform == "deb":
        test_file = "test_pep_server_deb.py"
        report_path = os.path.join(LOG_DIR, f"report-deb-{env_name}.html")
    else:
        print(f"⚠️ Unknown platform: {platform}")
        return

    print(f"▶️ Running {platform.upper()} tests for env {env_name}")

    # Load only this .env file
    env_vars = os.environ.copy()
    env_vars.update(dotenv_values(env_file))

    # Run pytest in isolated subprocess
    cmd = [
        "pytest",
        "-v",
        "-s",
        test_file,
        f"--html={report_path}",
        "--self-contained-html",
    ]

    subprocess.run(cmd, env=env_vars, check=False)
    print(f"✅ Completed {platform.upper()} tests for env {env_name}")
    print(f"   → Report saved to {report_path}")


def main():
    print("Select environment(s) to run:")
    env_options = ["16", "17", "18", "all"]
    env_choices = prompt_choices("", env_options)

    print("\nSelect platform(s) to run:")
    platform_options = ["rpm", "deb", "all"]
    platform_choices = prompt_choices("", platform_options)

    print("\n🚀 Starting test runs...")

    for env_name in env_choices:
        env_file = os.path.join(ENV_DIR, f".env-{env_name}")
        if not os.path.exists(env_file):
            print(f"⚠️ Skipping missing environment file: {env_file}")
            continue

        print(f"\n🔹 Running tests for environment file: {env_file}")

        for platform in platform_choices:
            run_pytest(env_file, platform, env_name)

    print("\n✅ All selected tests completed.")
    print(f"📁 Reports available in: {LOG_DIR}/")


if __name__ == "__main__":
    main()
