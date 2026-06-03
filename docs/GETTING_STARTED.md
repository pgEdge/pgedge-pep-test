# Getting Started with PEP Testing Framework

Welcome to the PEP (PostgreSQL Extension Package) Testing Framework! This guide will help you set up and run your first tests.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Running Your First Test](#running-your-first-test)
- [Understanding Test Results](#understanding-test-results)
- [Common Issues](#common-issues)
- [Next Steps](#next-steps)

---

## Prerequisites

### System Requirements

- **Operating System:** Linux, macOS, or Windows (with WSL)
- **Python:** 3.8 or higher
- **Docker:** 20.10 or higher
- **Git:** 2.x or higher
- **Disk Space:** At least 10GB free space
- **RAM:** Minimum 4GB (8GB recommended)

### Required Knowledge

- Basic command-line interface (CLI) usage
- Understanding of Docker containers
- Familiarity with PostgreSQL (helpful but not required)

---

## Installation

### Step 1: Clone the Repository

```bash
git clone https://github.com/your-org/auto-test-native_pg.git
cd auto-test-native_pg
```

### Step 2: Set Up Python Virtual Environment

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate
```

### Step 3: Install Python Dependencies

```bash
pip install -r requirements.txt
```

**requirements.txt should contain:**
```
pytest>=7.0.0
pytest-html>=3.1.0
pytest-metadata>=2.0.0
docker>=6.0.0
python-dotenv>=0.19.0
```

### Step 4: Verify Docker Installation

```bash
docker --version
docker ps
```

If Docker is not installed, follow the [official Docker installation guide](https://docs.docker.com/get-docker/).

### Step 5: Set Up Docker Containers

#### Option A: Pull Pre-built Containers (Recommended)

```bash
# RHEL family containers
docker pull rockylinux:9
docker pull almalinux:9

# Debian family containers
docker pull debian:11
docker pull ubuntu:22.04
```

#### Option B: Use Existing Containers

If you already have running containers, note their names:

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

### Step 6: Start Test Containers

```bash
# Start RHEL container
docker run -d --name auto-rocky9-arm \
  --hostname rocky9 \
  rockylinux:9 tail -f /dev/null

# Start Debian container
docker run -d --name auto-debian11-arm \
  --hostname debian11 \
  debian:11 tail -f /dev/null
```

**Verify containers are running:**

```bash
docker ps
```

You should see your containers in the "Up" state.

### Step 7: Configure Environment Variables

Create a `.env` file in the project root:

```bash
cp env.example env  # If example exists
# OR
touch env
```

**Edit `.env` with your settings:**

```bash
# Container Configuration
export CONTAINERS=auto-rocky9-arm
export DEB_CONTAINERS=auto-debian11-arm

# PostgreSQL Configuration
export PG_MAJOR_VERSION=18
export PG_PORT=5432
export PG_DATA_DIR=/tmp/n1
export PG_USER=postgres
export DEB_PG_USER=postgres

# Package Paths - RHEL
export PG_BIN_PATH=/usr/pgsql-18/bin

# Package Paths - Debian
export DEB_PG_BIN_PATH=/usr/lib/postgresql/18/bin
export DEB_PG_PATH=/usr/lib/postgresql/18
export DEB_PG_SHARE_PATH=/usr/share/postgresql/18

# Repository Configuration
export REPO=release  # Options: release, staging, daily

# Component Versions
export PGEDGE_LOLOR_18_VERSION=1.2.2
export PGEDGE_SNOWFLAKE_18_VERSION=2.4

# Component Configuration
export COMPONENTS=pgedge-lolor_18
export BASE_EXTENSIONS=lolor
export CHECK_EXTENSIONS=true
```

**Save the file** and load it:

```bash
source env
```

---

## Quick Start

### Verify Installation

Run a quick health check:

```bash
# Check Python packages
pip list | grep pytest

# Check Docker containers
docker ps

# Check environment variables
echo $CONTAINERS
echo $DEB_CONTAINERS
```

### Directory Structure Overview

```
auto-test-native_pg/
├── aspects/              # Reusable business logic
├── component-test/       # Test suites for each component
├── config/              # Component configurations
├── configuration/       # PostgreSQL version configs
├── docs/                # Documentation
├── expected-output/     # Expected test results
├── sql/                 # SQL test files
├── test-logs/           # Test execution logs
├── actual-output/       # Runtime test outputs
├── .env                 # Environment configuration
└── run_all_envs.sh     # Main test runner
```

---

## Running Your First Test

### Test a Single Component

Let's run the LOLOR extension tests:

```bash
cd component-test
pytest test_pep_lolor.py -v
```

**Expected Output:**

```
============================= test session starts ==============================
platform linux -- Python 3.10.12, pytest-7.4.3, pluggy-1.3.0
collected 24 items

test_pep_lolor.py::test_install_prerequisites[auto-rocky9-arm-rhel] PASSED [  4%]
test_pep_lolor.py::test_configure_repository[auto-rocky9-arm-rhel] PASSED [  8%]
test_pep_lolor.py::test_component_install[auto-rocky9-arm-rhel] PASSED [ 12%]
...
========================= 24 passed in 120.45s ===============================
```

### Run a Specific Test Function

```bash
pytest test_pep_lolor.py::test_component_install -v
```

### Run Tests for Multiple Containers

The tests will automatically run on all configured containers (RHEL and Debian):

```bash
pytest test_pep_lolor.py -v
```

### Run Tests with HTML Report

```bash
pytest test_pep_lolor.py --html=../test-logs/lolor-report.html --self-contained-html
```

View the report:

```bash
open ../test-logs/lolor-report.html  # macOS
xdg-open ../test-logs/lolor-report.html  # Linux
```

### Run All Component Tests

```bash
# Run all tests in the component-test directory
pytest -v
```

---

## Understanding Test Results

### Test Output Format

```
test_pep_lolor.py::test_component_install[auto-rocky9-arm-rhel] PASSED [ 12%]
│                    │                          │          │       └─ Progress %
│                    │                          │          └─ Test Status
│                    │                          └─ Container & Platform
│                    └─ Test Function Name
└─ Test File
```

### Test Statuses

- ✅ **PASSED** - Test completed successfully
- ❌ **FAILED** - Test encountered an error
- ⚠️ **SKIPPED** - Test was skipped (container not running, file missing, etc.)
- 🔄 **XFAIL** - Expected failure (known issue)

### Viewing Detailed Output

Run tests with verbose output:

```bash
pytest test_pep_lolor.py -v -s
```

The `-s` flag shows print statements and detailed output.

### Understanding Test Workflow

Each component test follows this sequence:

1. **Install Prerequisites** - Setup system dependencies
2. **Configure Repository** - Add pgEdge repository
3. **Install Package** - Install the extension package
4. **Verify Version** - Check installed version matches expected
5. **Validate Files** - Verify all bundled files are present
6. **Initialize Cluster** - Create PostgreSQL data directory
7. **Start Server** - Launch PostgreSQL
8. **Run Functional Tests** - Execute SQL tests
9. **Stop Server** - Shutdown PostgreSQL
10. **Uninstall Package** - Remove the extension
11. **Cleanup** - Remove all test artifacts

---

## Common Issues

### Issue 1: Container Not Running

**Error:**
```
docker.errors.NotFound: 404 Client Error: Not Found ("No such container: auto-rocky9-arm")
```

**Solution:**
```bash
# Check if container exists
docker ps -a | grep auto-rocky9-arm

# Start the container
docker start auto-rocky9-arm

# Or create a new one
docker run -d --name auto-rocky9-arm rockylinux:9 tail -f /dev/null
```

### Issue 2: Python Dependencies Missing

**Error:**
```
ModuleNotFoundError: No module named 'pytest'
```

**Solution:**
```bash
# Activate virtual environment
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Issue 3: Permission Denied

**Error:**
```
PermissionError: [Errno 13] Permission denied: '/var/run/docker.sock'
```

**Solution:**
```bash
# Add your user to docker group
sudo usermod -aG docker $USER

# Log out and log back in, or run:
newgrp docker
```

### Issue 4: Container Network Issues

**Error:**
```
Failed to configure repository: Network error
```

**Solution:**
```bash
# Check container networking
docker exec auto-rocky9-arm ping -c 1 google.com

# Restart container
docker restart auto-rocky9-arm
```

### Issue 5: Environment Variables Not Loaded

**Error:**
```
Container list is empty
```

**Solution:**
```bash
# Load environment variables
source env

# Verify they're loaded
echo $CONTAINERS
echo $DEB_CONTAINERS
```

---

## Next Steps

### Learn More

- Read the [User Guide](USER_GUIDE.md) for detailed usage instructions
- Review [Architecture Documentation](ARCHITECTURE.md) to understand the system
- Check [Component Development Guide](COMPONENT_DEVELOPMENT.md) to add new tests

### Explore Examples

1. **Run Snowflake Tests:**
   ```bash
   pytest component-test/test_pep_snowflake.py -v
   ```

2. **Test on Multiple PostgreSQL Versions:**
   ```bash
   # Edit env to change PG_MAJOR_VERSION
   export PG_MAJOR_VERSION=17
   pytest component-test/test_pep_lolor.py -v
   ```

3. **Run Tests in Parallel:**
   ```bash
   pytest component-test/ -n auto
   ```

### AWS Testing (Advanced)

If you want to test on AWS EC2 instances:

1. Configure AWS credentials in `keys/` directory
2. Set up EC2 instances
3. Update `.env` with instance details
4. Run AWS-specific test suites

---

## Getting Help

### Documentation Resources

- **Architecture:** See `docs/ARCHITECTURE.md`
- **User Guide:** See `docs/USER_GUIDE.md`
- **API Reference:** See `docs/API.md`
- **Troubleshooting:** See `docs/TROUBLESHOOTING.md`

### Community Support

- **GitHub Issues:** Report bugs and request features
- **Discussions:** Ask questions and share experiences
- **Wiki:** Community-contributed guides and tips

### Contact

For urgent issues or questions:
- Email: support@pgedge.com
- Slack: #pep-testing-framework
- GitHub: Create an issue at the repository

---

## Appendix: Useful Commands

### Docker Management

```bash
# List all containers
docker ps -a

# Start/stop containers
docker start <container_name>
docker stop <container_name>

# Remove containers
docker rm <container_name>

# View container logs
docker logs <container_name>

# Execute command in container
docker exec -it <container_name> bash
```

### Test Execution

```bash
# Run specific test file
pytest component-test/test_pep_lolor.py

# Run specific test function
pytest component-test/test_pep_lolor.py::test_component_install

# Run with keywords
pytest -k "install"

# Run with markers
pytest -m "smoke"

# Show available tests
pytest --collect-only
```

### Virtual Environment

```bash
# Activate
source .venv/bin/activate

# Deactivate
deactivate

# List installed packages
pip list

# Update package
pip install --upgrade <package_name>
```

---

## Congratulations!

You've successfully set up the PEP Testing Framework and run your first tests. Continue with the [User Guide](USER_GUIDE.md) to learn about advanced features and best practices.

Happy Testing! 🎉
