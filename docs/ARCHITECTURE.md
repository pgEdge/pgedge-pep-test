# PEP Testing Framework - Architecture Documentation

## System Architecture

The PEP (PostgreSQL Extension Package) Testing Framework is a comprehensive automated testing system designed to validate PostgreSQL extension packages across multiple platforms.

---

## High-Level Architecture Diagram

```
╔═══════════════════════════════════════════════════════════════════════════════╗
║                   PEP TESTING FRAMEWORK ARCHITECTURE                          ║
║               PostgreSQL Extension Package Automated Testing                  ║
╚═══════════════════════════════════════════════════════════════════════════════╝


┌─────────────────────────────────────────────────────────────────────────────┐
│                          🎯 USER INTERFACE LAYER                            │
│                                                                             │
│                         ┌─────────────────────┐                             │
│                         │  run_all_envs.sh    │                             │
│                         │  Main Entry Point   │                             │
│                         └──────────┬──────────┘                             │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        🧪 TEST ORCHESTRATION LAYER                          │
│                             (Pytest Framework)                              │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │   Snowflake      │  │     LOLOR        │  │   PgBouncer      │          │
│  │   Component      │  │   Component      │  │   Component      │          │
│  │   Test Suite     │  │   Test Suite     │  │   Test Suite     │   ...    │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘          │
│           │                     │                     │                     │
│           └─────────────────────┼─────────────────────┘                     │
└─────────────────────────────────┼────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      ⚙️  REUSABLE ASPECTS LAYER                             │
│                       (Business Logic Modules)                              │
│                                                                             │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────┐            │
│  │  Prerequisites  │  │   Repository     │  │    Package      │            │
│  │     Setup       │  │  Configuration   │  │   Management    │            │
│  └─────────────────┘  └──────────────────┘  └─────────────────┘            │
│                                                                             │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────┐            │
│  │   PostgreSQL    │  │      File        │  │    Machine      │            │
│  │  Server Mgmt    │  │   Management     │  │    Cleanup      │            │
│  └─────────────────┘  └──────────────────┘  └─────────────────┘            │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       📋 CONFIGURATION LAYER                                │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │    .env      │  │ config16.env │  │   SQL Files  │  │  Expected    │   │
│  │ Environment  │  │ config17.env │  │  Test Cases  │  │   Outputs    │   │
│  │  Variables   │  │ config18.env │  │              │  │              │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    🖥️  TARGET EXECUTION ENVIRONMENTS                        │
│                                                                             │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐       │
│  │  🐳 Docker       │   │  🐳 Docker       │   │  ☁️  AWS EC2     │       │
│  │  RHEL/Rocky/     │   │  Ubuntu/Debian   │   │  Instances       │       │
│  │  AlmaLinux       │   │  Containers      │   │  (Single/Multi)  │       │
│  │  (dnf/rpm)       │   │  (apt/deb)       │   │  (dnf/rpm)       │       │
│  └──────────────────┘   └──────────────────┘   └──────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       📊 OUTPUT & REPORTING LAYER                           │
│                                                                             │
│  ┌──────────────────┐              ┌──────────────────┐                    │
│  │   test-logs/     │              │ actual-output/   │                    │
│  │   • HTML Reports │    ◄─────►   │ • Test Results   │                    │
│  │   • Execution    │              │ • Runtime Data   │                    │
│  │     Logs         │              │                  │                    │
│  └──────────────────┘              └──────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Detailed Component Architecture

### 1. User Interface Layer

**Purpose:** Entry point for test execution and user interaction

**Components:**
- `run_all_envs.sh` - Main shell script orchestrating test execution
- Command-line interface for selecting components and test scenarios

**Responsibilities:**
- Parse user input and configuration
- Orchestrate pytest execution
- Display test progress and results

---

### 2. Test Orchestration Layer

**Purpose:** Define and execute test cases using pytest framework

**Directory:** `component-test/`

**Key Components:**
- `test_pep_snowflake.py` - Snowflake extension test suite
- `test_pep_lolor.py` - LOLOR extension test suite
- `test_pep_pgbouncer.py` - PgBouncer test suite
- `test_pep_component_template.py` - Template for new components

**Test Structure:**
Each test suite follows a standard workflow:
1. Install prerequisites
2. Configure repository
3. Install package
4. Verify package version
5. Validate bundled files
6. Initialize PostgreSQL cluster
7. Start PostgreSQL server
8. Run functional tests
9. Stop server
10. Uninstall package
11. Cleanup environment

**Key Features:**
- Parametrized tests for multiple containers/platforms
- Platform-specific test handling (RHEL vs Debian)
- Comprehensive error handling and reporting

---

### 3. Reusable Aspects Layer

**Purpose:** Provide reusable business logic across all component tests

**Directory:** `aspects/`

**Modules:**

#### `machine_prereq_setup.py`
- Install system prerequisites
- Setup required dependencies
- Configure base environment

#### `configure_repository.py`
- Configure pgEdge repositories (release/staging/daily)
- Auto-detect platform (RHEL/Debian)
- Handle platform-specific repository setup

#### `package_management.py`
- Install/uninstall packages
- Verify package versions
- Validate bundled files
- Support both RPM and DEB packages

#### `pg_server_management.py`
- Initialize PostgreSQL clusters
- Start/stop PostgreSQL servers
- Execute SQL queries
- Check server connections
- Configure GUC parameters

#### `file_management.py`
- File operations and validations
- Path handling
- File content verification

#### `machine_cleanup.py`
- Remove installed packages
- Clean up data directories
- Remove test users
- Comprehensive environment cleanup

---

### 4. Configuration Layer

**Purpose:** Store configuration data for different PostgreSQL versions and components

**Structure:**

#### `configuration/` - PostgreSQL Version Configurations
- `config16.env` - PostgreSQL 16 configuration
- `config17.env` - PostgreSQL 17 configuration
- `config18.env` - PostgreSQL 18 configuration

Contains: version-specific paths, package names, bundled files

#### `config/` - Component Configurations
- Component-specific settings
- Configuration parameters
- Custom settings per component

#### `sql/` - SQL Test Files
- `lolor.sql` - LOLOR functional tests
- `snowflake.sql` - Snowflake functional tests
- Component-specific SQL test cases

#### `.env` - Environment Variables
- Container names (RHEL/Debian)
- Version information
- Paths and ports
- Test parameters

#### `expected-output/` - Expected Test Results
```
expected-output/
├── rpm/
│   ├── lolor          # Expected files for LOLOR (RHEL)
│   ├── snowflake      # Expected files for Snowflake (RHEL)
│   └── ...
└── deb/
    ├── lolor          # Expected files for LOLOR (Debian)
    ├── snowflake      # Expected files for Snowflake (Debian)
    └── ...
