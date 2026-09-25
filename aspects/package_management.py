#!/usr/bin/env python3
"""
Generic package management module for installing packages on containers.
Supports both RHEL-based and Debian-based distributions.
"""

import importlib.util as _ilu
import re as _re
from pathlib import Path as _Path

_nz_spec = _ilu.spec_from_file_location(
    "pep_version_normalize",
    str(_Path(__file__).resolve().parent.parent / "utillities" / "pep_version_normalize.py"))
_nz = _ilu.module_from_spec(_nz_spec)
_nz_spec.loader.exec_module(_nz)

# Pure install-decision/assertion module (owns assert_safe_version); imported by path.
_pv_spec = _ilu.spec_from_file_location(
    "pep_verify",
    str(_Path(__file__).resolve().parent.parent / "utillities" / "pep_verify.py"))
_pv = _ilu.module_from_spec(_pv_spec)
_pv_spec.loader.exec_module(_pv)

# Sibling module (same aspects/ dir) loaded BY PATH -- mirrors the _nz/_pv shims
# above. This repo has no package __init__, so `from aspects.configure_repository
# import ...` only resolves when the repo root happens to be on sys.path/PYTHONPATH;
# loading by path makes install_pinned's apt-lock dependency work whenever
# package_management.py itself is loadable (e.g. loaded by path in unit tests).
_cr_spec = _ilu.spec_from_file_location(
    "configure_repository",
    str(_Path(__file__).resolve().parent / "configure_repository.py"))
_cr = _ilu.module_from_spec(_cr_spec)
_cr_spec.loader.exec_module(_cr)


def install_package(container, package_name, pg_major_version=None, install_pg_server=False):
    """
    Install a package on a Docker container.

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to install
        pg_major_version: PostgreSQL major version (e.g., "17")
        install_pg_server: If True, also install PostgreSQL server on Debian platforms

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If package installation fails
    """

    # Allow package_name to be a comma-separated string or a list of package names
    if isinstance(package_name, str):
        packages = [p.strip() for p in package_name.split(',') if p.strip()]
    elif isinstance(package_name, (list, tuple)):
        packages = [str(p).strip() for p in package_name if str(p).strip()]
    else:
        raise Exception("package_name must be a string or list/tuple of package names")

    print(f"\n--- Installing packages: {', '.join(packages)} on container ---")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        pkg_mgr = "dnf install -y"
        platform = "rhel"
        # Expire cached repo metadata so newly-published packages are resolvable.
        # Without this, dnf resolves against stale cache and reports "No match"
        # for RPMs added to the repo after the cache was last refreshed.
        # (Mirrors the `apt-get update` refresh on the Debian path below.)
        container.exec_run(["/bin/sh", "-c", "dnf clean expire-cache"], user="root")
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if exit_code == 0:
            from aspects.configure_repository import _wait_for_apt_lock
            _wait_for_apt_lock(container)
            container.exec_run(["/bin/sh", "-c", "apt-get update"], user="root")
            pkg_mgr = "DEBIAN_FRONTEND=noninteractive apt-get install -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Install the packages sequentially
    for pkg in packages:
        print(f"Installing {pkg}...")
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"{pkg_mgr} {pkg}"],
            user="root"
        )

        if exit_code != 0:
            raise Exception(f"Failed to install {pkg}: {output.decode()}")

        print(f"\x05 Successfully installed {pkg}")

    # Install PostgreSQL server package for Debian if requested
    if platform == "debian" and install_pg_server and pg_major_version:
        server_package = f"pgedge-postgresql-{pg_major_version}"
        print(f"\nInstalling PostgreSQL server package: {server_package}...")
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"{pkg_mgr} {server_package}"],
            user="root"
        )
        if exit_code != 0:
            raise Exception(f"Failed to install {server_package}: {output.decode()}")

        print(f"\x05 Successfully installed {server_package}")
        message = f"Packages {', '.join(packages)} and {server_package} installed successfully on {platform}"
    else:
        message = f"Packages {', '.join(packages)} installed successfully on {platform}"

    return True, platform, message


