# pgEdge Enterprise Postgres Test Framework

A pytest-based testing framework for validating pgEdge Enterprise Postgres native packages across multiple platforms, PostgreSQL versions, and components.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Environment Files](#environment-files)
  - [Key Variables](#key-variables)
  - [Container Selection (Docker Mode)](#container-selection-docker-mode)
  - [AWS Instance Selection (AWS Mode)](#aws-instance-selection-aws-mode)
- [Usage](#usage)
  - [Interactive Mode](#interactive-mode)
  - [Command Line Mode](#command-line-mode)
  - [Examples](#examples)
- [Output](#output)
  - [Test Reports](#test-reports)
  - [Actual Output Files](#actual-output-files)
- [Project Structure](#project-structure)
- [Supported Platforms](#supported-platforms)
- [Supported Components](#supported-components)

## Prerequisites

- Python 3.8+
- Docker (for Docker-based testing)
- SSH access to target machines (for AWS-based testing)

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
| `configuration/config19.env` | PostgreSQL 19 |

### Key Variables

Ensure all variables in the config files are correctly set:

```bash
# Container/instance names (populated automatically from containers_list.json or aws_instances.json)
export CONTAINERS=auto-rocky9-arm,auto-alma10-arm
export DEB_CONTAINERS=auto-debian13-amd,auto-ubuntu2204-arm

# Component versions
export PG_VERSION=16.11
export PG_MAJOR_VERSION=16

# Repository selection (release | staging | daily)
export REPO=staging
```

---

### Container Selection (Docker Mode)

In Docker mode (default), the test runner reads **`configuration/containers_list.json`** to determine which containers to test against. You do not need to edit `CONTAINERS` / `DEB_CONTAINERS` manually — the script builds them automatically from the JSON file.

**`configuration/containers_list.json`** structure:

```json
{
  "rhel": [
    { "name": "auto-rocky9-arm", "alias": "rocky9-arm64", "description": "Rocky Linux 9 / ARM64", "enabled": true  },
    { "name": "my-rocky9-amd",   "alias": "rocky9-amd64", "description": "Rocky Linux 9 / AMD64", "enabled": false }
  ],
  "deb": [
    { "name": "auto-ubuntu2604-arm", "alias": "ubuntu2604-arm64", "description": "Ubuntu 26.04 LTS / ARM64", "enabled": true  },
    { "name": "auto-ubuntu2604-amd", "alias": "ubuntu2604-amd64", "description": "Ubuntu 26.04 LTS / AMD64", "enabled": true  }
  ]
}
```

- Set `"enabled": true` to include a container in the test run.
- Set `"enabled": false` to skip it without removing the entry.
- Container names must match existing Docker containers on the host.
- `"alias"` is the short user-facing name (e.g. `rocky9-arm64`) accepted by the `--containers` override.

At runtime, only `enabled: true` entries are loaded into `CONTAINERS` (RPM) and `DEB_CONTAINERS` (DEB).

To pick a custom set for a single run without editing the catalog, pass `--containers` (a CSV of aliases or canonical names; `all` selects the entire catalog). A listed platform's opposite architecture is also selectable even if only one arch is listed. Run `./run_pep_tf.sh --list-containers` to print the catalog. See [docs/CI.md](docs/CI.md#selecting-container-targets-at-runtime) for full behavior.

---

### AWS Instance Selection (AWS Mode)

When running against live AWS EC2 instances instead of Docker containers, use `--target aws`. The test runner reads **`configuration/aws_instances.json`** and the `AWSInstanceClient` replaces the Docker client transparently — all existing test files work unchanged.

**`configuration/aws_instances.json`** structure:

```json
{
  "rhel": [
    {
      "name":        "z_Rocky9_ARM",
      "host":        "ec2-3-111-170-196.ap-south-1.compute.amazonaws.com",
      "username":    "rocky",
      "key_file":    "",
      "description": "Rocky Linux 9 ARM / ap-south-1",
      "enabled":     true
    }
  ],
  "deb": [
    {
      "name":        "z_Debian13_AMD",
      "host":        "ec2-3-110-84-181.ap-south-1.compute.amazonaws.com",
      "username":    "admin",
      "key_file":    "",
      "description": "Debian 13 AMD / ap-south-1",
      "enabled":     true
    }
  ]
}
```

- Set `"enabled": true` to include an instance in the AWS test run.
- `"key_file"` is optional — leave it empty (`""`) and use SSH agent or `AWS_SSH_KEY_PATH` instead (see below).

#### SSH Key Authentication

Three methods are supported, resolved in this order:

| Priority | Method | How to use |
|----------|--------|------------|
| 1 | `key_file` in JSON | Set path directly in `aws_instances.json` (not recommended for shared repos) |
| 2 | Environment variable | `export AWS_SSH_KEY_PATH=/path/to/key.pem` |
| 3 | SSH agent | `ssh-add /path/to/key.pem` (recommended) |

**Recommended — SSH agent (no file path stored anywhere):**
```bash
ssh-add ~/.ssh/your-key.pem   # once per session
./run_pep_tf.sh --target aws --pgver 17 --components server
```

**Alternative — environment variable (good for CI/CD):**
```bash
export AWS_SSH_KEY_PATH=/path/to/your-key.pem
./run_pep_tf.sh --target aws --pgver 17 --components server
```

> **Never commit `.pem` key files.** The `keys/` directory is gitignored for local storage. Prefer SSH agent or `AWS_SSH_KEY_PATH` over `key_file` in JSON.

#### Passwordless Sudo Requirement

AWS instances must be configured to allow passwordless `sudo` for the SSH user. See the [pgEdge prerequisites guide](https://docs.pgedge.com/platform/prerequisites/#configuring-passwordless-sudo) for setup instructions.

---

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
| `--pgver` | PostgreSQL versions to test | `16`, `17`, `18`, `19`, `all` |
| `--platforms` | Target platforms | `rpm`, `deb`, `all` |
| `--components` | Components to test | `server`, `snowflake`, `pgbouncer`, `pgbackrest`, `postgrest`, `lolor`, `postgis`, `system_stats`, `vectorizer`, `zerodowntime`, `mcp`, `rag`, `ace`, `repo_health`, `docloader`, `anonymizer`, `pg_vectorize`, `pg_tokenizer`, `vchord_bm25`, `pgaudit`, `pgadmin4`, `patroni`, `pg_stat_monitor`, `ai_db_workbench`, `radar`, `spock_patroni_failover`, `llvmjit`, `spock`, `supautils`, `ai_kb`, `pgvector`, `all` |
| `--repo` | Repository to use | `release`, `staging`, `daily` |
| `--target` | Execution target | `docker` (default), `aws` |
| `--arch` | Filter enabled containers by architecture | `arm64`, `amd64` |
| `--containers` | Runtime container override (CSV of aliases/names; `all` = whole catalog) | see [CI.md](docs/CI.md) |
| `--list-containers` | Print the container catalog and exit | - |
| `--help`, `-h` | Show help message | - |

### Examples

```bash
# Test PG 16 and 17 server on DEB platforms with staging repo (Docker)
./run_pep_tf.sh --pgver 16,17 --platforms deb --components server --repo staging

# Test all versions on RPM only for lolor and postgis (Docker)
./run_pep_tf.sh --pgver all --platforms rpm --components lolor,postgis

# Test everything with release repo (Docker)
./run_pep_tf.sh --pgver all --platforms all --components all --repo release

# Test patroni on AWS EC2 instances
./run_pep_tf.sh --target aws --pgver 17 --platforms all --components patroni --repo release

# Test repo health on AWS EC2 instances
./run_pep_tf.sh --target aws --pgver 17 --platforms all --components repo_health --repo release
```

## Output

### Test Reports

Test execution reports are saved in the `test-logs/` directory:

- HTML reports with detailed test results
- Consolidated reports organized by timestamp (header lists the run's inputs and the OS / containers in scope)
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
│   ├── test_pep_pgbackrest.py
│   ├── test_pep_postgrest.py
│   ├── test_pep_pgadmin4.py
│   ├── test_pep_ace.py
│   ├── test_pep_ai_db_workbench.py
│   ├── test_pep_ai_kb.py
│   ├── test_pep_system_stats.py
│   ├── test_pep_vectorizer.py
│   ├── test_pep_mcp.py
│   ├── test_pep_rag.py
│   ├── test_pep_repo_health.py
│   ├── test_pep_docloader.py
│   ├── test_pep_anonymizer.py
│   ├── test_pep_pg_vectorize.py
│   ├── test_pep_pg_tokenizer.py
│   ├── test_pep_vchord_bm25.py
│   ├── test_pep_pgaudit.py
│   ├── test_pep_patroni.py
│   ├── test_pep_radar.py
│   ├── test_pep_pg_stat_monitor.py
│   ├── test_pep_pgvector.py
│   ├── test_integration_zerodowntime.py
│   ├── test_pep_llvmjit.py
│   ├── test_spock_patroni_failover.py
│   ├── test_pep_spock.py
│   └── test_pep_supautils.py
├── configuration/           # Environment configuration files
│   ├── config16.env
│   ├── config17.env
│   ├── config18.env
│   ├── config19.env
│   ├── containers_list.json # Docker container registry (enable/disable per container)
│   └── aws_instances.json   # AWS EC2 instance registry (enable/disable per instance)
├── aspects/                 # Test aspects and utilities
│   ├── aws_client.py        # AWSInstanceClient (drop-in Docker replacement)
│   ├── ssh_executor.py      # SSH-based container interface for AWS VMs
│   ├── configure_repository.py  # pgEdge repository configuration helpers
│   ├── container_management.py  # Docker container lifecycle helpers
│   ├── file_management.py       # File copy/verification utilities
│   ├── machine_cleanup.py       # Post-test environment cleanup
│   ├── machine_prereq_setup.py  # OS prerequisite installation
│   ├── package_management.py    # RPM/DEB install, upgrade, uninstall helpers
│   └── pg_server_management.py  # PostgreSQL init, start, stop helpers
├── expected-output/         # Expected test outputs for comparison
├── actual-output/           # Actual test outputs
├── test-logs/               # Test execution reports
├── sql/                     # SQL scripts for testing
├── utillities/              # Helper utilities
├── run_pep_tf.sh            # Main test runner script
└── requirements.txt         # Python dependencies
```

## Supported Platforms

### RPM-based Linux (versions 9 and 10)

| Distribution                        | Version | Architecture  |
|-------------------------------------|---------|---------------|
| Red Hat Enterprise Linux (RHEL)     | 9, 10   | AMD64, ARM64  |
| Alma Linux                          | 9, 10   | AMD64, ARM64  |
| Rocky Linux                         | 9, 10   | AMD64, ARM64  |
| Oracle Enterprise Linux (OEL)       | 9, 10   | AMD64, ARM64  |

### Debian-based Linux

| Distribution | Version                | Architecture  |
|--------------|------------------------|---------------|
| Ubuntu       | 22.04 LTS (Jammy)      | AMD64, ARM64  |
| Ubuntu       | 24.04 LTS (Noble)      | AMD64, ARM64  |
| Ubuntu       | 26.04 LTS              | AMD64, ARM64  |
| Debian       | 11 (Bullseye)          | AMD64, ARM64  |
| Debian       | 12 (Bookworm)          | AMD64, ARM64  |
| Debian       | 13 (Trixie)            | AMD64, ARM64  |

## Supported Components

| Component         | Description                                        |
|-------------------|----------------------------------------------------|
| `server`          | PostgreSQL server and contrib packages             |
| `snowflake`       | Snowflake sequence generator extension             |
| `pgbouncer`       | Connection pooler                                  |
| `pgbackrest`      | Backup and restore tool                            |
| `postgrest`       | RESTful API for PostgreSQL                         |
| `pgadmin4`        | Web-based database management tool                 |
| `lolor`           | Large object logical replication                   |
| `postgis`         | Spatial and geographic objects                     |
| `system_stats`    | System statistics extension                        |
| `vectorizer`      | AI/ML vectorization tools                          |
| `zerodowntime`    | Zero downtime upgrade testing                      |
| `mcp`             | Model Context Protocol components                  |
| `rag`             | RAG server components                              |
| `ace`             | ACE extension tests                                |
| `repo_health`     | Repository health (install and verify all packages)|
| `docloader`       | Document loader utility                            |
| `anonymizer`      | Data anonymization extension                       |
| `pg_vectorize`    | Vectorization extension                            |
| `pg_tokenizer`    | Text tokenization extension                        |
| `vchord_bm25`     | BM25 vector chord search                           |
| `pgaudit`         | Audit logging extension                            |
| `patroni`         | High-availability solution for PostgreSQL          |
| `pg_stat_monitor` | PostgreSQL query performance monitoring extension  |
| `ai_db_workbench` | AI-powered database workbench (server, alerter, collector, client) |
| `radar` | Radar monitoring and observability tool |
| `spock_patroni_failover` | Spock logical replication + Patroni HA failover (n1/n2 Spock cluster with r1 standby) |
| `llvmjit` | LLVM JIT compilation support for PostgreSQL |
| `spock` | Spock 2-node multi-master replication (spock50/spock60 via `SPOCK_MAJOR`, cross-wired with 2node_crosswire.py) |
| `supautils` | Supautils PostgreSQL security/utility preload library (supautils.so) |
| `ai_kb` | AI knowledge-base embedding-model packages (gemini, ollama, openai, voyage) |
| `pgvector` | Vector similarity search extension (`vector`) |