```

#### `keys/` - AWS Credentials
- SSH keys for AWS EC2 access
- Authentication tokens
- Access credentials

---

### 5. Target Execution Environments

**Purpose:** Execute tests across multiple platforms

#### Docker Containers - RHEL Family
- Rocky Linux 9, 10
- AlmaLinux 9, 10
- Oracle Enterprise Linux 9, 10
- Package Manager: `dnf` / `rpm`

#### Docker Containers - Debian Family
- Debian 11, 12, 13
- Ubuntu 22.04, 24.04
- Package Manager: `apt` / `dpkg`

#### AWS EC2 Instances
- Single instance testing
- Multi-instance cluster testing
- SSH-based remote execution

**Platform Detection:**
- Automatic detection of package manager
- Platform-specific command execution
- Unified interface across platforms

---

### 6. Output & Reporting Layer

**Purpose:** Store test results and generate reports

#### `test-logs/` - Test Execution Logs
- HTML reports (`deb-report.html`, etc.)
- Execution logs
- Error traces
- Performance metrics

#### `actual-output/` - Runtime Test Results
```
actual-output/
└── sql/
    ├── snowflake/
    │   ├── 16/
    │   │   ├── rpm/
    │   │   └── deb/
    │   ├── 17/
    │   │   ├── rpm/
    │   │   └── deb/
    │   └── 18/
    │       ├── rpm/
    │       └── deb/
    └── lolor/
        └── ...
```

**Features:**
- Timestamped output files
- Platform-specific organization
- Version-specific directories
- Comparison with expected outputs

---

## Data Flow Diagram

```
┌────────────┐    ┌────────────┐    ┌────────────┐
│   INPUT    │───▶│  PROCESS   │───▶│   OUTPUT   │
└────────────┘    └────────────┘    └────────────┘

configuration/    component-test/    test-logs/
config/           aspects/           actual-output/
sql/
keys/             Docker/AWS
expected-output/  Containers

                      │
                      ▼
              ┌───────────────┐
              │  Validation   │
              │   Compare:    │
              │ actual vs     │
              │  expected     │
              └───────────────┘
