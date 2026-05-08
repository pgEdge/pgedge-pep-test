# PEP Testing Framework - User Guide

Complete guide for using the PostgreSQL Extension Package (PEP) Testing Framework.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Configuration Management](#configuration-management)
3. [Running Tests](#running-tests)
4. [Test Components](#test-components)
5. [Working with Multiple Platforms](#working-with-multiple-platforms)
6. [Output and Reporting](#output-and-reporting)
7. [Advanced Features](#advanced-features)
8. [Best Practices](#best-practices)
9. [Troubleshooting](#troubleshooting)
10. [FAQ](#faq)

---

## Introduction

The PEP Testing Framework automates testing of PostgreSQL extension packages across multiple platforms (RHEL, Debian, AWS). It provides:

- ✅ Automated installation and validation
- ✅ Multi-platform support (RHEL/Debian)
- ✅ Functional testing with SQL scripts
- ✅ Comprehensive reporting
- ✅ Easy extensibility for new components

### Framework Overview

```
User Input → Test Suite → Aspects (Logic) → Target Environment → Results
```

---

## Configuration Management

### Environment Variables (.env)

The `.env` file is the primary configuration file. It controls:
- Container selection
- PostgreSQL versions
- Package names and paths
- Test parameters

#### Essential Variables

```bash
# Container Configuration
export CONTAINERS=auto-rocky9-arm,auto-alma9-amd
export DEB_CONTAINERS=auto-debian11-amd,auto-ubuntu2204

# PostgreSQL Version
export PG_MAJOR_VERSION=18

# Server Configuration
export PG_PORT=5432
export PG_DATA_DIR=/tmp/n1
export PG_USER=postgres
export DEB_PG_USER=postgres

# Repository
export REPO=release  # or staging, daily
```

#### Component-Specific Variables

```bash
# LOLOR Configuration
export PGEDGE_LOLOR_18_VERSION=1.2.2
export LOLOR_PACKAGE=pgedge-lolor_18
export DEB_LOLOR_PACKAGE=pgedge-postgresql-18-lolor

# Snowflake Configuration
export PGEDGE_SNOWFLAKE_18_VERSION=2.4
export SNOWFLAKE_PACKAGE=pgedge-snowflake_18
export DEB_SNOWFLAKE_PACKAGE=pgedge-postgresql-18-snowflake
export SNOWFLAKE_NODE=1

# Component List
export COMPONENTS=pgedge-lolor_18,pgedge-snowflake_18
export BASE_EXTENSIONS=lolor,snowflake
export CHECK_EXTENSIONS=true
```

#### Path Configuration

```bash
# RHEL Paths
export PG_BIN_PATH=/usr/pgsql-18/bin

# Debian Paths
export DEB_PG_BIN_PATH=/usr/lib/postgresql/18/bin
export DEB_PG_PATH=/usr/lib/postgresql/18
export DEB_PG_SHARE_PATH=/usr/share/postgresql/18
```

### Version-Specific Configuration Files

Files in `configuration/` directory contain version-specific settings:

**configuration/config18.env**
```bash
PG_MAJOR_VERSION=18
PG_BIN_PATH=/usr/pgsql-18/bin
PGEDGE_LOLOR_18_VERSION=1.2.2
PGEDGE_SNOWFLAKE_18_VERSION=2.4
```

**configuration/config17.env**
```bash
PG_MAJOR_VERSION=17
PG_BIN_PATH=/usr/pgsql-17/bin
PGEDGE_LOLOR_17_VERSION=1.2.1
PGEDGE_SNOWFLAKE_17_VERSION=2.3
```

### Component Configuration Files

Files in `config/` directory contain component-specific settings.

---

## Running Tests

### Basic Test Execution

#### Run All Tests for a Component

```bash
cd component-test
pytest test_pep_lolor.py -v
```

#### Run Specific Test Function

```bash
pytest test_pep_lolor.py::test_component_install -v
```

#### Run Tests for Specific Container

```bash
pytest test_pep_lolor.py -k "rocky9" -v
```

#### Run Tests for Specific Platform

```bash
# RHEL only
pytest test_pep_lolor.py -k "rhel" -v

# Debian only
pytest test_pep_lolor.py -k "deb" -v
```

### Advanced Test Execution

#### Parallel Execution

```bash
# Install pytest-xdist
pip install pytest-xdist

# Run tests in parallel
pytest test_pep_lolor.py -n auto
```

#### Stop on First Failure

```bash
pytest test_pep_lolor.py -x
```

#### Run Last Failed Tests

```bash
pytest --lf
```

#### Verbose Output with Prints

```bash
pytest test_pep_lolor.py -v -s
```

#### Custom Markers

```bash
# Run only smoke tests
pytest -m smoke

# Skip slow tests
pytest -m "not slow"
```

### Test Selection Patterns

#### By Name Pattern

```bash
# Run all install tests
pytest -k "install"

# Run all validation tests
pytest -k "validate or verify"

# Exclude cleanup tests
pytest -k "not cleanup"
```

#### By File Pattern

```bash
# Run all LOLOR tests
pytest test_pep_lolor.py

# Run multiple files
pytest test_pep_lolor.py test_pep_snowflake.py
```

---

## Test Components

### LOLOR Extension Tests

**File:** `component-test/test_pep_lolor.py`

**What it tests:**
- LOLOR extension installation
- Logical replication setup
- Publication/subscription configuration
- WAL level settings
- Replication slots

**Key Tests:**
- `test_lolor_extension_loaded` - Verifies extension in shared_preload_libraries
- `test_lolor_replication_setup` - Creates tables and publications
- `test_lolor_wal_level` - Checks WAL level is set to logical

**Run:**
```bash
pytest component-test/test_pep_lolor.py -v
```

### Snowflake Extension Tests

**File:** `component-test/test_pep_snowflake.py`

**What it tests:**
- Snowflake ID generation
- Sequence creation and management
- ID formatting functions
- Multi-sequence handling

**Key Tests:**
- `test_snowflake_node_parameter` - Verifies snowflake.node setting
- `test_snowflake_format_function` - Tests snowflake.format()
- `test_snowflake_sequence_table_creation` - Creates tables with Snowflake IDs

**Run:**
```bash
pytest component-test/test_pep_snowflake.py -v
```

### Component Template

**File:** `component-test/test_pep_component_template.py`

Use this template to create tests for new components.

---

## Working with Multiple Platforms

### Platform Detection

The framework automatically detects platform type:
- **RHEL Family:** Uses `dnf` and `rpm`
- **Debian Family:** Uses `apt` and `dpkg`

### Platform-Specific Execution

Tests are parametrized to run on all configured platforms:

```python
@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_example(container_name, container_type):
    config = get_container_config(container_type)
    package = config["package_name"]
    # Test logic here
```

### Container Configuration

#### RHEL Containers

```bash
# Rocky Linux
docker run -d --name auto-rocky9-arm rockylinux:9 tail -f /dev/null

# AlmaLinux
docker run -d --name auto-alma9-amd almalinux:9 tail -f /dev/null

# Oracle Enterprise Linux
docker run -d --name auto-oel9-arm oraclelinux:9 tail -f /dev/null
```

#### Debian Containers

```bash
# Debian
docker run -d --name auto-debian11-amd debian:11 tail -f /dev/null
docker run -d --name auto-debian12 debian:12 tail -f /dev/null

# Ubuntu
docker run -d --name auto-ubuntu2204 ubuntu:22.04 tail -f /dev/null
docker run -d --name auto-ubuntu2404 ubuntu:24.04 tail -f /dev/null
```

### Multi-Version Testing

Test across multiple PostgreSQL versions:

```bash
# Test PostgreSQL 16
export PG_MAJOR_VERSION=16
pytest component-test/test_pep_lolor.py -v

# Test PostgreSQL 17
export PG_MAJOR_VERSION=17
pytest component-test/test_pep_lolor.py -v

# Test PostgreSQL 18
export PG_MAJOR_VERSION=18
pytest component-test/test_pep_lolor.py -v
```

---

## Output and Reporting

### HTML Reports

Generate HTML reports with pytest-html:

```bash
pytest component-test/test_pep_lolor.py \
  --html=test-logs/lolor-report.html \
  --self-contained-html
```

**Features:**
- ✅ Test summary with pass/fail counts
- ✅ Detailed error messages and tracebacks
- ✅ Test duration statistics
- ✅ Screenshots (if configured)

### Log Files

Test logs are stored in `test-logs/` directory:

```
test-logs/
├── lolor-report.html
├── snowflake-report.html
├── execution-YYYYMMDD-HHMMSS.log
└── errors.log
```

### Actual Output Files

Runtime test outputs are saved in `actual-output/`:

```
actual-output/
└── sql/
    └── lolor/
        └── 18/
            ├── rpm/
            │   └── lolor-020125-143022.txt
            └── deb/
                └── lolor-020125-143045.txt
```

**File Format:**
```
# Functional Smoke Test for pgedge-lolor_18
# Container: auto-rocky9-arm
# Container Type: rhel
# PostgreSQL Version: 18
# Date: 020125 Time: 143022
# SQL File: ./sql/lolor.sql
================================================================================

[SQL output here]
```

### Expected Output Comparison

The framework compares actual outputs with expected baselines:

```
expected-output/
├── rpm/
│   └── lolor      # Expected files for RHEL
└── deb/
    └── lolor      # Expected files for Debian
```

**Validation:**
- ✅ All expected files present
- ✅ Version normalization (handles /17/ vs /18/)
- ⚠️ Extra files reported but don't fail test

---

## Advanced Features

### Custom Test Markers

Add markers to categorize tests:

```python
@pytest.mark.smoke
@pytest.mark.rhel_only
def test_quick_validation(container_name, container_type):
    # Quick validation test
    pass
```

**Run marked tests:**
```bash
pytest -m smoke
pytest -m "rhel_only"
```

### Fixtures and Setup

Use pytest fixtures for common setup:

```python
@pytest.fixture
def postgres_connection(container, pgbin, pgport, pguser):
    """Provide a PostgreSQL connection"""
    # Setup connection
    yield connection
    # Teardown
```

### Parametrized Tests

Run same test with different inputs:

```python
@pytest.mark.parametrize("component", ["lolor", "snowflake", "pgvector"])
def test_multiple_components(container, component):
    # Test each component
    pass
```

### SQL Test Files

Create SQL test files in `sql/` directory:

**sql/lolor.sql**
```sql
-- Test LOLOR functionality
CREATE EXTENSION IF NOT EXISTS lolor;

-- Create test table
CREATE TABLE replication_test (
    id SERIAL PRIMARY KEY,
    data TEXT
);

-- Create publication
CREATE PUBLICATION test_pub FOR TABLE replication_test;

-- Insert test data
INSERT INTO replication_test (data) VALUES ('test1'), ('test2');

-- Verify data
SELECT * FROM replication_test;
```

### Custom Aspects

Create reusable logic in `aspects/`:

**aspects/custom_validation.py**
```python
def validate_custom_feature(container, feature_name):
    """Validate custom feature"""
    # Custom validation logic
    return success, message
```

**Use in tests:**
```python
from aspects import custom_validation

def test_custom_feature(container):
    success, msg = custom_validation.validate_custom_feature(
        container, "my_feature"
    )
    assert success, msg
```

---

## Best Practices

### 1. Test Organization

✅ **DO:**
- Keep test files focused on one component
- Use descriptive test function names
- Group related tests together
- Follow the template structure

❌ **DON'T:**
- Mix multiple components in one test file
- Create overly complex tests
- Duplicate code across test files

### 2. Configuration Management

✅ **DO:**
- Use environment variables for configuration
- Keep sensitive data in `.env` (not committed)
- Document all configuration options
- Use version-specific config files

❌ **DON'T:**
- Hard-code values in test files
- Commit credentials to version control
- Mix configuration with test logic

### 3. Test Execution

✅ **DO:**
- Run tests in isolated containers
- Clean up after each test
- Use parallel execution for speed
- Generate HTML reports for CI/CD

❌ **DON'T:**
- Run tests on production systems
- Skip cleanup steps
- Ignore test failures

### 4. Error Handling

✅ **DO:**
- Provide clear error messages
- Log detailed debugging information
- Handle platform-specific errors
- Skip tests gracefully when appropriate

❌ **DON'T:**
- Suppress errors silently
- Use generic error messages
- Fail on expected platform differences

### 5. Maintenance

✅ **DO:**
- Update expected outputs regularly
- Keep documentation current
- Review and refactor tests periodically
- Tag releases and versions

❌ **DON'T:**
- Let tests become outdated
- Ignore deprecated features
- Skip dependency updates

---

## Troubleshooting

### Test Failures

#### Package Installation Failed

**Problem:** Package installation returns error

**Debug:**
```bash
# Check repository configuration
docker exec <container> cat /etc/yum.repos.d/pgedge.repo  # RHEL
docker exec <container> cat /etc/apt/sources.list.d/pgedge.list  # Debian

# Test repository connectivity
docker exec <container> dnf repolist  # RHEL
docker exec <container> apt update  # Debian

# Check package availability
docker exec <container> dnf search pgedge-lolor  # RHEL
docker exec <container> apt search pgedge-postgresql-lolor  # Debian
```

**Solution:**
- Verify repository URL in configuration
- Check network connectivity
- Ensure package name is correct for platform

#### PostgreSQL Won't Start

**Problem:** PostgreSQL server fails to start

**Debug:**
```bash
# Check PostgreSQL logs
docker exec <container> cat /tmp/n1/log/postgresql-*.log

# Check data directory
docker exec <container> ls -la /tmp/n1/

# Check port availability
docker exec <container> netstat -tuln | grep 5432
```

**Solution:**
- Verify data directory exists and has correct permissions
- Check if port is already in use
- Review GUC parameters in postgresql.conf

#### Test Skipped Unexpectedly

**Problem:** Tests are skipping without clear reason

**Debug:**
```bash
# Run with verbose output
pytest test_pep_lolor.py -v -s

# Check skip conditions in test code
grep "pytest.skip" test_pep_lolor.py
```

**Solution:**
- Ensure containers are running
- Verify expected output files exist
- Check environment variables are loaded

### Performance Issues

#### Slow Test Execution

**Problem:** Tests take too long to complete

**Solutions:**
```bash
# Use parallel execution
pytest -n auto

# Run subset of tests
pytest -k "not slow"

# Skip cleanup during development
pytest --skip-cleanup  # If supported
```

#### Container Resource Limits

**Problem:** Containers running out of resources

**Solutions:**
```bash
# Increase container resources
docker update --memory=4g <container>

# Clean up unused containers
docker container prune

# Monitor container resources
docker stats
```

---

## FAQ

### General Questions

**Q: Can I test custom extensions?**

A: Yes! Copy `test_pep_component_template.py`, update package names and SQL tests, then run your new test suite.

**Q: How do I test on AWS?**

A: Configure AWS credentials in `keys/`, set up EC2 instances, update `.env`, and run AWS-specific tests.

**Q: Can I test multiple PostgreSQL versions simultaneously?**

A: Yes, but you'll need separate containers for each version or run tests sequentially with different config files.

**Q: How do I add a new platform (e.g., Fedora)?**

A: Add container to `.env`, ensure platform detection works in aspects modules, and test with existing test suites.

### Configuration Questions

**Q: Where should I store credentials?**

A: Store in `.env` file (add to `.gitignore`) or use environment variables. Never commit credentials.

**Q: How do I switch between repositories (release/staging/daily)?**

A: Set `export REPO=staging` in `.env` file and reload configuration.

**Q: Can I customize package names?**

A: Yes, set `LOLOR_PACKAGE` and `DEB_LOLOR_PACKAGE` in `.env` for each component.

### Testing Questions

**Q: How do I debug a failing test?**

A: Run with `-v -s` flags, check container logs, and exec into container to investigate manually.

**Q: Can I run tests without Docker?**

A: Not currently. The framework requires Docker for container isolation.

**Q: How do I generate test reports for CI/CD?**

A: Use `pytest --html=report.html --junitxml=results.xml` and configure your CI to archive artifacts.

---

## Additional Resources

### Documentation

- [Getting Started Guide](GETTING_STARTED.md)
- [Architecture Documentation](ARCHITECTURE.md)
- [API Reference](API.md)
- [Component Development Guide](COMPONENT_DEVELOPMENT.md)

### Examples

Check the `component-test/` directory for:
- `test_pep_lolor.py` - LOLOR extension example
- `test_pep_snowflake.py` - Snowflake extension example
- `test_pep_component_template.py` - Template for new components

### Community

- GitHub Issues: Report bugs
- GitHub Discussions: Ask questions
- Pull Requests: Contribute improvements

---

## Appendix

### Environment Variable Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `CONTAINERS` | RHEL container names | `auto-rocky9-arm` |
| `DEB_CONTAINERS` | Debian container names | `auto-debian11-amd` |
| `PG_MAJOR_VERSION` | PostgreSQL version | `18` |
| `PG_PORT` | PostgreSQL port | `5432` |
| `PG_DATA_DIR` | Data directory path | `/tmp/n1` |
| `REPO` | Repository type | `release` |

### Test Workflow Reference

```
Prerequisites → Repository → Install → Verify → Validate →
Initialize → Start → Test → Stop → Uninstall → Cleanup
```

### Command Reference

```bash
# Run tests
pytest <file> -v           # Verbose output
pytest <file> -s           # Show prints
pytest <file> -k <pattern> # Filter tests
pytest <file> -x           # Stop on first failure
pytest <file> -n auto      # Parallel execution

# Generate reports
pytest --html=report.html
pytest --junitxml=results.xml

# Docker commands
docker ps                  # List containers
docker exec -it <name> bash # Enter container
docker logs <name>         # View logs
```

---

**Happy Testing!** For questions or support, consult the documentation or open an issue on GitHub.
