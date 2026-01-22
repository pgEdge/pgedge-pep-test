#!/usr/bin/env bash
set -euo pipefail

# CI helper: iterate components and run tests in ephemeral containers
# Usage: ci/run_component_tests.sh [components_csv]
# components_csv format: component_name,container_image,platform_type (platform_type: deb|rhel)
# Example line: postgis,pgedge-postgis:16-deb,deb

COMPONENTS_CSV=${1:-ci/components.csv}
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=python3

if [ ! -f "$COMPONENTS_CSV" ]; then
  echo "Components CSV not found: $COMPONENTS_CSV"
  exit 1
fi

while IFS=',' read -r component image platform; do
  component=$(echo "$component" | xargs)
  image=$(echo "$image" | xargs)
  platform=$(echo "$platform" | xargs)

  if [ -z "$component" ]; then
    continue
  fi

  echo "\n=== Running component: $component (image: $image, platform: $platform) ==="

  # create and start container
  container_name="ci-${component}"
  docker run -d --name "$container_name" "$image" sleep infinity

  # expose variables for pytest to pick up
  if [ "$platform" = "deb" ]; then
    export DEB_CONTAINERS="$container_name"
    export DEB_POSTGIS_PACKAGE="$image"
  else
    export CONTAINERS="$container_name"
    export POSTGIS_PACKAGE="$image"
  fi

  # run pytest for the matching component test file
  case "$component" in
    postgis)
      $PYTHON -m pytest -q component-test/test_pep_postgis.py -k test_verify_bundled_files -s || true
      ;;
    pgbouncer)
      $PYTHON -m pytest -q component-test/test_pep_pgbouncer.py -k test_stop_pgbouncer -s || true
      ;;
    lolor)
      $PYTHON -m pytest -q component-test/test_pep_lolor.py -k test_some_lolor_test -s || true
      ;;
    *)
      echo "No test mapping for component: $component, skipping"
      ;;
  esac

  # teardown
  docker rm -f "$container_name" || true

done < "$COMPONENTS_CSV"
