# pgEdge Enterprise Postgres Test Framework

A pytest-based testing framework for validating pgEdge Enterprise Postgres native packages across multiple platforms, PostgreSQL versions, and components.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Environment Files](#environment-files)
  - [Key Variables](#key-variables)
- [Usage](#usage)
  - [Interactive Mode](#interactive-mode)
  - [Command Line Mode](#command-line-mode)
  - [Examples](#examples)
- [Output](#output)
  - [Test Reports](#test-reports)
  - [Actual Output Files](#actual-output-files)
- [Project Structure](#project-structure)
- [Supported Components](#supported-components)

## Prerequisites

- Python 3.8+
- Docker (for container-based testing)
- SSH access to target test containers

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd pgedge-pep-test
   ```

2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip3 install -r requirements.txt
   ```

## Configuration

### Environment Files

Edit the configuration files located in the `configuration/` directory based on your PostgreSQL version:

| File | PostgreSQL Version |
|------|-------------------|
| `configuration/config16.env` | PostgreSQL 16 |
| `configuration/config17.env` | PostgreSQL 17 |
| `configuration/config18.env` | PostgreSQL 18 |

### Key Variables

Ensure all variables in the config files are correctly set:

```bash
# Container details
export CONTAINERS=auto-alma10-arm,auto-oel9-arm
export DEB_CONTAINERS=auto-debian13-amd,auto-ubuntu2204-arm

# Component versions
export PG_VERSION=16.11
export PG_MAJOR_VERSION=16

# Repository selection (release | staging | daily)
export REPO=staging
```

## Usage

### Interactive Mode

Run without arguments to enter interactive menu mode:

```bash
./run_pep_tf.sh
```

### Command Line Mode

```bash
./run_pep_tf.sh [OPTIONS]
```

**Options:**

| Option | Description | Values |
|--------|-------------|--------|
| `--pgver` | PostgreSQL versions to test | `16`, `17`, `18`, `all` |
| `--platforms` | Target platforms | `rpm`, `deb`, `all` |
| `--components` | Components to test | `server`, `snowflake`, `pgbouncer`, `lolor`, `postgis`, `system_stats`, `vectorizer`, `zerodowntime`, `mcp`, `rag`, `all` |
| `--repo` | Repository to use | `release`, `staging`, `daily` |
| `--help`, `-h` | Show help message | - |

### Examples

```bash
# Test PG 16 and 17 server on DEB platforms with staging repo
./run_pep_tf.sh --pgver 16,17 --platforms deb --components server --repo staging

# Test all versions on RPM only for lolor and postgis
./run_pep_tf.sh --pgver all --platforms rpm --components lolor,postgis

# Test everything with release repo
./run_pep_tf.sh --pgver all --platforms all --components all --repo release
```

## Output

### Test Reports

Test execution reports are saved in the `test-logs/` directory:

- HTML reports with detailed test results
- Consolidated reports organized by timestamp
- Component-specific logs

### Actual Output Files

Relevant output files from test execution are stored in the `actual-output/` folder:

- SQL execution outputs
- Component validation results
- Comparison data for expected vs actual results

## Project Structure

```
pgedge-pep-test/
├── component-test/          # Test modules for each component
│   ├── test_pep_server.py
│   ├── test_pep_lolor.py
│   ├── test_pep_snowflake.py
│   ├── test_pep_postgis.py
│   ├── test_pep_pgbouncer.py
│   ├── test_pep_system_stats.py
│   ├── test_pep_vectorizer.py
│   ├── test_pep_mcp.py
│   ├── test_pep_rag.py
│   └── test_integration_zerodowntime.py
├── configuration/           # Environment configuration files
│   ├── config16.env
│   ├── config17.env
│   └── config18.env
├── expected-output/         # Expected test outputs for comparison
├── actual-output/           # Actual test outputs
├── test-logs/               # Test execution reports
├── sql/                     # SQL scripts for testing
├── aspects/                 # Test aspects and utilities
├── utillities/              # Helper utilities
├── run_pep_tf.sh            # Main test runner script
└── requirements.txt         # Python dependencies
```

## Supported Components

| Component | Description |
|-----------|-------------|
| `server` | PostgreSQL server and contrib packages |
| `snowflake` | Snowflake sequence generator extension |
| `pgbouncer` | Connection pooler |
| `lolor` | Large object logical replication |
| `postgis` | Spatial and geographic objects |
| `system_stats` | System statistics extension |
| `vectorizer` | AI/ML vectorization tools |
| `zerodowntime` | Zero downtime upgrade testing |
| `mcp` | Model Context Protocol components |
| `rag` | RAG server components |