def upgrade_package(container, package_name):
    """
    Upgrade a package on a Docker container.

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to upgrade

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If package upgrade fails
    """

    # Allow package_name to be a comma-separated string or a list of package names
    if isinstance(package_name, str):
        packages = [p.strip() for p in package_name.split(',') if p.strip()]
    elif isinstance(package_name, (list, tuple)):
        packages = [str(p).strip() for p in package_name if str(p).strip()]
    else:
        raise Exception("package_name must be a string or list/tuple of package names")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        pkg_mgr = "dnf upgrade -y"
        platform = "rhel"
        # Expire cached repo metadata so a newly-published version is resolvable
        # (mirrors the `apt-get update` refresh on the Debian path below).
        container.exec_run(["/bin/sh", "-c", "dnf clean expire-cache"], user="root")
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if exit_code == 0:
            container.exec_run(["/bin/sh", "-c", "apt-get update"], user="root")
            pkg_mgr = "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Upgrade the packages sequentially
    for pkg in packages:
        print(f"Upgrading {pkg}...")
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"{pkg_mgr} {pkg}"],
            user="root"
        )

        output_text = output.decode().lower()

        if exit_code != 0:
            raise Exception(f"Failed to upgrade {pkg}: {output.decode()}")

    # If we reach here for all packages without exception, prepare message
    message = f"Packages {', '.join(packages)} upgraded successfully on {platform}"
    print(f"✅ {message}")
    return True, platform, message


