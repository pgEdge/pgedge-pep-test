#!/bin/bash

# Version of this test runner
VERSION="1.1.0"

# Directory containing your env files
ENV_DIR="./configuration"

# ============================================================================
# CLI argument parsing
# ============================================================================
CLI_MODE=false
PGVER=""
PLATFORMS=""
COMPONENTS=""
REPO_OVERRIDE=""
SPOCK_OVERRIDE=""
TARGET="docker"   # default: local Docker containers
CONTAINERS_OVERRIDE=""
LIST_CONTAINERS=false
ARCH=""
DRY_RUN="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pgver)
      PGVER="$2"
      CLI_MODE=true
      shift 2
      ;;
    --platforms)
      PLATFORMS="$2"
      CLI_MODE=true
      shift 2
      ;;
    --components)
      COMPONENTS="$2"
      CLI_MODE=true
      shift 2
      ;;
    --repo)
      REPO_OVERRIDE="$2"
      CLI_MODE=true
      shift 2
      ;;
    --spock)
      SPOCK_OVERRIDE="$2"
      CLI_MODE=true
      shift 2
      ;;
    --target)
      TARGET="$2"
      CLI_MODE=true
      shift 2
      ;;
    --containers)
      CONTAINERS_OVERRIDE="$2"
      CLI_MODE=true
      shift 2
      ;;
    --list-containers)
      LIST_CONTAINERS=true
      CLI_MODE=true
      shift 1
      ;;
    --arch)
      ARCH="$2"
      CLI_MODE=true
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      CLI_MODE=true
      shift 1
      ;;
    --version|-V)
      echo "$(basename "$0") version ${VERSION}"
      exit 0
      ;;
    --help|-h)
      cat <<HELPTEXT
Usage: $(basename "$0") [OPTIONS]

Run pgEdge component tests across environments, platforms, and components.
When no options are provided, the script runs in interactive menu mode.

OPTIONS:
  --pgver <versions>      PostgreSQL versions to test (default: all)
                          Values: 16, 17, 18, all
                          Comma-separated for multiple: 16,17

  --platforms <platforms>  Target platforms (default: all)
                          Values: rpm, deb, all
                          Comma-separated for multiple: rpm,deb

  --components <names>    Components to test (default: all)
                          Values: server, snowflake, pgbouncer, pgbackrest, postgrest, lolor, postgis,
                                  system_stats, vectorizer, zerodowntime, mcp, rag, ace, repo_health,
                                  docloader, anonymizer, pg_vectorize, pg_tokenizer, vchord_bm25, pgaudit, pgadmin4, patroni, pg_stat_monitor, ai_db_workbench, radar, spock_patroni_failover, llvmjit, ai_kb, spock, supautils, pgvector, all
                          Comma-separated for multiple: lolor,postgis

  --repo <repository>     Repository to use (default: staging)
                          Values: release, staging, daily

  --spock <major>         Spock major version to install/verify (default: from config, 50)
                          Values: 50, 60
                          Overrides SPOCK_MAJOR from the config env file for this run.

  --target <target>       Execution target (default: docker)
                          Values: docker, aws
                          docker: run against local Docker containers (containers_list.json)
                          aws:    run against live AWS EC2 instances (aws_instances.json)
                          Key files for AWS must exist under keys/ (gitignored).

  --containers <csv>      Runtime container override (default: use containers_list.json
                          enabled:true subset). Accepts aliases (e.g. rocky9-arm64) and
                          canonical names. A listed platform's opposite architecture is
                          also accepted even if only one arch is listed (e.g. request
                          ubuntu2404-amd64 when only ubuntu2404-arm64 is in the catalog);
                          the counterpart is synthesized on the fly and maps to the same
                          image. Special value 'all' (sole token) = entire catalog
                          regardless of enabled, but 'all' is CATALOG-ONLY and does NOT
                          include these implicit counterparts. See --list-containers.

  --list-containers       Print the catalog (alias, canonical name, family, arch,
                          enabled, description) and exit. Note: any listed platform's
                          opposite arch is also selectable via --containers even though
                          only the listed arch appears here.

  --arch <arch>           Filter enabled containers by architecture (default: no filter)
                          Values: arm64, amd64
                          Filters by container-name suffix: -arm or -amd
                          Used by the GitHub Actions workflow to scope matrix targets per arch.

  --dry-run               Resolve containers and print what would run, then exit
                          (no pytest, no Docker pulls, no package installs, no repo setup)

  --help, -h              Show this help message and exit

  --version, -V           Show the test runner version and exit

EXAMPLES:
  # Interactive mode (no arguments)
  ./$(basename "$0")

  # Test PG 16 server on RPM with staging repo
  ./$(basename "$0") --pgver 16 --platforms rpm --components server

  # Test all versions, DEB only, lolor and postgis
  ./$(basename "$0") --pgver all --platforms deb --components lolor,postgis

  # Test PG 17 RPM ARM64 only
  ./$(basename "$0") --pgver 17 --platforms rpm --arch arm64 --components server

  # Show resolved containers without launching tests (no Docker required)
  ./$(basename "$0") --pgver 17 --platforms deb --arch arm64 --components pgbouncer --dry-run

  # Test everything with release repo
  ./$(basename "$0") --pgver all --platforms all --components all --repo release

  # Test zerodowntime on PG 18 RPM against spock50 from the daily repo
  ./$(basename "$0") --pgver 18 --platforms rpm --components zerodowntime --repo daily --spock 50
HELPTEXT
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Validate --arch (empty == absent == no filter)
if [[ -n "$ARCH" ]]; then
  case "$ARCH" in
    arm64|amd64) ;;
    *)
      echo "[arch-filter] ERROR: --arch must be 'arm64' or 'amd64' (got '$ARCH')"
      exit 2
      ;;
  esac
fi
export PEP_ARCH_FILTER="$ARCH"

# Discovery shortcut: print the catalog and exit.
if [[ "$LIST_CONTAINERS" == "true" ]]; then
  python3 utillities/container_resolver.py list-containers
  exit $?
fi

# --containers / PEP_CONTAINERS is docker-only. Reject the combination with
# --target aws loudly rather than silently ignoring the override.
if [[ "$TARGET" == "aws" ]] && { [[ -n "$CONTAINERS_OVERRIDE" ]] || [[ -n "${PEP_CONTAINERS:-}" ]]; }; then
  echo "[container-override] ERROR: --containers / PEP_CONTAINERS override is only" >&2
  echo "                            supported with --target docker (got --target aws)." >&2
  echo "                            Edit configuration/aws_instances.json directly for AWS runs." >&2
  exit 2
fi

mkdir -p test-logs

# Generate timestamp for this test run
RUN_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CONSOLIDATED_REPORT_DIR="test-logs/consolidated-${RUN_TIMESTAMP}"
mkdir -p "$CONSOLIDATED_REPORT_DIR"

# Array to store all individual report paths
declare -a ALL_REPORTS=()
declare -a ALL_JUNIT_XMLS=()

if [[ "$CLI_MODE" == true ]]; then
  # CLI mode: use argument values with defaults
  env_choice="${PGVER:-all}"
  platform_choice="${PLATFORMS:-all}"
  test_type_choice="${COMPONENTS:-all}"
  echo "Running in CLI mode"
