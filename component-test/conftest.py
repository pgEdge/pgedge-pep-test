import os
import pytest
from dotenv import load_dotenv

# Import pytest_html for adding extra content to HTML reports
try:
    import pytest_html
except ImportError:
    # pytest-html not installed
    pytest_html = None

# Load environment variables
load_dotenv()


def detect_component_from_test_file(config):
    """Detect which component is being tested based on test file name"""
    # Get the test file being run
    test_paths = config.args

    # Component mapping based on test file names
    component_map = {
        'test_pep_pgbouncer': {
            'name': 'PgBouncer',
            'version_env': 'PGEDGE_PGBOUNCER_VERSION',
            'version_default': '1.25.1',
            'rhel_package_env': 'PGBOUNCER_PACKAGE',
            'rhel_package_default': 'pgedge-pgbouncer',
            'deb_package_env': 'DEB_PGBOUNCER_PACKAGE',
            'deb_package_default': 'pgedge-pgbouncer'
        },
        'test_pep_postgis': {
            'name': 'PostGIS',
            'version_env': 'PGEDGE_POSTGIS35_16_VERSION',
            'version_default': '3.5.4',
            'rhel_package_env': 'POSTGIS_PACKAGE',
            'rhel_package_default': 'pgedge-postgis35_16',
            'deb_package_env': 'DEB_POSTGIS_PACKAGE',
            'deb_package_default': 'pgedge-postgresql-16-postgis-3'
        },
        'test_pep_lolor': {
            'name': 'LOLOR',
            'version_env': 'PGEDGE_LOLOR_16_VERSION',
            'version_default': '1.2.2',
            'rhel_package_env': 'LOLOR_PACKAGE',
            'rhel_package_default': 'pgedge-lolor_16',
            'deb_package_env': 'DEB_LOLOR_PACKAGE',
            'deb_package_default': 'pgedge-postgresql-16-lolor'
        },
        'test_pep_snowflake': {
            'name': 'Snowflake',
            'version_env': 'PGEDGE_SNOWFLAKE_16_VERSION',
            'version_default': '2.4',
            'rhel_package_env': 'SNOWFLAKE_PACKAGE',
            'rhel_package_default': 'pgedge-snowflake_16',
            'deb_package_env': 'DEB_SNOWFLAKE_PACKAGE',
            'deb_package_default': 'pgedge-postgresql-16-snowflake'
        },
        'test_pep_system_stats': {
            'name': 'System Stats',
            'version_env': 'PGEDGE_SYSTEM_STATS_16_VERSION',
            'version_default': '3.2.1',
            'rhel_package_env': 'SYSTEM_STATS_PACKAGE',
            'rhel_package_default': 'pgedge-system_stats_16',
            'deb_package_env': 'DEB_SYSTEM_STATS_PACKAGE',
            'deb_package_default': 'pgedge-postgresql-16-system_stats'
        },
        'test_pep_vectorizer': {
            'name': 'Vectorizer',
            'version_env': 'PGEDGE_VECTORIZER_18_VERSION',
            'version_default': '1.0-beta2',
            'rhel_package_env': 'VECTORIZER_PACKAGE',
            'rhel_package_default': 'pgedge-vectorizer_18',
            'deb_package_env': 'DEB_VECTORIZER_PACKAGE',
            'deb_package_default': 'pgedge-postgresql-18-vectorizer'
        },
        'test_integration_zerodowntime': {
            'name': 'Zero Downtime Integration',
            'version_env': f'PGEDGE_ENTERPRISE_ALL_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '16.11',
            'rhel_package_env': 'ENTERPRISE_ALL_PACKAGE',
            'rhel_package_default': 'pgedge-enterprise-all_16',
            'deb_package_env': 'DEB_ENTERPRISE_ALL_PACKAGE',
            'deb_package_default': 'pgedge-enterprise-all-16'
        },
        'test_pep_docloader': {
            'name': 'Docloader',
            'version_env': 'PGEDGE_DOCLOADER_VERSION',
            'version_default': '',
            'rhel_package_env': 'DOCLOADER_PACKAGE',
            'rhel_package_default': 'pgedge-docloader',
            'deb_package_env': 'DEB_DOCLOADER_PACKAGE',
            'deb_package_default': 'pgedge-docloader'
        },
        'test_pep_anonymizer': {
            'name': 'Anonymizer',
            'version_env': 'PGEDGE_ANONYMIZER_VERSION',
            'version_default': '',
            'rhel_package_env': 'ANONYMIZER_PACKAGE',
            'rhel_package_default': 'pgedge-anonymizer',
            'deb_package_env': 'DEB_ANONYMIZER_PACKAGE',
            'deb_package_default': 'pgedge-anonymizer'
        },
        'test_pep_pg_vectorize': {
            'name': 'pg-vectorize',
            'version_env': f'PGEDGE_PG_VECTORIZE_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '',
            'rhel_package_env': 'PG_VECTORIZE_PACKAGE',
            'rhel_package_default': f'pgedge-pg-vectorize_{os.getenv("PG_MAJOR_VERSION", "16")}',
            'deb_package_env': 'DEB_PG_VECTORIZE_PACKAGE',
            'deb_package_default': f'pgedge-postgresql-{os.getenv("PG_MAJOR_VERSION", "16")}-pg-vectorize'
        },
        'test_pep_pg_tokenizer': {
            'name': 'pg-tokenizer',
            'version_env': f'PGEDGE_PG_TOKENIZER_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '',
            'rhel_package_env': 'PG_TOKENIZER_PACKAGE',
            'rhel_package_default': f'pgedge-pg-tokenizer_{os.getenv("PG_MAJOR_VERSION", "16")}',
            'deb_package_env': 'DEB_PG_TOKENIZER_PACKAGE',
            'deb_package_default': f'pgedge-postgresql-{os.getenv("PG_MAJOR_VERSION", "16")}-pg-tokenizer'
        },
        'test_pep_vchord_bm25': {
            'name': 'vchord-bm25',
            'version_env': f'PGEDGE_VCHORD_BM25_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '',
            'rhel_package_env': 'VCHORD_BM25_PACKAGE',
            'rhel_package_default': f'pgedge-vchord-bm25_{os.getenv("PG_MAJOR_VERSION", "16")}',
            'deb_package_env': 'DEB_VCHORD_BM25_PACKAGE',
            'deb_package_default': f'pgedge-postgresql-{os.getenv("PG_MAJOR_VERSION", "16")}-vchord-bm25'
        },
        'test_pep_pgaudit': {
            'name': 'pgaudit',
            'version_env': f'PGEDGE_PGAUDIT_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '',
            'rhel_package_env': 'PGAUDIT_PACKAGE',
            'rhel_package_default': f'pgedge-pgaudit_{os.getenv("PG_MAJOR_VERSION", "16")}',
            'deb_package_env': 'DEB_PGAUDIT_PACKAGE',
            'deb_package_default': f'pgedge-postgresql-{os.getenv("PG_MAJOR_VERSION", "16")}-pgaudit'
        },
        'test_pep_pgadmin4': {
            'name': 'pgAdmin4',
            'version_env': 'PGEDGE_PGADMIN4_VERSION',
            'version_default': '',
            'rhel_package_env': 'PGADMIN_PACKAGES',
            'rhel_package_default': 'pgedge-pgadmin4',
            'deb_package_env': 'PGADMIN_PACKAGES',
            'deb_package_default': 'pgedge-pgadmin4'
        },
        'test_pep_patroni': {
            'name': 'Patroni',
            'version_env': 'PGEDGE_PATRONI_VERSION',
            'version_default': '4.1.0',
            'rhel_package_env': 'PATRONI_PACKAGE',
            'rhel_package_default': 'pgedge-patroni-etcd',
            'deb_package_env': 'DEB_PATRONI_PACKAGE',
            'deb_package_default': 'pgedge-patroni'
        },
        'test_pep_pg_stat_monitor': {
            'name': 'Pg Stat Monitor',
            'version_env': f'PGEDGE_PG_STAT_MONITOR_{os.getenv("PG_MAJOR_VERSION", "16")}_VERSION',
            'version_default': '2.3.0',
            'rhel_package_env': 'PG_STAT_MONITOR_PACKAGE',
            'rhel_package_default': f'pgedge-pg-stat-monitor_{os.getenv("PG_MAJOR_VERSION", "16")}',
            'deb_package_env': 'DEB_PG_STAT_MONITOR_PACKAGE',
            'deb_package_default': f'pgedge-postgresql-{os.getenv("PG_MAJOR_VERSION", "16")}-pg-stat-monitor'
        }
    }

    # Try to detect component from test file path
    for test_path in test_paths:
        for key, component_info in component_map.items():
            if key in test_path:
                return component_info

    # Default to generic component if not detected
    return {
        'name': 'Component',
        'version_env': 'COMPONENT_VERSION',
        'version_default': 'unknown',
        'rhel_package_env': 'COMPONENT_PACKAGE',
        'rhel_package_default': 'unknown',
        'deb_package_env': 'DEB_COMPONENT_PACKAGE',
        'deb_package_default': 'unknown'
    }