def uninstall_package(container, package_name):
    """
    Uninstall a package from a Docker container.

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to uninstall

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If package uninstallation fails
    """

    print(f"\n--- Uninstalling {package_name} from container ---")

    # Detect package manager
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        pkg_mgr = "dnf remove -y"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if exit_code == 0:
            pkg_mgr = "DEBIAN_FRONTEND=noninteractive apt-get purge -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Uninstall the package
    print(f"Uninstalling {package_name}...")
    exit_code, output = container.exec_run(
        ## Comment out the line due to not working on Debian-based systems. User not properly removed.
        #["/bin/sh", "-c", f"{pkg_mgr} {package_name}"],
        ["/bin/sh", "-c", f"{pkg_mgr} pgedge-*"],
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to uninstall {package_name}: {output.decode()}")

    print(f" Successfully uninstalled {package_name}")

    message = f"Package {package_name} uninstalled successfully from {platform}"
    return True, platform, message


def normalize_version(version_string, package_name=""):
    """
    Normalize a version string to handle beta versions with different formats.

    Beta normalization is only applied if:
    1. The version string contains "beta", OR
    2. The package name contains keywords: vectorizer, anonymizer, rag, mcp, nla

    Handles formats like:
    - 1.0-beta2, 1.0.0-beta1 (hyphen separator)
    - 1.0~beta2, 1.0.0~beta1 (Debian tilde separator for pre-releases)
    - 1.0beta2, 1.0.0beta2 (no separator)
    - 1.0, 1.0.0 (different precision)
    - 1.0.0-beta3.1.el9 (RPM VERSION-RELEASE with dist suffix)
    - 16.11-1.bullseye (Debian packaging suffix)
    - 1.0.0~beta2-1.trixie (Debian pre-release + packaging suffix)

    Args:
        version_string: Version string to normalize
        package_name: Optional package name to determine if beta logic should apply

    Returns:
        str: Normalized version string in format "1.0.0.beta2" (dots as separators) or "1.0.0" for non-beta
    """
    # Delegates to the shared normalizer (utillities/pep_version_normalize.py),
    # which handles RPM/deb packaging suffixes and folds the Debian pre-release
    # tilde (1.0.0~beta2 -> 1.0.0-beta2) so a deb-installed pre-release compares
    # equal to the hyphenated value from the config env files.
    return _nz.normalize_version(version_string, package_name)


def verify_package_version(container, package_name, expected_version):
    """
    Verify the installed version of a package.

    Supports beta versions with different formats:
    - 1.0-beta2, 1.0.0-beta1 (hyphen separator)
    - 1.0beta2, 1.0.0beta2 (no separator)
    - Different version precision (1.0 vs 1.0.0)

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to verify
        expected_version: Expected version string to match

    Returns:
        tuple: (success: bool, platform: str, installed_version: str, message: str)

    Raises:
        Exception: If version verification fails or version doesn't match
    """

    print(f"\n--- Verifying {package_name} version on container ---")
    print(f"Expected version: {expected_version}")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        # RHEL-based: use rpm to query version (include RELEASE for beta info)
        version_cmd = f"rpm -q --queryformat '%{{VERSION}}-%{{RELEASE}}' {package_name}"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if exit_code == 0:
            # Debian-based: use dpkg-query to get version
            version_cmd = f"dpkg-query --showformat='${{Version}}' --show {package_name}"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    # Get installed version
    exit_code, output = container.exec_run(["/bin/sh", "-c", version_cmd], user="root")

    if exit_code != 0:
        raise Exception(f"Failed to query {package_name} version: {output.decode()}")

    installed_version = output.decode().strip()
    print(f"Installed version: {installed_version}")

    # Normalize both versions for comparison to handle beta formats
    # Pass package_name to determine if beta logic should apply
    normalized_expected = normalize_version(expected_version, package_name)
    normalized_installed = normalize_version(installed_version, package_name)

    print(f"Normalized expected: {normalized_expected}")
    print(f"Normalized installed: {normalized_installed}")

    # Version comparison - check if normalized expected version is contained in normalized installed version
    if normalized_expected not in normalized_installed:
        raise Exception(
            f"Version mismatch for {package_name} on {platform}\n"
            f"Expected: {expected_version} (normalized: {normalized_expected})\n"
            f"Installed: {installed_version} (normalized: {normalized_installed})"
        )

    message = f"Version verified: {package_name} {installed_version} on {platform}"
    print(f" {message}")

    return True, platform, installed_version, message


def validate_bundled_file(container, file_path):
    """
    Validate that a bundled file exists in the container.

    Args:
        container: Docker container object with exec_run method
        file_path: Path to the file/directory to validate

    Returns:
        tuple: (success: bool, file_info: str, message: str)

    Raises:
        Exception: If file validation fails
    """

    print(f"\n--- Validating bundled file: {file_path} ---")

    # Check if file/directory exists
    exit_code, output = container.exec_run(
        f"test -e {file_path}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Bundled file/directory not found: {file_path}")

    # Get file type info
    exit_code, output = container.exec_run(
        f"ls -la {file_path}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to get file info for {file_path}: {output.decode()}")

    file_info = output.decode().strip()
    print(f"File info: {file_info}")

    message = f"Bundled file validated: {file_path}"
    print(f" {message}")

    return True, file_info, message



def install_pinned(container, package_name, exact_version):
    """Install an EXACT version (L2a) and return the SHA-256 of the package file that
    was installed. Every caller-derived value travels as one ARGUMENT-VECTOR element —
    never inside `sh -c` — so shell metacharacters in exact_version can never become
    commands; exact_version is also validated (allowlist) before any exec.

    Repository-freshness/error handling mirrors install_package so a just-published
    exact package is resolvable and a failed preparation never falls through to an
    install:
      * DNF: `dnf clean expire-cache` first; if that refresh fails, stop;
      * APT: wait out the apt/dpkg lock (a raised preparation failure stops before any
        refresh), then `apt-get update`; if that refresh fails, stop;
      * no supported package manager -> stop, nothing attempted.

    The install itself stays an ordinary repository install, so it keeps the signed
    repository's trust checks (APT's authenticated index; DNF's gpgcheck and repodata
    checksum) — a local-file install would bypass them. It is split so the bytes can
    be proven (see _install_verified): download the exact pin into the package
    manager's own cache, identify and hash the ONE cached file for it, then install
    the same pin from that cache only.

    Returns (success: bool, output: str, sha256: str | None) for OPERATIONAL outcomes;
    sha256 is the verified file's lowercase hex digest on success, else None.
    Programming/request errors are NOT converted to tuples: assert_safe_version raises
    UnsafeVersionError on an unsafe/malformed version and ValueError on an unsafe package
    name (and choose_install raises InstallDecisionError upstream on an inconsistent
    request); those propagate."""
    _pv.assert_safe_version(exact_version)          # RAISES UnsafeVersionError (programming/safety)
    if not (isinstance(package_name, str) and _PACKAGE_NAME.fullmatch(package_name)):
        raise ValueError("unsafe package name for a pinned install: %r" % (package_name,))
    ec, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")  # constant probe
    if ec == 0:
        # Expire cached repo metadata so a newly-published exact RPM resolves
        # (mirrors install_package's dnf refresh; constant command, no caller data).
        rec, rout = container.exec_run(["/bin/sh", "-c", "dnf clean expire-cache"], user="root")
        if rec != 0:
            return False, f"dnf clean expire-cache failed before pinned install: {_text(rout)}", None
        return _install_verified(container, "rpm", package_name, exact_version)
    ec, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
    if ec != 0:
        return False, "No supported package manager found (dnf or apt-get)", None
    # Preserve install_package's Debian preparation: wait out the apt/dpkg lock,
    # then refresh the index. Loaded by path (_cr) so it works without the repo
    # root on sys.path. A raised lock failure or a failed refresh must NOT fall
    # through to an install against a stale/incomplete index.
    try:
        _cr._wait_for_apt_lock(container)
    except Exception as exc:                     # operational: lock never freed / prep failed
        return False, f"apt/dpkg lock preparation failed before pinned install: {exc}", None
    uec, uout = container.exec_run(["apt-get", "update"], user="root")
    if uec != 0:
        return False, f"apt-get update failed before pinned install: {_text(uout)}", None
    return _install_verified(container, "deb", package_name, exact_version)


# --- verified pinned install ------------------------------------------------------
# Constant cache listings (no caller data): one line per cached package file,
# "<path>\t<name>\t<version>\t<arch>[\t<SHA256HEADER>]". The metadata comes from the
# file itself, never from its name. A file the package tool cannot read prints only its
# path, so it can never be selected.
_APT_LIST_CACHE = r"""eval "$(apt-config shell ARCHIVES Dir::Cache::archives/d)"
for f in "${ARCHIVES:-/var/cache/apt/archives/}"*.deb; do
  [ -f "$f" ] || continue
  printf '%s\t' "$f"
  dpkg-deb --showformat='${Package}\t${Version}\t${Architecture}\n' --show "$f" 2>/dev/null || echo
done"""
_DNF_LIST_CACHE = r"""find /var/cache/dnf -type f -name '*.rpm' 2>/dev/null | sort | while IFS= read -r f; do
  printf '%s\t' "$f"
  rpm -qp --nosignature --qf '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{SHA256HEADER}\n' "$f" 2>/dev/null || echo
done"""
_SHA256_HEX = _re.compile(r"[0-9a-f]{64}")
_ARCH_TOKEN = _re.compile(r"[a-z0-9_]+")
# A package name as both package managers spell one. It must start alphanumeric, so it
# can never be read as an option by rpm, dpkg-query or apt-cache.
_PACKAGE_NAME = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_DEB_ENV = {"DEBIAN_FRONTEND": "noninteractive"}


def _text(out):
    return out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)