else
  # Interactive mode: prompt user for choices
  # Prompt user for environment choice
  echo "Select environment(s) to run:"
  echo "1) 16"
  echo "2) 17"
  echo "3) 18"
  echo "4) All"
  echo ""
  echo "💡 You can specify multiple environments separated by commas"
  echo "   Example: 16,17"
  read -p "Enter your choice: " env_choice

  # Prompt user for platform choice
  echo ""
  echo "Select platform(s) to run:"
  echo "1) RPM"
  echo "2) DEB"
  echo "3) All"
  echo ""
  echo "💡 You can specify multiple platforms separated by commas"
  echo "   Example: RPM,DEB"
  read -p "Enter your choice: " platform_choice
  echo ""

  # Prompt user for test type choice
  echo "Select test type(s) to run:"
  echo "1) server - PostgreSQL server tests"
  echo "2) snowflake - Snowflake extension tests"
  echo "3) pgbouncer - PgBouncer tests"
  echo "4) pgbackrest - pgBackRest tests"
  echo "5) postgrest - PostgREST tests"
  echo "6) lolor - LOLOR tests"
  echo "7) postgis - PostGIS tests"
  echo "8) system_stats - System Stats tests"
  echo "9) vectorizer - Vectorizer tests"
  echo "10) zerodowntime - Zero Downtime Integration tests"
  echo "11) mcp - MCP (postgres-mcp, nla-cli, nla-web) tests"
  echo "12) rag - RAG server tests"
  echo "13) ace - ACE tests"
  echo "14) repo_health - Repository health tests (install all packages)"
  echo "15) docloader - Docloader tests"
  echo "16) anonymizer - Anonymizer tests"
  echo "17) pg_vectorize - pg-vectorize extension tests"
  echo "18) pg_tokenizer - pg-tokenizer extension tests"
  echo "19) vchord_bm25 - vchord-bm25 extension tests"
  echo "20) pgaudit - pgaudit extension tests"
  echo "21) pgadmin4 - pgAdmin4 tests"
  echo "22) patroni - Patroni HA tests"
  echo "23) pg_stat_monitor - Pg Stat Monitor tests"
  echo "24) ai_db_workbench - AI DB Workbench tests"
  echo "25) radar - Radar tests"
  echo "26) spock_patroni_failover - Spock + Patroni HA failover tests"
  echo "27) llvmjit - LLVM JIT tests"
  echo "28) spock - Spock 2-node replication tests (spock50/spock60)"
  echo "29) supautils - Supautils tests"
  echo "30) ai_kb - AI KB tests"
  echo "31) pgvector - Pgvector extension tests"
  echo "32) all - All tests"
  echo ""
  echo "💡 You can specify multiple components separated by commas"
  echo "   Example: lolor,postgis,system_stats"
  read -p "Enter your choice: " test_type_choice
fi

# Determine environments to run
if [[ "$env_choice" == "all" || "$env_choice" == "All" ]]; then
  env_list=(16 17 18)
else
  # Split by comma and trim whitespace
  IFS=',' read -ra env_list <<< "$env_choice"
  # Trim whitespace from each element
  for i in "${!env_list[@]}"; do
    env_list[$i]=$(echo "${env_list[$i]}" | xargs)
  done
fi

# Determine platforms to run
if [[ "$platform_choice" == "all" || "$platform_choice" == "All" ]]; then
  platform_list=(RPM DEB)
else
  # Split by comma and trim whitespace
  IFS=',' read -ra platform_list <<< "$platform_choice"
  # Trim whitespace from each element
  for i in "${!platform_list[@]}"; do
    platform_list[$i]=$(echo "${platform_list[$i]}" | xargs)
  done
fi

# Determine test types to run
if [[ "$test_type_choice" == "all" || "$test_type_choice" == "All" ]]; then
  test_type_list=(server snowflake pgbouncer pgbackrest postgrest lolor postgis system_stats vectorizer zerodowntime mcp rag ace repo_health docloader anonymizer pg_vectorize pg_tokenizer vchord_bm25 pgaudit pgadmin4 patroni pg_stat_monitor ai_db_workbench radar spock_patroni_failover llvmjit ai_kb spock supautils pgvector)
else
  # Split by comma and trim whitespace
  IFS=',' read -ra test_type_list <<< "$test_type_choice"
  # Trim whitespace from each element
  for i in "${!test_type_list[@]}"; do
    test_type_list[$i]=$(echo "${test_type_list[$i]}" | xargs)
  done
fi

# Display selected configuration
echo ""
echo "=========================================="
echo "📋 Test Configuration Summary"
echo "=========================================="
echo "Environments: ${env_list[*]}"
echo "Platforms: ${platform_list[*]}"
echo "Test types: ${test_type_list[*]}"
echo "Target: ${TARGET}"
if [[ -n "$REPO_OVERRIDE" ]]; then
  echo "Repo override: $REPO_OVERRIDE"
fi
echo "=========================================="
echo ""

# Function to run pytest and track results
run_pytest_with_tracking() {
  local test_file=$1
  local env=$2
  local platform=$3
  local component=$4

  # Create component-specific directory structure: test-logs/{component}/{pg_version}/
  local component_dir="test-logs/${component}/${env}"
  mkdir -p "$component_dir"

  local report_name="report-${platform}-${component}-${env}"

  # Store individual report in component folder
  local component_html_report="${component_dir}/${report_name}.html"
  local component_junit_xml="${component_dir}/${report_name}.xml"

  # Also store in consolidated folder for consolidated report
  local consolidated_html_report="${CONSOLIDATED_REPORT_DIR}/${report_name}.html"
  local consolidated_junit_xml="${CONSOLIDATED_REPORT_DIR}/${report_name}.xml"

  echo "▶️  Running ${platform} ${component} tests for env ${env}"
  echo "   📁 Component report will be stored in: ${component_dir}"

  # Check if we should include logs for passed tests
  local pytest_opts="-v"

  if [[ "${REQUIRE_PASS_TEST_LOGS}" == "true" ]]; then
    # Include detailed logs for all tests (passed, failed, skipped)
    # -s: disable output capturing (show print statements)
    # --capture=no: don't capture stdout/stderr
    pytest_opts="-v -s --capture=no"
    echo "   📝 Capturing logs for passed tests (REQUIRE_PASS_TEST_LOGS=true)"
  else
    # Only show output for failed tests (default behavior)
    pytest_opts="-v -s"
  fi

  # Run pytest with both HTML and JUnit XML output
  # Store in component-specific directory
  # Use || true to continue even if tests fail
  pytest $pytest_opts "$test_file" \
    --html="$component_html_report" \
    --self-contained-html \
    --junit-xml="$component_junit_xml" || true

  # Copy reports to consolidated directory for consolidated report generation
  cp "$component_html_report" "$consolidated_html_report" 2>/dev/null || true
  cp "$component_junit_xml" "$consolidated_junit_xml" 2>/dev/null || true

  # Store report paths for consolidation
  ALL_REPORTS+=("$report_name.html")
  ALL_JUNIT_XMLS+=("$consolidated_junit_xml")

  echo "   ✅ Reports saved:"
  echo "      - Component: ${component_html_report}"
  echo "      - Consolidated: ${consolidated_html_report}"
}

# v2.2: Once-per-invocation override validation. Runs before the per-env
# loop because the override and the user's --platforms/--arch scope don't
# change between envs — there's no value in re-validating per env, and the
# resolver's stderr (source/requested lines) would otherwise duplicate.
#
# Skipped for --target aws (the override is docker-only; the aws+override
# fail-fast above already handled the misuse case). For docker, this runs
# whether or not the user supplied an override: the resolver's default path
# is a no-op (no chatter, no failure) so it's cheap.
#
# Also skipped when PEP_GLOBAL_VALIDATED is set. In CI, the workflow's plan
# job already runs validate-global with the USER'S FULL scope (families=all,
# arches=all if so chosen). Each per-target test-job invocation arrives here
# with a narrowed --platforms/--arch — running validate-global again at that
# narrow scope would falsely global-zero whenever the user's override has
# entries outside the current cell (e.g. arm64 entries on an amd64 target).
# The plan job sets PEP_GLOBAL_VALIDATED=true to signal "already done
# globally; per-target work only".
if [[ "$TARGET" == "docker" && -z "${PEP_GLOBAL_VALIDATED:-}" ]]; then
  # Scope strings in user-facing names (rpm/deb, arm64/amd64). platform_list
  # uses uppercase RPM/DEB; lowercase + comma-join for the resolver.
  _scope_families="$(echo "${platform_list[*]}" | tr '[:upper:]' '[:lower:]' | tr ' ' ',')"
  if [[ -z "$_scope_families" || "$_scope_families" == "all" ]]; then
    _scope_families="rpm,deb"
  fi
  _scope_arches="${ARCH:-arm64,amd64}"
  [[ "$_scope_arches" == "all" ]] && _scope_arches="arm64,amd64"

  if ! python3 utillities/container_resolver.py validate-global \
        --containers "$CONTAINERS_OVERRIDE" \
        --scope-families "$_scope_families" \
        --scope-arches "$_scope_arches" >/dev/null; then
    echo "[container-override] ERROR: validate-global failed (see message above)" >&2
    exit 2
  fi
  unset _scope_families _scope_arches
