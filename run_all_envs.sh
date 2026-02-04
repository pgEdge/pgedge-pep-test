#!/bin/bash

# Directory containing your env files
ENV_DIR="./configuration"

mkdir -p test-logs

# Prompt user for environment choice
echo "Select environment(s) to run:"
echo "1) 16"
echo "2) 17"
echo "3) 18"
echo "4) All"
read -p "Enter your choice (16/17/18/all): " env_choice

# Prompt user for platform choice
echo "Select platform(s) to run:"
echo "1) RPM"
echo "2) DEB"
echo "3) All"
read -p "Enter your choice (RPM/DEB/all): " platform_choice

# Prompt user for test type choice
echo "Select test type(s) to run:"
echo "1) server - PostgreSQL server tests"
echo "2) snowflake - Snowflake extension tests"
echo "3) lolor - lolor extension tests"
echo "4) pgbouncer - PgBouncer tests"
echo "5) all - All tests"
read -p "Enter your choice (server/snowflake/pgbouncer/lolor/all): " test_type_choice

# Determine environments to run
if [[ "$env_choice" == "all" || "$env_choice" == "All" ]]; then
  env_list=(16 17 18)
else
  env_list=("$env_choice")
fi

# Determine platforms to run
if [[ "$platform_choice" == "all" || "$platform_choice" == "All" ]]; then
  platform_list=(RPM DEB)
else
  platform_list=("$platform_choice")
fi

# Determine test types to run
if [[ "$test_type_choice" == "all" || "$test_type_choice" == "All" ]]; then
  test_type_list=(server snowflake pgbouncer lolor)
else
  test_type_list=("$test_type_choice")
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
  timestamp=$(date +%Y%m%d_%H%M%S)
  report_dir="test-logs/report-${env}-${timestamp}"

  for platform in "${platform_list[@]}"; do
    for test_type in "${test_type_list[@]}"; do
      case "$platform" in
        RPM|rpm)
          case "$test_type" in
            server)
              echo "▶️ Running RPM server tests for env ${env}"
              pytest -v -s test_pep_server_rhel.py \
                --html="${report_dir}/report-rpm-server-${env}.html" \
                --self-contained-html
              ;;
            snowflake)
              echo "▶️ Running RPM snowflake tests for env ${env}"
              pytest -v -s test_pep_snowflake.py \
                --html="${report_dir}/report-rpm-snowflake-${env}.html" \
                --self-contained-html
              ;;
            lolor)
              echo "▶️ Running RPM snowflake tests for env ${env}"
              pytest -v -s test_pep_lolor_rhel.py \
                --html="${report_dir}/report-rpm-lolor-${env}.html" \
                --self-contained-html
              ;;
            pgbouncer)
              echo "▶️ Running RPM pgbouncer tests for env ${env}"
              pytest -v -s test_pep_pgbouncer.py \
                --html="${report_dir}/report-rpm-pgbouncer-${env}.html" \
                --self-contained-html
              ;;
            *)
              echo "⚠️ Unknown test type: $test_type"
              ;;
          esac
          ;;
        DEB|deb)
          case "$test_type" in
            server)
              echo "▶️ Running DEB server tests for env ${env}"
              pytest -v -s test_pep_server_deb.py \
                --html="${report_dir}/report-deb-server-${env}.html" \
                --self-contained-html
              ;;
            snowflake)
              echo "▶️ Running DEB snowflake tests for env ${env}"
              pytest -v -s test_pep_snowflake_deb.py \
                --html="${report_dir}/report-deb-snowflake-${env}.html" \
                --self-contained-html
              ;;
            lolor)
              echo "▶️ Running DEB snowflake tests for env ${env}"
              pytest -v -s test_pep_lolor_deb.py \
                --html="${report_dir}/report-deb-lolor-${env}.html" \
                --self-contained-html
              ;;
            pgbouncer)
              echo "▶️ Running DEB pgbouncer tests for env ${env}"
              pytest -v -s test_pep_pgbouncer_deb.py \
                --html="${report_dir}/report-deb-pgbouncer-${env}.html" \
                --self-contained-html
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

echo "✅ All selected tests completed. Reports are available in the test-logs/ folder."