def _installed_instances(container, family, package_name):
    """Installed instances of package_name as (version, arch, header) tuples, where
    header is the RPM SHA256HEADER ('' for DEB). [] only when the package manager says,
    in its exact words, that the package is not installed; None when the query failed or
    printed anything it cannot account for. So a query fault is never taken for "not
    installed", which would let a no-op install be attested."""
    if family == "rpm":
        ec, out = container.exec_run(
            ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\t%{ARCH}\t%{SHA256HEADER}\n", package_name],
            user="root")
        lines = [line for line in _text(out).splitlines() if line.strip()]
        if ec != 0:
            # rpm prints this same line when its database cannot be read; the
            # post-install SHA256HEADER tie still refuses a no-op of any other build.
            return [] if ec == 1 and lines == ["package %s is not installed" % package_name] else None
        rows = [line.split("\t") for line in lines]
        if not rows or any(len(r) != 3 or not all(r) for r in rows):
            return None
        return [tuple(r) for r in rows]
    ec, out = container.exec_run(
        ["dpkg-query", "-W", "-f", "${Status}\t${Version}\t${Architecture}\n", package_name],
        user="root")
    lines = [line for line in _text(out).splitlines() if line]
    if ec != 0:                                      # 1 = no such package; 2 = dpkg-query error
        return ([] if ec == 1 and [l.strip() for l in lines]
                == ["dpkg-query: no packages found matching %s" % package_name] else None)
    rows = [line.split("\t") for line in lines]
    if not rows or any(len(r) != 3 for r in rows):
        return None
    # "<want> ok installed": install, hold and deinstall all leave the package installed;
    # "unknown ok not-installed" (purged) and "... config-files" do not.
    installed = [(r[1], r[2], "") for r in rows if r[0].endswith(" ok installed")]
    if any(not v or not a for v, a, _ in installed):
        return None
    return installed


