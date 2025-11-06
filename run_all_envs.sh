#!/bin/bash

# Directory containing your .env files
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
    case "$platform" in
      RPM|rpm)
        echo "▶️ Running RPM tests for env ${env}"
        pytest -v -s test_pep_server_rhel.py \
          --html="${report_dir}/report-deb-${env}.html" \
          --self-contained-html
        ;;
      DEB|deb)
        echo "▶️ Running DEB tests for env ${env}"
        pytest -v -s test_pep_server_deb.py \
          --html="${report_dir}/report-deb-${env}.html" \
          --self-contained-html
        ;;
      *)
        echo "⚠️ Unknown platform: $platform"
        ;;
    esac
  done
done

echo "✅ All selected tests completed. Reports are available in the test-logs/ folder."