```

---

## Test Execution Workflow

```
    ┌─────────────────────────────────────────────────────┐
    │              START TEST EXECUTION                    │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  1. Install Prerequisites                           │
    │     • Python, Docker, System packages               │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  2. Configure Repository                            │
    │     • Detect platform (RHEL/Debian)                 │
    │     • Setup pgEdge repository                       │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  3. Install Package                                 │
    │     • Use dnf/rpm (RHEL) or apt/dpkg (Debian)       │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  4. Verify Package Version                          │
    │     • Check installed version matches expected      │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  5. Validate Bundled Files                          │
    │     • Compare installed files with expected         │
    │     • Normalize version-specific paths              │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  6. Initialize PostgreSQL Cluster                   │
    │     • Run initdb with custom GUC parameters         │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  7. Start PostgreSQL Server                         │
    │     • Start server on specified port                │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  8. Check Connection                                │
    │     • Verify server is accessible                   │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  9. Run Functional Tests                            │
    │     • Execute SQL test files                        │
    │     • Validate extension functionality              │
    │     • Compare results with expected output          │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  10. Stop PostgreSQL Server                         │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  11. Uninstall Package                              │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │  12. Cleanup Environment                            │
    │     • Remove packages, data directories, users      │
    └─────────────────────┬───────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────────────────────────────┐
    │              END TEST EXECUTION                      │
    │     • Generate HTML report                          │
    │     • Save logs and outputs                         │
    └─────────────────────────────────────────────────────┘
```

---

## Key Design Principles

### 1. Modularity
- Reusable aspects modules for common operations
- Independent test suites for each component
- Separation of concerns across layers

### 2. Multi-Platform Support
- Auto-detection of platform (RHEL/Debian/AWS)
- Platform-specific command execution
- Unified interface for all platforms

### 3. Template-Based Extension
- `test_pep_component_template.py` for easy component addition
- Standardized test structure
- Copy-and-modify approach

### 4. Comprehensive Testing
- Package-level validation (installation, files, version)
- Functional testing (SQL execution, feature validation)
- Cleanup verification

### 5. Automation
- End-to-end automated workflow
- No manual intervention required
- Parallel execution support

### 6. Traceability
- Detailed logging at each step
- HTML reports with timestamps
- Output comparison with baselines

---

## Adding New Components

To add support for a new PostgreSQL extension:

1. **Copy the template:**
   ```bash
   cp component-test/test_pep_component_template.py \
      component-test/test_pep_[component].py
   ```

2. **Update package names:**
   - RHEL package: `pgedge-[component]_[version]`
   - Debian package: `pgedge-postgresql-[version]-[component]`

3. **Create SQL test file:**
   ```bash
   touch sql/[component].sql
   ```

4. **Create expected output files:**
   ```bash
   touch expected-output/rpm/[component]
   touch expected-output/deb/[component]
   ```

5. **Update .env configuration:**
   ```bash
   export COMPONENTS=pgedge-[component]_18
   export [COMPONENT]_VERSION=x.y.z
   ```

6. **Run tests:**
   ```bash
   pytest component-test/test_pep_[component].py -v
   ```

---

## Technology Stack

- **Testing Framework:** pytest
- **Container Runtime:** Docker
- **Cloud Platform:** AWS EC2
- **Programming Language:** Python 3.x
- **Shell Scripting:** Bash
- **Configuration:** dotenv
- **Reporting:** pytest-html

---

## Security Considerations

1. **Credential Management:**
   - AWS keys stored in `keys/` directory
   - Not committed to version control
   - Secure SSH access for remote instances

2. **Container Isolation:**
   - Tests run in isolated Docker containers
   - No impact on host system
   - Clean state for each test run

3. **Package Verification:**
   - Version validation
   - File integrity checks
   - Expected output comparison

---

## Performance Optimization

1. **Parallel Execution:**
   - Multiple containers tested simultaneously
   - Pytest parallel execution support

2. **Caching:**
   - Docker image caching
   - Package cache for faster installations

3. **Incremental Testing:**
   - Run specific test functions
   - Skip unnecessary steps
   - Platform-specific test selection

---

## Future Enhancements

1. **CI/CD Integration:**
   - GitHub Actions workflows
   - Automated PR testing
   - Nightly regression tests

2. **Extended Platform Support:**
   - Additional Linux distributions
   - Windows support (via WSL)
   - macOS testing

3. **Enhanced Reporting:**
   - Real-time test dashboards
   - Historical trend analysis
   - Performance metrics tracking

4. **Test Coverage:**
   - More extension components
   - Edge case testing
   - Stress testing

---

## Conclusion

The PEP Testing Framework provides a robust, scalable, and maintainable solution for testing PostgreSQL extension packages across multiple platforms. Its modular architecture enables easy extension while maintaining consistency and reliability in test execution.