def _apt_index_digests(container, package_name, exact_version, arches):
    """The SHA256 values APT's authenticated index records for this exact pin (native or
    arch-independent), i.e. the digests APT will verify the cached file against; None
    when the index cannot be read."""
    ec, out = container.exec_run(["apt-cache", "show", f"{package_name}={exact_version}"], user="root")
    if ec != 0:
        return None
    digests = set()
    for record in _re.split(r"\n\s*\n", _text(out)):
        fields = {}
        for line in record.splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if (fields.get("Package") == package_name and fields.get("Version") == exact_version
                and fields.get("Architecture") in arches and fields.get("SHA256")):
            digests.add(fields["SHA256"].lower())
    return digests


def _install_verified(container, family, package_name, exact_version):
    """Download the exact pin into the package manager's cache, identify and hash the ONE
    cached file for it, install that pin from the cache only, and confirm the install
    used that file. Returns (ok, output, sha256|None); every refusal happens BEFORE the
    install unless it is a post-install tie failure.

    The package manager re-verifies each cached file against its repository metadata at
    install time and refuses changed bytes, and a cache-only install never fetches new
    ones. The tie from the hashed file to what is installed is then made explicit:
      * DEB: the hash must equal the one SHA256 the authenticated index records for the
        pin, which is what APT verifies the cached file against;
      * RPM: the installed package's SHA256HEADER must equal the hashed file's.
    A pin that is already installed is refused: its install would be a no-op, so no
    bytes of this run could be tied to it."""
    rpm = family == "rpm"
    spec = f"{package_name}-{exact_version}" if rpm else f"{package_name}={exact_version}"
    env = None if rpm else _DEB_ENV

    def fail(msg):
        return False, f"verified pinned install of {spec} refused: {msg}", None

    before = _installed_instances(container, family, package_name)
    if before is None:
        return fail("could not determine whether the package is already installed, so a no-op "
                    "install could not be ruled out")
    if any(inst[0] == exact_version for inst in before):
        return fail("that version is already installed, so an install would be a no-op and "
                    "the installed bytes could not be tied to a verified download")
    # A clean cache leaves only files this download fetched.
    ec, out = container.exec_run(["dnf", "clean", "packages"] if rpm else ["apt-get", "clean"], user="root")
    if ec != 0:
        return fail(f"could not clean the package cache: {_text(out)}")
    download = (["dnf", "install", "-y", "--downloadonly", spec] if rpm
                else ["apt-get", "install", "-y", "--download-only", spec])
    ec, out = container.exec_run(download, user="root", environment=env)
    if ec != 0:
        return fail(f"download failed: {_text(out)}")

    ec, out = container.exec_run(["rpm", "--eval", "%{_arch}"] if rpm else ["dpkg", "--print-architecture"],
                                 user="root")
    # Pick the one architecture token, so stray output (e.g. a sudo warning over SSH) is ignored.
    tokens = [line.strip() for line in _text(out).splitlines() if _ARCH_TOKEN.fullmatch(line.strip())]
    if ec != 0 or len(tokens) != 1:
        return fail(f"could not read the native architecture: {_text(out)!r}")
    native = tokens[0]
    arches = (native, "noarch" if rpm else "all")
    ec, out = container.exec_run(["/bin/sh", "-c", _DNF_LIST_CACHE if rpm else _APT_LIST_CACHE], user="root")
    if ec != 0:
        return fail(f"could not list the package cache: {_text(out)}")
    rows = [line.split("\t") for line in _text(out).splitlines()]
    matches = [r for r in rows if len(r) >= 4 and r[1] == package_name and r[2] == exact_version
               and r[3] in arches]
    if len(matches) != 1:
        return fail("expected exactly one cached file for %s %s (%s), found %d%s"
                    % (package_name, exact_version, "/".join(arches), len(matches),
                       "".join("\n  " + m[0] for m in matches)))
    path, file_arch = matches[0][0], matches[0][3]
    file_header = matches[0][4] if rpm and len(matches[0]) > 4 else ""
    if rpm and not _SHA256_HEX.fullmatch(file_header):
        return fail(f"cached file {path} has no SHA256 header digest to tie the install to")

    ec, out = container.exec_run(["sha256sum", path], user="root")
    digests = [w[0].lower() for w in (line.split() for line in _text(out).splitlines())
               if len(w) == 2 and w[1] == path and _SHA256_HEX.fullmatch(w[0].lower())]
    if ec != 0 or len(digests) != 1:
        return fail(f"could not hash {path}: {_text(out)}")
    digest = digests[0]
    if not rpm:
        index = _apt_index_digests(container, package_name, exact_version, arches)
        if index != {digest}:
            return fail(f"cached file {path} (sha256 {digest}) does not match the one digest the "
                        f"repository index records for the pin: {sorted(index or [])}")

    install = (["dnf", "-C", "install", "-y", spec] if rpm
               else ["apt-get", "install", "-y", "--no-download", spec])
    ec, out = container.exec_run(install, user="root", environment=env)
    install_out = _text(out)
    if ec != 0:
        return fail(f"cache-only install failed: {install_out}")
    installed = _installed_instances(container, family, package_name)
    if installed != [(exact_version, file_arch, file_header)]:
        return fail(f"the installed package {installed} cannot be tied to the verified file "
                    f"{path} ({exact_version} {file_arch}{' ' + file_header if rpm else ''})")
    return True, f"{install_out}\nverified {spec}: {path} sha256={digest}", digest


def query_installed_version(container, package_name):
    """Read-only: return the installed package-manager identity string — RPM
    VERSION-RELEASE or DEB Version — or None if not installed / unqueryable.
    (package_name is a known component package, not caller free-text; mirrors the
    query used by verify_package_version.)"""
    ec, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if ec == 0:
        cmd = f"rpm -q --queryformat '%{{VERSION}}-%{{RELEASE}}' {package_name}"
    else:
        ec, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if ec != 0:
            return None
        cmd = f"dpkg-query --showformat='${{Version}}' --show {package_name}"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], user="root")
    if ec != 0:
        return None
    return out.decode(errors="replace").strip() or None


def query_binary_version(container, binary_path):
    """Read-only: return the raw `<binary> -version` output (contains a 'Version:'
    line for a real tag build), or None if the binary can't be run."""
    ec, out = container.exec_run([binary_path, "-version"], user="root")
    if ec != 0:
        return None
    return out.decode(errors="replace").strip() or None