def pytest_configure(config):
    """Add custom metadata to pytest-html report"""
    if hasattr(config, '_metadata'):
        # Detect component being tested
        component_info = detect_component_from_test_file(config)

        # Get configuration values
        pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
        component_version = os.getenv(component_info['version_env'], component_info['version_default'])
        repo = os.getenv("REPO", "release")

        # Determine package names based on platform
        rhel_package = os.getenv(component_info['rhel_package_env'], component_info['rhel_package_default'])
        deb_package = os.getenv(component_info['deb_package_env'], component_info['deb_package_default'])

        # Check which containers are being used
        rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
        deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

        # Determine package name(s) based on which containers are defined
        if rhel_containers and deb_containers:
            package_name = f"RHEL: {rhel_package}, DEB: {deb_package}"
        elif rhel_containers:
            package_name = rhel_package
        elif deb_containers:
            package_name = deb_package
        else:
            package_name = f"{rhel_package} / {deb_package}"

        # Add metadata to report
        config._metadata['Component'] = component_info['name']
        config._metadata['Package Name'] = package_name
        config._metadata['Package Version'] = component_version
        config._metadata['Repository'] = repo
        config._metadata['PostgreSQL Version'] = pg_major_version


def pytest_html_report_title(report):
    """Customize the report title"""
    # Detect component from environment or config
    pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")

    # Try to detect component from pytest session
    component_info = None
    if hasattr(report, 'config'):
        component_info = detect_component_from_test_file(report.config)

    if component_info:
        component_name = component_info['name']
        component_version = os.getenv(component_info['version_env'], component_info['version_default'])
        report.title = f"{component_name} {component_version} Test Report (PostgreSQL {pg_major_version})"
    else:
        # Fallback to generic title
        report.title = f"Component Test Report (PostgreSQL {pg_major_version})"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Capture and include logs for passed tests in HTML report when REQUIRE_PASS_TEST_LOGS=true

    This hook intercepts the test report creation and ensures that stdout/stderr
    are captured and displayed in the HTML report for all tests, not just failed ones.
    """
    outcome = yield
    report = outcome.get_result()

    # Check if pytest_html is available and if we should include logs for passed tests
    if pytest_html is None:
        return

    require_pass_logs = os.getenv("REQUIRE_PASS_TEST_LOGS", "false").lower() == "true"

    if require_pass_logs and report.when == "call" and report.passed:
        # For passed tests, add captured output to the HTML report
        extra = getattr(report, 'extra', [])

        # Capture stdout
        if hasattr(report, 'capstdout') and report.capstdout:
            extra.append(pytest_html.extras.text(report.capstdout, "Captured stdout"))

        # Capture stderr
        if hasattr(report, 'capstderr') and report.capstderr:
            extra.append(pytest_html.extras.text(report.capstderr, "Captured stderr"))

        # Capture log output
        if hasattr(report, 'caplog') and report.caplog:
            extra.append(pytest_html.extras.text(report.caplog, "Captured log"))

        report.extra = extra