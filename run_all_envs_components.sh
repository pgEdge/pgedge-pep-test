#!/bin/bash

# Directory containing your env files
ENV_DIR="./configuration"

mkdir -p test-logs

# Generate timestamp for this test run
RUN_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CONSOLIDATED_REPORT_DIR="test-logs/consolidated-${RUN_TIMESTAMP}"
mkdir -p "$CONSOLIDATED_REPORT_DIR"

# Array to store all individual report paths
declare -a ALL_REPORTS=()
declare -a ALL_JUNIT_XMLS=()

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
echo "4) lolor - LOLOR tests"
echo "5) postgis - PostGIS tests"
echo "6) system_stats - System Stats tests"
echo "7) vectorizer - Vectorizer tests"
echo "8) zerodowntime - Zero Downtime Integration tests"
echo "9) all - All tests"
echo ""
echo "💡 You can specify multiple components separated by commas"
echo "   Example: lolor,postgis,system_stats"
read -p "Enter your choice: " test_type_choice

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
  test_type_list=(server snowflake pgbouncer lolor postgis system_stats vectorizer zerodowntime)
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

  for platform in "${platform_list[@]}"; do
    for test_type in "${test_type_list[@]}"; do
      case "$platform" in
        RPM|rpm)
          export PLATFORM_FILTER=rpm
          case "$test_type" in
            server)
              run_pytest_with_tracking "test_pep_server_rhel.py" "$env" "rpm" "server"
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
            *)
              echo "⚠️ Unknown test type: $test_type"
              ;;
          esac
          ;;
        DEB|deb)
          export PLATFORM_FILTER=deb
          case "$test_type" in
            server)
              run_pytest_with_tracking "test_pep_server_deb.py" "$env" "deb" "server"
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

        # Create index.html for this component/version
        cat > "${component_dir}/index.html" <<EOF
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>${test_type^} Test Reports - PostgreSQL ${env}</title>
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
        <a href="../../../${CONSOLIDATED_REPORT_DIR}/index.html">← Back to Consolidated Report</a>
    </div>

    <h1>${test_type^} Test Reports - PostgreSQL ${env}</h1>

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
            echo "        <li><a href=\"${report_basename}\">${platform_name^^} - ${report_basename}</a></li>" >> "${component_dir}/index.html"
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

# Parse all XML files and collect statistics
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

        tests = int(testsuite.get('tests', 0))
        failures = int(testsuite.get('failures', 0))
        errors = int(testsuite.get('errors', 0))
        skipped = int(testsuite.get('skipped', 0))
        time = float(testsuite.get('time', 0))

        passed = tests - failures - errors - skipped

        total_tests += tests
        total_failed += failures
        total_errors += errors
        total_skipped += skipped
        total_passed += passed

        # Extract platform, component, and env from filename
        # Format: report-{platform}-{component}-{env}.xml
        name_parts = xml_file.stem.replace('report-', '').split('-')
        if len(name_parts) >= 3:
            platform = name_parts[0]
            component = name_parts[1]
            env = name_parts[2] if len(name_parts) == 3 else '-'.join(name_parts[2:])
        else:
            platform = component = env = "unknown"

        # Determine status
        if failures > 0 or errors > 0:
            status = "FAILED"
            status_class = "failed"
        elif skipped == tests:
            status = "SKIPPED"
            status_class = "skipped"
        else:
            status = "PASSED"
            status_class = "passed"

        test_results.append({
            'platform': platform.upper(),
            'component': component,
            'env': env,
            'tests': tests,
            'passed': passed,
            'failed': failures,
            'errors': errors,
            'skipped': skipped,
            'time': time,
            'status': status,
            'status_class': status_class,
            'html_report': xml_file.stem + '.html'
        })

    except Exception as e:
        print(f"⚠️  Error parsing {xml_file}: {e}")
        continue

# Sort results by environment, platform, component
test_results.sort(key=lambda x: (x['env'], x['platform'], x['component']))

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