fi

# Run tests for each combination
for env in "${env_list[@]}"; do
  envfile="${ENV_DIR}/config${env}.env"

  if [[ ! -f "$envfile" ]]; then
    echo "⚠️  Skipping missing environment file: $envfile"
    continue
  fi

  echo "🔹 Running tests for environment: ${envfile}"

  # Export environment variables
  set -a
  source "$envfile"
  set +a

  # Apply repo override BEFORE the JSON loader and --dry-run block so that the
  # dry-run output reflects the effective REPO (not the envfile's default).
  if [[ -n "$REPO_OVERRIDE" ]]; then
    export REPO="$REPO_OVERRIDE"
    echo "   Overriding REPO to: $REPO_OVERRIDE"
  fi

  # Load target instances — Docker containers or AWS EC2 instances
  if [[ "$TARGET" == "aws" ]]; then
    # ── AWS mode ────────────────────────────────────────────────────────────
    AWS_INSTANCES_JSON="${ENV_DIR}/aws_instances.json"
    if [[ ! -f "$AWS_INSTANCES_JSON" ]]; then
      echo "❌ aws_instances.json not found at ${AWS_INSTANCES_JSON}"
      exit 1
    fi
    _loaded_containers=$(python3 -c "
import json, sys
try:
    d = json.load(open('$AWS_INSTANCES_JSON'))
    print(','.join(c['name'] for c in d.get('rhel', []) if c.get('enabled')))
except Exception as e:
    sys.stderr.write(f'Warning: failed to parse aws_instances.json: {e}\n')
    print('')
")
    _loaded_deb_containers=$(python3 -c "
import json, sys
try:
    d = json.load(open('$AWS_INSTANCES_JSON'))
    print(','.join(c['name'] for c in d.get('deb', []) if c.get('enabled')))
except Exception as e:
    sys.stderr.write(f'Warning: failed to parse aws_instances.json: {e}\n')
    print('')
")
    [[ -n "$_loaded_containers" ]] && export CONTAINERS="$_loaded_containers"
    [[ -n "$_loaded_deb_containers" ]] && export DEB_CONTAINERS="$_loaded_deb_containers"
    export AWS_MODE=true
    echo "   🌐 AWS mode: CONTAINERS=${CONTAINERS}  DEB_CONTAINERS=${DEB_CONTAINERS}"
    unset _loaded_containers _loaded_deb_containers
  else
    # Load containers from containers_list.json (overrides empty CONTAINERS/DEB_CONTAINERS from env file)
    CONTAINERS_JSON="${ENV_DIR}/containers_list.json"
    if [[ -f "$CONTAINERS_JSON" ]]; then
      _arch_filter="${PEP_ARCH_FILTER:-}"
      _platforms_for_resolution="$(echo "${platform_list[*]}" | tr '[:upper:]' '[:lower:]')"

      # Whether each family is in the user-selected --platforms scope. Used
      # below to suppress the legacy [container-resolution] log AND the
      # resolver's [container-override] chatter for out-of-scope families,
      # matching pre-v2.2 logging behavior.
      _rpm_in_scope=false
      _deb_in_scope=false
      [[ " ${_platforms_for_resolution} " == *" rpm "* || " ${_platforms_for_resolution} " == *" all "* ]] && _rpm_in_scope=true
      [[ " ${_platforms_for_resolution} " == *" deb "* || " ${_platforms_for_resolution} " == *" all "* ]] && _deb_in_scope=true

      _arch_label="${_arch_filter:-<none>}"

      # ── rpm-side resolution via container_resolver.py ─────────────────────
      _rpm_err=$(mktemp)
      _loaded_containers=$(python3 utillities/container_resolver.py resolve-for-target \
            --containers "$CONTAINERS_OVERRIDE" \
            --target-family rpm \
            --target-arch "$_arch_filter" 2>"$_rpm_err")
      _rpm_exit=$?
      if [[ $_rpm_exit -ne 0 ]]; then
        cat "$_rpm_err" >&2
        rm -f "$_rpm_err"
        echo "[container-override] ERROR: rpm-side resolver failed (see above)" >&2
        exit 2
      fi
      if [[ "$_rpm_in_scope" == "true" ]]; then
        # Forward the resolver's [container-override] stderr (already empty on
        # default path; populated only on override path).
        cat "$_rpm_err" >&2
        # Emit the legacy [container-resolution] line in its original shape.
        if [[ -n "$_loaded_containers" ]]; then
          _rpm_count=$(echo "$_loaded_containers" | awk -F',' '{print NF}')
          echo "[container-resolution] platforms=rpm arch=$_arch_label -> $_rpm_count container(s): ${_loaded_containers//,/, }" >&2
        else
          echo "[container-resolution] platforms=rpm arch=$_arch_label -> 0 container(s): (none)" >&2
        fi
      fi
      rm -f "$_rpm_err"

      # ── deb-side resolution (symmetric) ─────────────────────────────────
      _deb_err=$(mktemp)
      _loaded_deb_containers=$(python3 utillities/container_resolver.py resolve-for-target \
            --containers "$CONTAINERS_OVERRIDE" \
            --target-family deb \
            --target-arch "$_arch_filter" 2>"$_deb_err")
      _deb_exit=$?
      if [[ $_deb_exit -ne 0 ]]; then
        cat "$_deb_err" >&2
        rm -f "$_deb_err"
        echo "[container-override] ERROR: deb-side resolver failed (see above)" >&2
        exit 2
      fi
      if [[ "$_deb_in_scope" == "true" ]]; then
        cat "$_deb_err" >&2
        if [[ -n "$_loaded_deb_containers" ]]; then
          _deb_count=$(echo "$_loaded_deb_containers" | awk -F',' '{print NF}')
          echo "[container-resolution] platforms=deb arch=$_arch_label -> $_deb_count container(s): ${_loaded_deb_containers//,/, }" >&2
        else
          echo "[container-resolution] platforms=deb arch=$_arch_label -> 0 container(s): (none)" >&2
        fi
      fi
      rm -f "$_deb_err"

      [[ -n "$_loaded_containers" ]] && export CONTAINERS="$_loaded_containers"
      [[ -n "$_loaded_deb_containers" ]] && export DEB_CONTAINERS="$_loaded_deb_containers"
      unset _loaded_containers _loaded_deb_containers _arch_filter \
            _platforms_for_resolution _arch_label _rpm_in_scope _deb_in_scope \
            _rpm_count _deb_count _rpm_exit _deb_exit
    fi
    export AWS_MODE=false
  fi

  # --dry-run: resolve containers, print platform-scoped image/platform inference, exit env iteration.
  # Side-effect-free: no pytest, no Docker pull/create/start, no package install, no repo setup.
  # Image/platform map intentionally inlined (mirrors aspects/container_management.py CONTAINER_IMAGES
  # and the suffix-detection in ensure_container_running) so --dry-run does not require the `docker`
  # Python package. Keep these in sync; verified during periodic cleanup.
  if [[ "$DRY_RUN" == "true" ]]; then
    _platforms_lc="$(echo "${platform_list[*]}" | tr '[:upper:]' '[:lower:]')"
    echo "[dry-run] env=${env} platforms=${_platforms_lc} arch=${PEP_ARCH_FILTER:-<none>} repo=${REPO:-}"

    case " $_platforms_lc " in
      *" rpm "*|*" all "*) echo "[dry-run] CONTAINERS (rpm)=${CONTAINERS:-}" ;;
    esac
    case " $_platforms_lc " in
      *" deb "*|*" all "*) echo "[dry-run] DEB_CONTAINERS (deb)=${DEB_CONTAINERS:-}" ;;
    esac

    PEP_DRY_RUN_PLATFORMS="$_platforms_lc" \
    PEP_DRY_RUN_RHEL="${CONTAINERS:-}" \
    PEP_DRY_RUN_DEB="${DEB_CONTAINERS:-}" \
    python3 -c "
