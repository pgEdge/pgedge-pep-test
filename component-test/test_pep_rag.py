import os
import sys
import subprocess
import importlib.util as _ilu
from pathlib import Path
from datetime import datetime

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, file_management, container_management


def _load_util(name):
    """Load a utillities/ module by path (no package __init__ there), so these
    imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'utillities', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pep_request_env = _load_util('pep_request_env')
pep_verify = _load_util('pep_verify')
pep_evidence = _load_util('pep_evidence')

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract. It is None in standalone
# mode (marker unset), which keeps every legacy code path below unchanged; when it
# is set, the install decision and the consolidated identity test take over.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

# Load values from env
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

# Combine all containers with their type
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

def _effective_platform_filter(request, env_value):
    """Platform filter for container selection.

    In integration mode the request family is authoritative for the platform
    (rpm -> rhel containers, deb -> deb) and OVERRIDES any configured
    PLATFORM_FILTER, confining EVERY test — including the container-scoped
    prerequisite/repository steps — to the requested platform. Standalone uses the
    configured PLATFORM_FILTER env value unchanged.
    """
    if request is not None:
        return request["family"]                           # "rpm" | "deb"
    return (env_value or "").lower()


# Filter containers based on the effective platform filter.
platform_filter = _effective_platform_filter(INTEGRATION_REQUEST, os.getenv("PLATFORM_FILTER", ""))
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]
# If platform_filter is empty or "all", use all containers

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"

# RAG Components - standalone packages (no PG version suffix)
rhel_rag_components = [c.strip() for c in os.getenv("RAG_COMPONENTS", "").split(",") if c.strip()]
deb_rag_components = [c.strip() for c in os.getenv("DEB_RAG_COMPONENTS", "").split(",") if c.strip()]


def _integration_component_lists(request, rhel_cfg, deb_cfg):
    """Caller-authoritative RAG package/platform selection for integration mode.

    Standalone (request is None) returns the configured lists unchanged. In
    integration mode the request is authoritative for BOTH the physical package
    and the platform: the sole package under test is request["package_name"], and
    the family selects the platform (rpm -> rhel containers, deb -> deb). Collapsing
    the lists here — once, before the parametrization matrix is built — makes every
    package-scoped combination (install, bundled-files, SBOM, stripping, uninstall,
    identity) target only the caller's package on the requested family, so no
    product test can fall back to a config-named package or the wrong platform.
    """
    if request is None:
        return rhel_cfg, deb_cfg
    pkg = request["package_name"]
    if request["family"] == "rpm":
        return [pkg], []
    return [], [pkg]


# Apply the caller-authoritative selection once, so the matrix below (and every
# product test parametrized from it) is already scoped to the request in
# integration mode. Standalone leaves RAG_COMPONENTS / DEB_RAG_COMPONENTS intact.
rhel_rag_components, deb_rag_components = _integration_component_lists(
    INTEGRATION_REQUEST, rhel_rag_components, deb_rag_components)

# RAG Component versions
rag_server_version = os.getenv("PGEDGE_RAG_SERVER_VERSION", "")

# Keyed off the configured component list rather than hardcoded names, so
# switching RAG_COMPONENTS / DEB_RAG_COMPONENTS between pgedge-rag-server and
# pgedge-rag-server2 needs no edit here. Hardcoding "pgedge-rag-server"
# previously meant a server2 run silently skipped the package-version,
# binary-version and binary-stripped checks — the map lookups missed, and each
# of those tests skips on an empty value.
_rag_components_all = list(dict.fromkeys(rhel_rag_components + deb_rag_components))

# pgedge-rag-server and pgedge-rag-server2 are the same component and share
# PGEDGE_RAG_SERVER_VERSION. Give a future RAG package that versions
# independently its own env var and list it here.
_rag_version_overrides = {}

# Version mapping for RAG components
rag_version_map = {
    component: _rag_version_overrides.get(component, rag_server_version)
    for component in _rag_components_all
}

# Decoupled components SBOM path (fallback only — see below)
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# The two RAG packages do NOT share a filesystem layout:
#
#   pgedge-rag-server   binary /usr/bin/pgedge-rag-server
#                       sbom   /usr/share/pgedge-rag-server/
#   pgedge-rag-server2  binary /usr/pgedge/rag-server2/bin/pgedge-rag-server
#                       sbom   /usr/pgedge/rag-server2/sbom/
#
# Note server2's binary basename is still 'pgedge-rag-server' — only the
# directory carries the '2'. Any rule derived from the package name alone gets
# this wrong, so the paths are read out of the expected-output file for the
# package instead. That file is already the authority on what the package
# ships, and test_verify_bundled_files checks it against the real install, so a
# stale entry fails loudly there rather than silently mistargeting these tests.


def _expected_output_entries(component, container_type):
    """Return the recorded file list for a package, or [] if there is none.

    Mirrors the expected-output name derivation in file_management so both look
    up the same file: strip the 'pgedge-' prefix and any trailing _<PG> suffix.
    """
    platform_dir = "rpm" if container_type == "rhel" else "deb"
    base_name = component.replace("pgedge-", "").rsplit('_', 1)[0]
    path = Path(__file__).parent.parent / "expected-output" / platform_dir / base_name
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def get_rag_binary(component, container_type):
    """Resolve the installed binary path for a RAG package.

    Falls back to the flat /usr/bin/<package> layout when the package has no
    expected-output file to read.
    """
    for entry in _expected_output_entries(component, container_type):
        if "/bin/" in entry and os.path.basename(entry).startswith("pgedge-rag"):
            return entry
    return f"/usr/bin/{component}"


def get_rag_sbom(component, container_type):
    """Resolve (sbom_directory, sbom_filename) for a RAG package.

    The filename is read rather than assumed: server2 ships its SBOM as
    pgedge-rag-server-sbom.json despite being the server2 package.
    """
    for entry in _expected_output_entries(component, container_type):
        if entry.endswith("-sbom.json"):
            return os.path.dirname(entry), os.path.basename(entry)
    return f"{decoupled_sbom_path}/{component}", "pgedge-rag-server-sbom.json"


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "rag_components": rhel_rag_components,
        }
    else:  # deb
        return {
            "rag_components": deb_rag_components,
        }


def get_rag_components_for_container(container_type):
    """Get list of RAG components for the container type"""
    config = get_container_config(container_type)
    return config["rag_components"]


# Generate all container-component combinations for parametrization
def generate_container_component_combinations():
    """Generate all combinations of containers and their RAG components"""
    combinations = []
    for container_name, container_type in all_containers:
        components = get_rag_components_for_container(container_type)
        for component in components:
            combinations.append((container_name, container_type, component))
    return combinations


all_container_component_combinations = generate_container_component_combinations()


# ============================================================================
# Test Functions
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_prerequisites(container_name, container_type):
    """Step 0: Install prerequisites using machine_prereq_setup module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Ensure container exists and is running - create if not available
    container, created, message = container_management.ensure_container_running(
        client, container_name, container_type
    )
    print(f"{'🆕 ' if created else ''}{message}")

    assert container.status == "running", f"Container {container_name} is not running (status: {container.status})"

    print(f"\n--- Installing prerequisites on {container_name} ({container_type}) ---")

    # Use the machine_prereq_setup module
    try:
        success, os_info, message = machine_prereq_setup.install_prerequisites_on_container(container)
        assert success, f"Prerequisite installation failed: {message}"
        print(f"✅ Prerequisite installation completed on {container_name} ({os_info})")
        print(f"   {message}")
    except Exception as e:
        pytest.fail(f"Failed to install prerequisites: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_repository(container_name, container_type):
    """Step 1: Configure the repository file using configure_repository module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Configuring repository in {container_name} ---")

    # Use the configure_repository module
    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_component_install(container_name, container_type, component):
    """Step 2: Install RAG component using package_management module"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {component} on {container_name} ({container_type}) ---")

    try:
        if INTEGRATION_REQUEST is not None:
            # Integration mode: install per the request's decision.
            req = INTEGRATION_REQUEST
            kind, exact = pep_verify.choose_install(req)
            if kind == "pinned":
                # EXACT / L2a -- assert the result so a failed exact-version install
                # fails loudly here instead of being papered over by identity later.
                ok, out = package_management.install_pinned(container, req["package_name"], exact)
                assert ok, f"pinned install of {req['package_name']}={exact} failed: {out}"
                print(f"✅ pinned install: {req['package_name']}={exact}")
            else:
                # L1 degraded path -- latest, not pinned. Install the REQUEST
                # package (caller-authoritative), never the parametrized config
                # component, so an L1 run cannot install a config-named package.
                ok, platform, message = package_management.install_package(container, req["package_name"])
                assert ok, f"Package installation failed: {message}"
                print(f"✅ {message} (L1/latest -- degraded, not pinned)")
            # Record the install scope marker AFTER a successful install: binds this
            # run + target so the identity test can enforce install-before-identity
            # (a failed install raises above, so no marker is written -> identity fails).
            install_out = os.getenv("PEP_INSTALL_OUT", "test-logs/install-evidence.json")
            pep_evidence.write_install_evidence(
                req, os.environ.get("PEP_RUN_TOKEN", ""), kind, exact, install_out)
        else:
            # Standalone mode: unchanged legacy behavior.
            success, platform, message = package_management.install_package(container, component)
            assert success, f"Package installation failed: {message}"
            print(f"✅ {message}")
            print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {component}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_component_upgrade(container_name, container_type, component):
    """Upgrade RAG component if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {component} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, component)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{component} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {component}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_component_version(container_name, container_type, component):
    """Step 3: Check the RAG component version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_rag_identity")
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    # Get expected version for this component
    expected_version = rag_version_map.get(component, "")
    if not expected_version:
        pytest.skip(f"No version defined for {component} in env, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {component} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, component, expected_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_verify_bundled_files(container_name, container_type, component):
    """Verify bundled files for each RAG component match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=component,
            package_name=component,
            project_root=project_root
        )

        # If verification failed, fail the test with details
        if not success:
            # Format details for display
            details_str = ""
            if details:
                if "missing_files" in details and details["missing_files"]:
                    details_str += f"\n\nMissing files ({len(details['missing_files'])}):\n"
                    for file in details["missing_files"]:
                        details_str += f"  - {file}\n"
                if "extra_files" in details and details["extra_files"]:
                    details_str += f"\nExtra files ({len(details['extra_files'])}):\n"
                    for file in details["extra_files"]:
                        details_str += f"  + {file}\n"
            pytest.fail(f"{message}{details_str}")

    except Exception as e:
        # Handle cases like missing expected files
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_verify_sbom(container_name, container_type, component):
    """Verify SBOM signature files located under the decoupled components SBOM directory

    Parametrized per component: each RAG package ships its SBOM under
    /usr/share/<package>/, so the directory has to follow the configured
    package name rather than a fixed pgedge-rag-server.
    """
    container_name = container_name.strip()
    component = component.strip()
    if not container_name or not component:
        pytest.skip("Invalid container or component")

    sbom_dir, rag_sbom_basename = get_rag_sbom(component, container_type)
    if not sbom_dir:
        pytest.skip(f"No SBOM path resolved for {component}, skipping SBOM verification")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"


    if container_type == "rhel":
        print(f"\n--- Verifying SBOM on {container_name} (RHEL) in {sbom_dir} ---")

        # Download pgedge-rsa.pub signing key into the sbom directory
        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        # Verify SBOM signature
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {rag_sbom_basename}.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{rag_sbom_basename}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 authenticated signature." in output_str, \
            f"Expected '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (RHEL)")
        print(f"   {output_str.strip()}")

    else:  # deb
        print(f"\n--- Verifying SBOM on {container_name} (Deb) in {sbom_dir} ---")

        # Verify SBOM signature using the distro keyring
        # Detect sq signer flag (older sq uses --signer-cert, newer uses --signer-file)
        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {rag_sbom_basename}.asc "
            f"{rag_sbom_basename}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_binary_version(container_name, container_type, component):
    """Verify RAG binary -version output matches expected version"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_rag_identity")
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    binary_path = get_rag_binary(component, container_type)
    if not binary_path:
        pytest.skip(f"No binary path defined for {component}")

    expected_version = rag_version_map.get(component, "")
    if not expected_version:
        pytest.skip(f"No version defined for {component} in env, skipping binary version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {component} binary version on {container_name} ({container_type}) ---")

    try:
        exit_code, output = container.exec_run(
            [binary_path, "-version"],
            user="root"
        )
        output_text = output.decode().strip()
        print(f"Binary output:\n{output_text}")

        assert exit_code == 0, f"Binary {binary_path} -version returned exit code {exit_code}: {output_text}"

        # Parse the Version line from the output
        actual_version = None
        for line in output_text.splitlines():
            if "Version:" in line:
                actual_version = line.split("Version:")[-1].strip()
                break

        assert actual_version is not None, f"Could not find 'Version:' in binary output:\n{output_text}"
        assert actual_version == expected_version, (
            f"Binary version mismatch for {component}: "
            f"expected '{expected_version}', got '{actual_version}'"
        )
        print(f"✅ Binary version matches: {actual_version}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} binary version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_identity(container_name, container_type, component):
    """Integration mode: one consolidated identity verdict. Gather every
    observation in one place (so no test marks another's unobserved rung), persist
    the evidence, then fail on any unmet attemptable rung. Skipped in standalone
    mode, where the legacy version/binary tests above cover identity instead."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container_name = container_name.strip()
    component = component.strip()
    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    identity_out = os.getenv("PEP_IDENTITY_OUT", "test-logs/identity-evidence.json")

    # Install-before-identity precondition: trust identity only if THIS run installed
    # THIS target (scope marker written by test_rag_component_install). On any failure
    # -- absent/stale/mismatched marker -- persist schema-valid zero-observation
    # identity evidence (L2 rungs may be not_proven while L1 is not_attempted, since
    # identity was never queried), so the run reports a truthful completed/fail rather
    # than a masked infra_failure, and fail WITHOUT querying identity, so no
    # post-replacement state can be certified.
    install_out = os.getenv("PEP_INSTALL_OUT", "test-logs/install-evidence.json")
    run_token = os.environ.get("PEP_RUN_TOKEN", "")
    pre = pep_evidence.install_precondition_problems(
        pep_evidence.load_json_object(install_out), req, run_token)
    if pre:
        reason = "; ".join(pre)
        pep_evidence.record_precondition_failure(req, identity_out)
        pytest.fail(f"install-before-identity precondition failed: {reason}")

    fam = req["family"]
    # Gather ALL observations together (read-only helpers from package_management).
    pm_ver = package_management.query_installed_version(container, req["package_name"])
    # Caller-authoritative: resolve the binary for the REQUEST package (not the
    # config-parametrized component) via upstream's fixture-derived helper, using
    # the actual container type so the right expected-output file is read.
    binary_path = get_rag_binary(req["package_name"], container_type)
    binary_missing = not binary_path
    bin_ver = None if binary_missing else package_management.query_binary_version(container, binary_path)
    observed = {
        "rpm": pm_ver if fam == "rpm" else None,
        "deb": pm_ver if fam == "deb" else None,
        "binary": bin_ver,
        "component_version": pm_ver,
    }

    # record_identity_verdict persists the evidence BEFORE returning problems, so a
    # failing assertion below never loses it. It also writes an audit-only
    # observed-identity.json (the actual package/binary observed) to PEP_OBSERVED_OUT
    # -- a SEPARATE file that never affects the verdict; identity_out stays strict.
    observed_out = os.getenv("PEP_OBSERVED_OUT", "test-logs/observed-identity.json")
    ev, problems = pep_evidence.record_identity_verdict(
        observed, req, identity_out, binary_missing=binary_missing,
        run_token=run_token, observed_out=observed_out)
    print(f"identity evidence ({component}): {ev}")

    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_binary_stripped(container_name, container_type, component):
    """Verify RAG binary is stripped (debugging symbols removed)"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    binary_path = get_rag_binary(component, container_type)
    if not binary_path:
        pytest.skip(f"No binary path defined for {component}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Extract directory and binary name from full path
    binary_dir = os.path.dirname(binary_path)
    binary_name = os.path.basename(binary_path)

    print(f"\n--- Verifying {component} binary is stripped on {container_name} ({container_type}) ---")
    print(f"Binary: {binary_path}")

    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path=binary_dir,
            container_name=container_name,
            binary_names=binary_name
        )

        print(f"Total binaries checked: {details['total_binaries']}")
        print(f"Stripped binaries: {details['stripped_binaries']}")

        if not success:
            print(f"⚠️ Unstripped binaries found: {len(details['unstripped_binaries'])}")
            for binary in details['unstripped_binaries']:
                print(f"  - {binary}")

        assert success, f"Binary stripping verification failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} binary is stripped: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_component_uninstall(container_name, container_type, component):
    """Uninstall RAG component using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {component} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, component)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {component}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=None  # RAG components don't use pgdata/pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")