import os
plats = os.environ.get('PEP_DRY_RUN_PLATFORMS', '').lower().split()
include_rpm = ('rpm' in plats) or ('all' in plats)
include_deb = ('deb' in plats) or ('all' in plats)
rhel = [n.strip() for n in os.environ.get('PEP_DRY_RUN_RHEL', '').split(',') if n.strip()]
deb  = [n.strip() for n in os.environ.get('PEP_DRY_RUN_DEB',  '').split(',') if n.strip()]
selected = []
if include_rpm:
    selected += [('rpm', n) for n in rhel]
if include_deb:
    selected += [('deb', n) for n in deb]
IMAGES = {
    'rocky9': 'rockylinux:9', 'rocky10': 'rockylinux:10', 'rocky8': 'rockylinux:8',
    'alma9': 'almalinux:9',  'alma10': 'almalinux:10',  'alma8': 'almalinux:8',
    'oel9':  'oraclelinux:9', 'oel10': 'oraclelinux:10', 'oel8': 'oraclelinux:8',
    'debian11': 'debian:11', 'debian12': 'debian:12', 'debian13': 'debian:13',
    'ubuntu2204': 'ubuntu:22.04', 'ubuntu2404': 'ubuntu:24.04', 'ubuntu2604': 'ubuntu:26.04',
}
def infer(name):
    n = name.lower()
    image = next((img for k, img in IMAGES.items() if k in n), '<unknown>')
    if '-arm' in n or 'arm64' in n:
        platform = 'linux/arm64'
    elif '-amd' in n or 'amd64' in n or 'x86' in n:
        platform = 'linux/amd64'
    else:
        platform = '<unknown>'
    return image, platform
if not selected:
    print('[image-resolution] (no containers in scope for this target)')
for fam, name in selected:
    img, plat = infer(name)
    print(f'[image-resolution] family={fam} container={name} image={img} platform={plat}')
"

    echo "[dry-run] skipping test execution (no pytest, no Docker, no package install, no repo setup)"
    unset _platforms_lc
    continue
  fi

  # Apply spock major version override if specified via CLI
  if [[ -n "$SPOCK_OVERRIDE" ]]; then
    export SPOCK_MAJOR="$SPOCK_OVERRIDE"
    echo "   Overriding SPOCK_MAJOR to: $SPOCK_OVERRIDE"
  fi

  for platform in "${platform_list[@]}"; do
    for test_type in "${test_type_list[@]}"; do
      case "$platform" in
        RPM|rpm)
          export PLATFORM_FILTER=rpm
          case "$test_type" in
            server)
              run_pytest_with_tracking "component-test/test_pep_server.py" "$env" "rpm" "server"
              ;;
            snowflake)
              run_pytest_with_tracking "component-test/test_pep_snowflake.py" "$env" "rpm" "snowflake"
              ;;
            lolor)
              run_pytest_with_tracking "component-test/test_pep_lolor.py" "$env" "rpm" "lolor"
              ;;
            pgbouncer)
              run_pytest_with_tracking "component-test/test_pep_pgbouncer.py" "$env" "rpm" "pgbouncer"
              ;;
            pgbackrest)
              run_pytest_with_tracking "component-test/test_pep_pgbackrest.py" "$env" "rpm" "pgbackrest"
              ;;
            postgrest)
              run_pytest_with_tracking "component-test/test_pep_postgrest.py" "$env" "rpm" "postgrest"
              ;;
            postgis)
              run_pytest_with_tracking "component-test/test_pep_postgis.py" "$env" "rpm" "postgis"
              ;;
            system_stats)
              run_pytest_with_tracking "component-test/test_pep_system_stats.py" "$env" "rpm" "system_stats"
              ;;
            vectorizer)
              run_pytest_with_tracking "component-test/test_pep_vectorizer.py" "$env" "rpm" "vectorizer"
              ;;
            zerodowntime)
              run_pytest_with_tracking "component-test/test_integration_zerodowntime.py" "$env" "rpm" "zerodowntime"
              ;;
            mcp)
              run_pytest_with_tracking "component-test/test_pep_mcp.py" "$env" "rpm" "mcp"
              ;;
            rag)
              run_pytest_with_tracking "component-test/test_pep_rag.py" "$env" "rpm" "rag"
              ;;
            ace)
              run_pytest_with_tracking "component-test/test_pep_ace.py" "$env" "rpm" "ace"
              ;;
            repo_health)
              run_pytest_with_tracking "component-test/test_pep_repo_health.py" "$env" "rpm" "repo_health"
              ;;
            docloader)
              run_pytest_with_tracking "component-test/test_pep_docloader.py" "$env" "rpm" "docloader"
              ;;
            anonymizer)
              run_pytest_with_tracking "component-test/test_pep_anonymizer.py" "$env" "rpm" "anonymizer"
              ;;
            pg_vectorize)
              run_pytest_with_tracking "component-test/test_pep_pg_vectorize.py" "$env" "rpm" "pg_vectorize"
              ;;
            pg_tokenizer)
              run_pytest_with_tracking "component-test/test_pep_pg_tokenizer.py" "$env" "rpm" "pg_tokenizer"
              ;;
            vchord_bm25)
              run_pytest_with_tracking "component-test/test_pep_vchord_bm25.py" "$env" "rpm" "vchord_bm25"
              ;;
            pgaudit)
              run_pytest_with_tracking "component-test/test_pep_pgaudit.py" "$env" "rpm" "pgaudit"
              ;;
            pgadmin4)
              run_pytest_with_tracking "component-test/test_pep_pgadmin4.py" "$env" "rpm" "pgadmin4"
              ;;
            patroni)
              run_pytest_with_tracking "component-test/test_pep_patroni.py" "$env" "rpm" "patroni"
              ;;
            pg_stat_monitor)
              run_pytest_with_tracking "component-test/test_pep_pg_stat_monitor.py" "$env" "rpm" "pg_stat_monitor"
              ;;
            ai_db_workbench)
              run_pytest_with_tracking "component-test/test_pep_ai_db_workbench.py" "$env" "rpm" "ai_db_workbench"
              ;;
            radar)
              run_pytest_with_tracking "component-test/test_pep_radar.py" "$env" "rpm" "radar"
              ;;
            spock_patroni_failover)
              run_pytest_with_tracking "component-test/test_spock_patroni_failover.py" "$env" "rpm" "spock_patroni_failover"
              ;;
            llvmjit)
              run_pytest_with_tracking "component-test/test_pep_llvmjit.py" "$env" "rpm" "llvmjit"
              ;;
            spock)
              run_pytest_with_tracking "component-test/test_pep_spock.py" "$env" "rpm" "spock"
              ;;
            supautils)
              run_pytest_with_tracking "component-test/test_pep_supautils.py" "$env" "rpm" "supautils"
              ;;
            ai_kb)
              run_pytest_with_tracking "component-test/test_pep_ai_kb.py" "$env" "rpm" "ai_kb"
              ;;
            pgvector)
              run_pytest_with_tracking "component-test/test_pep_pgvector.py" "$env" "rpm" "pgvector"
              ;;
            *)
              echo "⚠️ Unknown test type: $test_type"
              ;;
          esac
          ;;
        DEB|deb)
          export PLATFORM_FILTER=deb
          case "$test_type" in
            server)
              run_pytest_with_tracking "component-test/test_pep_server.py" "$env" "deb" "server"
              ;;
            snowflake)
              run_pytest_with_tracking "component-test/test_pep_snowflake.py" "$env" "deb" "snowflake"
              ;;
            lolor)
              run_pytest_with_tracking "component-test/test_pep_lolor.py" "$env" "deb" "lolor"
              ;;
            pgbouncer)
              run_pytest_with_tracking "component-test/test_pep_pgbouncer.py" "$env" "deb" "pgbouncer"
              ;;
            pgbackrest)
              run_pytest_with_tracking "component-test/test_pep_pgbackrest.py" "$env" "deb" "pgbackrest"
              ;;
            postgrest)
              run_pytest_with_tracking "component-test/test_pep_postgrest.py" "$env" "deb" "postgrest"
              ;;
            postgis)
              run_pytest_with_tracking "component-test/test_pep_postgis.py" "$env" "deb" "postgis"
              ;;
            system_stats)
              run_pytest_with_tracking "component-test/test_pep_system_stats.py" "$env" "deb" "system_stats"
              ;;
            vectorizer)
              run_pytest_with_tracking "component-test/test_pep_vectorizer.py" "$env" "deb" "vectorizer"
              ;;
            zerodowntime)
              run_pytest_with_tracking "component-test/test_integration_zerodowntime.py" "$env" "deb" "zerodowntime"
              ;;
            mcp)
              run_pytest_with_tracking "component-test/test_pep_mcp.py" "$env" "deb" "mcp"
              ;;
            rag)
              run_pytest_with_tracking "component-test/test_pep_rag.py" "$env" "deb" "rag"
              ;;
            ace)
              run_pytest_with_tracking "component-test/test_pep_ace.py" "$env" "deb" "ace"
              ;;
            repo_health)
              run_pytest_with_tracking "component-test/test_pep_repo_health.py" "$env" "deb" "repo_health"
              ;;
            docloader)
              run_pytest_with_tracking "component-test/test_pep_docloader.py" "$env" "deb" "docloader"
              ;;
            anonymizer)
              run_pytest_with_tracking "component-test/test_pep_anonymizer.py" "$env" "deb" "anonymizer"
              ;;
            pg_vectorize)
              run_pytest_with_tracking "component-test/test_pep_pg_vectorize.py" "$env" "deb" "pg_vectorize"
              ;;
            pg_tokenizer)
              run_pytest_with_tracking "component-test/test_pep_pg_tokenizer.py" "$env" "deb" "pg_tokenizer"
              ;;
            vchord_bm25)
              run_pytest_with_tracking "component-test/test_pep_vchord_bm25.py" "$env" "deb" "vchord_bm25"
              ;;
            pgaudit)
              run_pytest_with_tracking "component-test/test_pep_pgaudit.py" "$env" "deb" "pgaudit"
              ;;
            pgadmin4)
              run_pytest_with_tracking "component-test/test_pep_pgadmin4.py" "$env" "deb" "pgadmin4"
              ;;
            patroni)
              run_pytest_with_tracking "component-test/test_pep_patroni.py" "$env" "deb" "patroni"
              ;;
            pg_stat_monitor)
              run_pytest_with_tracking "component-test/test_pep_pg_stat_monitor.py" "$env" "deb" "pg_stat_monitor"
              ;;
            ai_db_workbench)
              run_pytest_with_tracking "component-test/test_pep_ai_db_workbench.py" "$env" "deb" "ai_db_workbench"
              ;;
            radar)
              run_pytest_with_tracking "component-test/test_pep_radar.py" "$env" "deb" "radar"
              ;;
            spock_patroni_failover)
              run_pytest_with_tracking "component-test/test_spock_patroni_failover.py" "$env" "deb" "spock_patroni_failover"
              ;;
            llvmjit)
              run_pytest_with_tracking "component-test/test_pep_llvmjit.py" "$env" "deb" "llvmjit"
              ;;
            spock)
              run_pytest_with_tracking "component-test/test_pep_spock.py" "$env" "deb" "spock"
              ;;
            supautils)
              run_pytest_with_tracking "component-test/test_pep_supautils.py" "$env" "deb" "supautils"
              ;;
            ai_kb)
              run_pytest_with_tracking "component-test/test_pep_ai_kb.py" "$env" "deb" "ai_kb"
              ;;
            pgvector)
              run_pytest_with_tracking "component-test/test_pep_pgvector.py" "$env" "deb" "pgvector"
              ;;
            *)
              echo "⚠️ Unknown test type: $test_type"
              ;;
          esac
          ;;
        *)
          echo "⚠️ Unknown platform: $platform"
          ;;
      esac
    done
  done
done

# --dry-run: env loop is the only place that produces results; nothing real
# happened, so skip the report/index/consolidated generation entirely.
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] env loop complete; skipping report/index/consolidated generation"
  exit 0
fi

echo ""
echo "=========================================="
echo "📊 Generating Component Index Pages"
echo "=========================================="

# Create index.html for each component directory
for test_type in "${test_type_list[@]}"; do
  for env in "${env_list[@]}"; do
    component_dir="test-logs/${test_type}/${env}"
    if [[ -d "$component_dir" ]]; then
      # Count reports in this directory
      report_count=$(find "$component_dir" -name "report-*.html" | wc -l)
      if [[ $report_count -gt 0 ]]; then
        echo "   Creating index for ${test_type}/${env}"

        # Capitalize first letter of test_type (portable for bash 3.x)
        test_type_cap=$(echo "$test_type" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')

        # Create index.html for this component/version
        cat > "${component_dir}/index.html" <<EOF
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>${test_type_cap} Test Reports - PostgreSQL ${env}</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            max-width: 1200px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }
        h1 {
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
        }
        .info {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .reports-list {
            list-style: none;
            padding: 0;
        }
        .reports-list li {
            background: white;
            margin: 10px 0;
            padding: 15px 20px;
            border-radius: 5px;
            border-left: 4px solid #2196F3;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .reports-list a {
            color: #2196F3;
            text-decoration: none;
            font-weight: 500;
            font-size: 16px;
        }
        .reports-list a:hover {
            text-decoration: underline;
        }
        .breadcrumb {
            color: #666;
            margin-bottom: 20px;
        }
        .breadcrumb a {
            color: #2196F3;
            text-decoration: none;
        }
        .breadcrumb a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="breadcrumb">
        <a href="../index.html">← Back to ${test_type_cap} Component</a> |
        <a href="../../index.html">All Components</a>
    </div>

    <h1>${test_type_cap} Test Reports - PostgreSQL ${env}</h1>

    <div class="info">
        <p><strong>Component:</strong> ${test_type}</p>
        <p><strong>PostgreSQL Version:</strong> ${env}</p>
        <p><strong>Report Count:</strong> ${report_count}</p>
    </div>

    <h2>Available Reports</h2>
    <ul class="reports-list">
EOF

        # Add links to all reports in this directory
        for report in "$component_dir"/report-*.html; do
          if [[ -f "$report" ]]; then
            report_basename=$(basename "$report")
            # Extract platform from filename (report-{platform}-{component}-{env}.html)
            platform_name=$(echo "$report_basename" | sed 's/report-\([^-]*\)-.*/\1/')
            # Convert to uppercase (portable for bash 3.x)
            platform_name_upper=$(echo "$platform_name" | tr '[:lower:]' '[:upper:]')
            echo "        <li><a href=\"${report_basename}\">${platform_name_upper} - ${report_basename}</a></li>" >> "${component_dir}/index.html"
          fi
        done

        # Close HTML
        cat >> "${component_dir}/index.html" <<EOF
    </ul>
</body>
</html>
EOF

      fi
    fi
  done
done

echo ""
echo "=========================================="
echo "📊 Generating Component-Level Index Pages"
echo "=========================================="

# Helper function to parse JUnit XML and extract aggregate stats
get_test_stats() {
  local xml_file=$1
  if [[ -f "$xml_file" ]]; then
    # Extract attributes from testsuite element using grep/sed (portable)
    local tests=$(grep -o 'tests="[0-9]*"' "$xml_file" | head -1 | sed 's/tests="\([0-9]*\)"/\1/')
    local failures=$(grep -o 'failures="[0-9]*"' "$xml_file" | head -1 | sed 's/failures="\([0-9]*\)"/\1/')
    local errors=$(grep -o 'errors="[0-9]*"' "$xml_file" | head -1 | sed 's/errors="\([0-9]*\)"/\1/')
    local skipped=$(grep -o 'skipped="[0-9]*"' "$xml_file" | head -1 | sed 's/skipped="\([0-9]*\)"/\1/')

    tests=${tests:-0}
    failures=${failures:-0}
    errors=${errors:-0}
    skipped=${skipped:-0}

    local passed=$((tests - failures - errors - skipped))
    local failed=$((failures + errors))

    echo "${passed}|${failed}|${skipped}"
  else
    echo "0|0|0"
  fi
}

# Helper function to parse JUnit XML and extract per-container stats
# Output format: one line per container: container_name|passed|failed|skipped
get_per_container_stats() {
  local xml_file=$1
  if [[ -f "$xml_file" ]]; then
    python3 - "$xml_file" <<'PYEOF'
import xml.etree.ElementTree as ET
import re, sys

xml_file = sys.argv[1]
try:
    tree = ET.parse(xml_file)
    root = tree.getroot()
    ts = root if root.tag == 'testsuite' else root.find('testsuite')
    if ts is None:
        sys.exit(0)

    def get_base_container(name):
        """Extract base container from potentially prefixed names.
        e.g. 'bloom-auto-alma10-arm' -> 'auto-alma10-arm'
             'pgedge-lolor_16-auto' -> 'auto'
             'auto-alma10-arm' -> 'auto-alma10-arm'
             'my-rocky9-amd' -> 'my-rocky9-amd'
        """
        for prefix in ['auto-', 'my-']:
            idx = name.find(prefix)
            if idx >= 0:
                return name[idx:]
        if name == 'auto' or name.endswith('-auto'):
            return 'auto'
        return name

    containers = {}
    for tc in ts.findall('testcase'):
        name = tc.get('name', '')
        # Match container-type with lookahead: -deb/-rhel must be followed by ] or -
        # This prevents false match on 'debian' in container names like auto-debian13-amd
        m = re.search(r'\[(.+)-(rhel|deb)(?=[-\]])', name)
        if m:
            raw_container = m.group(1)
        else:
            # Fallback: tests without -rhel/-deb suffix (e.g. test_pgbouncer_show_help[auto-debian13-amd])
            m2 = re.search(r'\[([^\]]+)\]', name)
            if not m2:
                continue
            raw_container = m2.group(1)
        container = get_base_container(raw_container)

        if container not in containers:
            containers[container] = {'passed': 0, 'failed': 0, 'skipped': 0}

        if tc.find('failure') is not None or tc.find('error') is not None:
            containers[container]['failed'] += 1
        elif tc.find('skipped') is not None:
            containers[container]['skipped'] += 1
        else:
            containers[container]['passed'] += 1

    for container in sorted(containers.keys()):
        s = containers[container]
        print(f"{container}|{s['passed']}|{s['failed']}|{s['skipped']}")
except Exception as e:
    print(f"ERROR|0|0|0", file=sys.stderr)
PYEOF
  fi
}

# Create component-level index.html that aggregates all PG versions and platforms
for test_type in "${test_type_list[@]}"; do
  component_base_dir="test-logs/${test_type}"
  if [[ -d "$component_base_dir" ]]; then
    # Check if there are any version subdirectories with reports
    version_dirs=$(find "$component_base_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    if [[ -n "$version_dirs" ]]; then
      echo "   Creating component index for ${test_type}"

      # Capitalize first letter of test_type (portable for bash 3.x)
      test_type_cap=$(echo "$test_type" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')

      # Create component-level index.html
      cat > "${component_base_dir}/index.html" <<EOF
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>${test_type_cap} Test Reports - All Versions</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            max-width: 1400px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .header h1 {
            margin: 0 0 10px 0;
        }
        .header .subtitle {
            opacity: 0.9;
            font-size: 14px;
        }
        .breadcrumb {
            color: #666;
            margin-bottom: 20px;
        }
        .breadcrumb a {
            color: #2196F3;
            text-decoration: none;
        }
        .breadcrumb a:hover {
            text-decoration: underline;
        }
        .version-section {
            background: white;
            border-radius: 8px;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .version-header {
            background: #f8f9fa;
            padding: 15px 20px;
            border-bottom: 2px solid #dee2e6;
            font-weight: 600;
            color: #333;
        }
        .version-header .pg-badge {
            background: #667eea;
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th {
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #666;
            font-size: 12px;
            text-transform: uppercase;
        }
        td {
            padding: 12px;
            border-bottom: 1px solid #eee;
        }
        tr:hover {
            background-color: #f8f9fa;
        }
        .platform-badge {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .platform-badge.rpm {
            background-color: #fee2e2;
            color: #991b1b;
        }
        .platform-badge.deb {
            background-color: #dbeafe;
            color: #1e40af;
        }
        .report-link {
            color: #667eea;
            text-decoration: none;
            font-weight: 500;
        }
        .report-link:hover {
            text-decoration: underline;
        }
        .no-reports {
            padding: 20px;
            text-align: center;
            color: #666;
        }
        .stats {
            display: flex;
            gap: 8px;
            font-size: 12px;
        }
        .stat {
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 600;
        }
        .stat.passed {
            background: #d1fae5;
            color: #065f46;
        }
        .stat.failed {
            background: #fee2e2;
            color: #991b1b;
        }
        .stat.skipped {
            background: #fef3c7;
            color: #92400e;
        }
    </style>
</head>
<body>
    <div class="breadcrumb">
        <a href="../index.html">← Back to All Components</a>
    </div>

    <div class="header">
        <h1>🧪 ${test_type_cap} Test Reports</h1>
        <div class="subtitle">All PostgreSQL versions and platforms</div>
    </div>
EOF

      # Iterate through each version directory (16, 17, 18)
      for version_dir in $(ls -d "$component_base_dir"/*/ 2>/dev/null | sort); do
        version=$(basename "$version_dir")
        report_count=$(find "$version_dir" -name "report-*.html" 2>/dev/null | wc -l | tr -d ' ')

        if [[ $report_count -gt 0 ]]; then
          cat >> "${component_base_dir}/index.html" <<EOF
    <div class="version-section">
        <div class="version-header">
            PostgreSQL <span class="pg-badge">${version}</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Platform</th>
                    <th>Container</th>
                    <th>Results</th>
                    <th>Report File</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
EOF

          # Add rows for each report, broken down by container
          for report in "$version_dir"/report-*.html; do
            if [[ -f "$report" ]]; then
              report_basename=$(basename "$report")
              # Extract platform from filename
              platform_name=$(echo "$report_basename" | sed 's/report-\([^-]*\)-.*/\1/')
              platform_upper=$(echo "$platform_name" | tr '[:lower:]' '[:upper:]')
              platform_lower=$(echo "$platform_name" | tr '[:upper:]' '[:lower:]')

              # Get corresponding XML file for per-container stats
              xml_file="${report%.html}.xml"
              if [[ -f "$xml_file" ]]; then
                container_stats=$(get_per_container_stats "$xml_file")
                if [[ -n "$container_stats" ]]; then
                  while IFS='|' read -r container_name c_passed c_failed c_skipped; do
                    stats_html="<span class=\"stats\"><span class=\"stat passed\">✓ ${c_passed}</span><span class=\"stat failed\">✗ ${c_failed}</span><span class=\"stat skipped\">○ ${c_skipped}</span></span>"
                    cat >> "${component_base_dir}/index.html" <<EOF
                <tr>
                    <td><span class="platform-badge ${platform_lower}">${platform_upper}</span></td>
                    <td>${container_name}</td>
                    <td>${stats_html}</td>
                    <td>${report_basename}</td>
                    <td><a href="${version}/${report_basename}" class="report-link">View Report →</a></td>
                </tr>
EOF
                  done <<< "$container_stats"
                fi
              else
                cat >> "${component_base_dir}/index.html" <<EOF
                <tr>
                    <td><span class="platform-badge ${platform_lower}">${platform_upper}</span></td>
                    <td>-</td>
                    <td>-</td>
                    <td>${report_basename}</td>
                    <td><a href="${version}/${report_basename}" class="report-link">View Report →</a></td>
                </tr>
EOF
              fi
            fi
          done

          cat >> "${component_base_dir}/index.html" <<EOF
            </tbody>
        </table>
    </div>
EOF
        fi
      done

      # Close HTML
      cat >> "${component_base_dir}/index.html" <<EOF
</body>
</html>
EOF
    fi
  fi
done

echo ""
echo "=========================================="
echo "📊 Generating Master Index Page"
echo "=========================================="

# Get relative path for consolidated report (strip 'test-logs/' prefix)
CONSOLIDATED_RELATIVE=$(echo "$CONSOLIDATED_REPORT_DIR" | sed 's|^test-logs/||')

# Create master index.html at test-logs/index.html
cat > "test-logs/index.html" <<EOF
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>pgEdge Test Reports - All Components</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            max-width: 1400px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            border-radius: 8px;
            margin-bottom: 30px;
            text-align: center;
        }
        .header h1 {
            margin: 0 0 10px 0;
            font-size: 32px;
        }
        .header .subtitle {
            opacity: 0.9;
            font-size: 16px;
        }
        .header .timestamp {
            opacity: 0.7;
            font-size: 12px;
            margin-top: 15px;
        }
        .components-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .component-card {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .component-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }
        .component-card-header {
            background: #f8f9fa;
            padding: 20px;
            border-bottom: 1px solid #eee;
        }
        .component-card-header h2 {
            margin: 0;
            font-size: 18px;
            color: #333;
        }
        .component-card-body {
            padding: 15px 20px;
        }
        .version-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        .version-table th {
            text-align: left;
            padding: 8px 5px;
            border-bottom: 2px solid #eee;
            color: #666;
            font-size: 11px;
            text-transform: uppercase;
        }
        .version-table td {
            padding: 8px 5px;
            border-bottom: 1px solid #eee;
        }
        .version-table tr:last-child td {
            border-bottom: none;
        }
        .version-badge {
            background: #667eea;
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }
        .platform-badge {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 600;
        }
        .platform-badge.rpm {
            background: #fee2e2;
            color: #991b1b;
        }
        .platform-badge.deb {
            background: #dbeafe;
            color: #1e40af;
        }
        .stats {
            display: flex;
            gap: 8px;
            font-size: 11px;
        }
        .stat {
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 600;
        }
        .stat.passed {
            background: #d1fae5;
            color: #065f46;
        }
        .stat.failed {
            background: #fee2e2;
            color: #991b1b;
        }
        .stat.skipped {
            background: #fef3c7;
            color: #92400e;
        }
        .component-card-footer {
            background: #f8f9fa;
            padding: 15px 20px;
            text-align: center;
        }
        .view-all-link {
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
        }
        .view-all-link:hover {
            text-decoration: underline;
        }
        .consolidated-link {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }
        .consolidated-link a {
            color: #667eea;
            text-decoration: none;
            font-weight: 600;
        }
        .consolidated-link a:hover {
            text-decoration: underline;
        }
        .no-reports {
            text-align: center;
            padding: 40px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🧪 pgEdge Test Reports</h1>
        <div class="subtitle">Comprehensive test results for all components</div>
        <div class="timestamp">Generated: $(date '+%Y-%m-%d %H:%M:%S')</div>
    </div>

    <div class="consolidated-link">
        📊 <a href="${CONSOLIDATED_RELATIVE}/index.html">View Latest Consolidated Report →</a>
    </div>

    <h2>Components</h2>
    <div class="components-grid">
EOF

# Add card for each component that has reports
for test_type in server snowflake pgbouncer pgbackrest postgrest lolor postgis system_stats vectorizer zerodowntime mcp rag ace repo_health docloader anonymizer pg_vectorize pg_tokenizer vchord_bm25 pgaudit pgadmin4 patroni pg_stat_monitor ai_db_workbench radar spock_patroni_failover llvmjit ai_kb spock supautils pgvector; do
  component_dir="test-logs/${test_type}"
  if [[ -d "$component_dir" ]]; then
    # Check for any HTML reports
    total_reports=$(find "$component_dir" -name "report-*.html" 2>/dev/null | wc -l | tr -d ' ')
    if [[ $total_reports -gt 0 ]]; then
      # Capitalize component name
      test_type_cap=$(echo "$test_type" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')

      cat >> "test-logs/index.html" <<EOF
        <div class="component-card">
            <div class="component-card-header">
                <h2>${test_type_cap}</h2>
            </div>
            <div class="component-card-body">
                <table class="version-table">
                    <thead>
                        <tr>
                            <th>Version</th>
                            <th>Platform</th>
                            <th>Container</th>
                            <th>Results</th>
                        </tr>
                    </thead>
                    <tbody>
EOF

      # List versions with their platforms, containers, and stats
      for version_dir in $(ls -d "$component_dir"/*/ 2>/dev/null | sort); do
        version=$(basename "$version_dir")

        # Check RPM report — show per-container rows
        rpm_xml=$(find "$version_dir" -name "report-rpm-*.xml" 2>/dev/null | head -1)
        if [[ -n "$rpm_xml" && -f "$rpm_xml" ]]; then
          container_stats=$(get_per_container_stats "$rpm_xml")
          if [[ -n "$container_stats" ]]; then
            while IFS='|' read -r container_name c_passed c_failed c_skipped; do
              cat >> "test-logs/index.html" <<EOF
                        <tr>
                            <td><span class="version-badge">PG ${version}</span></td>
                            <td><span class="platform-badge rpm">RPM</span></td>
                            <td>${container_name}</td>
                            <td>
                                <span class="stats">
                                    <span class="stat passed">✓ ${c_passed}</span>
                                    <span class="stat failed">✗ ${c_failed}</span>
                                    <span class="stat skipped">○ ${c_skipped}</span>
                                </span>
                            </td>
                        </tr>
EOF
            done <<< "$container_stats"
          fi
        fi

        # Check DEB report — show per-container rows
        deb_xml=$(find "$version_dir" -name "report-deb-*.xml" 2>/dev/null | head -1)
        if [[ -n "$deb_xml" && -f "$deb_xml" ]]; then
          container_stats=$(get_per_container_stats "$deb_xml")
          if [[ -n "$container_stats" ]]; then
            while IFS='|' read -r container_name c_passed c_failed c_skipped; do
              cat >> "test-logs/index.html" <<EOF
                        <tr>
                            <td><span class="version-badge">PG ${version}</span></td>
                            <td><span class="platform-badge deb">DEB</span></td>
                            <td>${container_name}</td>
                            <td>
                                <span class="stats">
                                    <span class="stat passed">✓ ${c_passed}</span>
                                    <span class="stat failed">✗ ${c_failed}</span>
                                    <span class="stat skipped">○ ${c_skipped}</span>
                                </span>
                            </td>
                        </tr>
EOF
            done <<< "$container_stats"
          fi
        fi
      done

      cat >> "test-logs/index.html" <<EOF
                    </tbody>
                </table>
            </div>
            <div class="component-card-footer">
                <a href="${test_type}/index.html" class="view-all-link">View All Reports →</a>
            </div>
        </div>
EOF
    fi
  fi
done

# Close the master index HTML
cat >> "test-logs/index.html" <<EOF
    </div>
</body>
</html>
EOF

echo "   ✅ Master index created: test-logs/index.html"

echo ""
echo "=========================================="
echo "📊 Generating Consolidated Report"
echo "=========================================="

# Create Python script to generate consolidated report
python3 - <<'PYTHON_SCRIPT'
import xml.etree.ElementTree as ET
import os
import sys
from pathlib import Path
from datetime import datetime

# Get consolidated report directory from environment
report_dir = os.environ.get('CONSOLIDATED_REPORT_DIR', 'test-logs/consolidated')

# Find all JUnit XML files
xml_files = list(Path(report_dir).glob('*.xml'))

if not xml_files:
    print("⚠️  No test results found!")
    sys.exit(1)

# Parse all XML files and collect per-container statistics
import re

total_tests = 0
total_passed = 0
total_failed = 0
total_skipped = 0
total_errors = 0
test_results = []

for xml_file in xml_files:
    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Extract test suite info
        testsuite = root if root.tag == 'testsuite' else root.find('testsuite')
        if testsuite is None:
            continue

        # Extract platform, component, and env from filename
        # Format: report-{platform}-{component}-{env}.xml
        name_parts = xml_file.stem.replace('report-', '').split('-')
        if len(name_parts) >= 3:
            platform = name_parts[0]
            component = name_parts[1]
            env = name_parts[2] if len(name_parts) == 3 else '-'.join(name_parts[2:])
        else:
            platform = component = env = "unknown"

        # Parse individual testcase elements and group by base container
        containers = {}
        for tc in testsuite.findall('testcase'):
            name = tc.get('name', '')
            tc_time = float(tc.get('time', 0))
            # Match container-type with lookahead: -deb/-rhel must be followed by ] or -
            # This prevents false match on 'debian' in container names like auto-debian13-amd
            m = re.search(r'\[(.+)-(rhel|deb)(?=[-\]])', name)
            if m:
                raw_container = m.group(1)
            else:
                # Fallback: tests without -rhel/-deb suffix (e.g. test_pgbouncer_show_help[auto-debian13-amd])
                m2 = re.search(r'\[([^\]]+)\]', name)
                if not m2:
                    continue
                raw_container = m2.group(1)
            # Strip extension prefix (e.g. 'bloom-auto-alma10-arm' -> 'auto-alma10-arm')
            container = raw_container
            for pfx in ['auto-', 'my-']:
                idx = raw_container.find(pfx)
                if idx >= 0:
                    container = raw_container[idx:]
                    break
            else:
                if raw_container == 'auto' or raw_container.endswith('-auto'):
                    container = 'auto'

            if container not in containers:
                containers[container] = {'passed': 0, 'failed': 0, 'skipped': 0, 'tests': 0, 'time': 0.0}

            containers[container]['tests'] += 1
            containers[container]['time'] += tc_time

            if tc.find('failure') is not None or tc.find('error') is not None:
                containers[container]['failed'] += 1
            elif tc.find('skipped') is not None:
                containers[container]['skipped'] += 1
            else:
                containers[container]['passed'] += 1

        # Create a result entry per container
        for container_name in sorted(containers.keys()):
            stats = containers[container_name]
            total_tests += stats['tests']
            total_passed += stats['passed']
            total_failed += stats['failed']
            total_skipped += stats['skipped']

            if stats['failed'] > 0:
                status = "FAILED"
                status_class = "failed"
            elif stats['skipped'] == stats['tests']:
                status = "SKIPPED"
                status_class = "skipped"
            else:
                status = "PASSED"
                status_class = "passed"

            test_results.append({
                'platform': platform.upper(),
                'component': component,
                'container': container_name,
                'env': env,
                'tests': stats['tests'],
                'passed': stats['passed'],
                'failed': stats['failed'],
                'errors': 0,
                'skipped': stats['skipped'],
                'time': stats['time'],
                'status': status,
                'status_class': status_class,
                'html_report': xml_file.stem + '.html'
            })

    except Exception as e:
        print(f"⚠️  Error parsing {xml_file}: {e}")
        continue

# Sort results by environment, platform, component, container
test_results.sort(key=lambda x: (x['env'], x['platform'], x['component'], x['container']))

# Generate HTML report
html_content = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <title>Consolidated Test Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 20px;
        }}
        .header h1 {{
            margin: 0 0 10px 0;
        }}
        .header .timestamp {{
            opacity: 0.9;
            font-size: 14px;
        }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .summary-card h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #666;
            text-transform: uppercase;
        }}
        .summary-card .value {{
            font-size: 32px;
            font-weight: bold;
        }}
        .summary-card.total .value {{ color: #667eea; }}
        .summary-card.passed .value {{ color: #10b981; }}
        .summary-card.failed .value {{ color: #ef4444; }}
        .summary-card.skipped .value {{ color: #f59e0b; }}

        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th {{
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #333;
            border-bottom: 2px solid #dee2e6;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #dee2e6;
        }}
        tr:hover {{
            background-color: #f8f9fa;
        }}
        .status-badge {{
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .status-badge.passed {{
            background-color: #d1fae5;
            color: #065f46;
        }}
        .status-badge.failed {{
            background-color: #fee2e2;
            color: #991b1b;
        }}
        .status-badge.skipped {{
            background-color: #fef3c7;
            color: #92400e;
        }}
        .report-link {{
            color: #667eea;
            text-decoration: none;
        }}
        .report-link:hover {{
            text-decoration: underline;
        }}
        .stats-cell {{
            font-family: monospace;
            font-size: 13px;
        }}
        .footer {{
            margin-top: 30px;
            text-align: center;
            color: #666;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🧪 Consolidated Test Report</h1>
        <div class="timestamp">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
    </div>

    <div class="summary">
        <div class="summary-card total">
            <h3>Total Tests</h3>
            <div class="value">{total_tests}</div>
        </div>
        <div class="summary-card passed">
            <h3>Passed</h3>
            <div class="value">{total_passed}</div>
        </div>
        <div class="summary-card failed">
            <h3>Failed</h3>
            <div class="value">{total_failed + total_errors}</div>
        </div>
        <div class="summary-card skipped">
            <h3>Skipped</h3>
            <div class="value">{total_skipped}</div>
        </div>
    </div>

    <table>
        <thead>
            <tr>
                <th>Environment</th>
                <th>Platform</th>
                <th>Component</th>
                <th>Container</th>
                <th>Status</th>
                <th>Tests</th>
                <th>Passed</th>
                <th>Failed</th>
                <th>Skipped</th>
                <th>Time (s)</th>
                <th>Detailed Report</th>
            </tr>
        </thead>
        <tbody>
'''

for result in test_results:
    html_content += f'''
            <tr>
                <td><strong>PG {result['env']}</strong></td>
                <td>{result['platform']}</td>
                <td>{result['component']}</td>
                <td>{result['container']}</td>
                <td><span class="status-badge {result['status_class']}">{result['status']}</span></td>
                <td class="stats-cell">{result['tests']}</td>
                <td class="stats-cell">{result['passed']}</td>
                <td class="stats-cell">{result['failed'] + result['errors']}</td>
                <td class="stats-cell">{result['skipped']}</td>
                <td class="stats-cell">{result['time']:.2f}</td>
                <td><a href="{result['html_report']}" class="report-link">View Details →</a></td>
            </tr>
'''

html_content += f'''
        </tbody>
    </table>

    <div class="footer">
        <p>All individual test reports are available in the same directory.</p>
        <p>Total test runs: {len(test_results)} | Total execution time: {sum(r['time'] for r in test_results):.2f}s</p>
    </div>
</body>
</html>
'''

# Write consolidated report
consolidated_html = Path(report_dir) / 'index.html'
with open(consolidated_html, 'w') as f:
    f.write(html_content)

print(f"✅ Consolidated report generated: {consolidated_html}")
print(f"\n📊 Summary:")
print(f"   Total Tests: {total_tests}")
print(f"   Passed: {total_passed}")
print(f"   Failed: {total_failed + total_errors}")
print(f"   Skipped: {total_skipped}")

PYTHON_SCRIPT

echo ""
echo "=========================================="
echo "✅ All Tests Completed"
echo "=========================================="
echo ""
echo "📊 Consolidated Report:"
echo "   ${CONSOLIDATED_REPORT_DIR}/index.html"
echo ""
echo "📁 Component-Specific Reports:"

# List all component directories created
for test_type in "${test_type_list[@]}"; do
  for env in "${env_list[@]}"; do
    component_dir="test-logs/${test_type}/${env}"
    if [[ -d "$component_dir" ]]; then
      # Count reports in this directory
      report_count=$(find "$component_dir" -name "*.html" | wc -l)
      if [[ $report_count -gt 0 ]]; then
        echo "   ${component_dir}/ (${report_count} report(s))"
      fi
    fi
  done
done

echo ""
echo "=========================================="
echo ""
echo "To view consolidated report, open:"
echo "   ${CONSOLIDATED_REPORT_DIR}/index.html"
echo ""
echo "To view component-specific reports, navigate to:"
echo "   test-logs/{component}/{pg_version}/"
echo "=========================================="
