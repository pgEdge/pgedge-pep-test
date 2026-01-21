#!/usr/bin/env python3
"""
Spock Build Automation Script
Automates the process of building PostgreSQL 17.7 with Spock extension from source.

Supports both automated download and local source paths.

Usage:
    # Download everything automatically
    python build_spock.py --install-dir /opt/pgedge --verbose
    
    # Use local PostgreSQL source and Spock patches
    python build_spock.py --install-dir /opt/pgedge --pg-source /path/to/postgresql-17.7 --patches-dir /path/to/spock_pg17_patches --verbose
"""

import os
import sys
import subprocess
import urllib.request
import tarfile
import re
import argparse
import shutil
from pathlib import Path
from typing import List, Tuple, Optional

class SpockBuilder:
    def __init__(self, install_dir: str, work_dir: str = None, verbose: bool = False,
                 pg_source_dir: str = None, patches_dir: str = None):
        self.install_dir = Path(install_dir).resolve()
        self.work_dir = Path(work_dir).resolve() if work_dir else Path.cwd() / "spock_build"
        self.verbose = verbose
        
        # User-provided paths
        self.user_pg_source_dir = Path(pg_source_dir).resolve() if pg_source_dir else None
        self.user_patches_dir = Path(patches_dir).resolve() if patches_dir else None
        
        # PostgreSQL settings
        self.pg_version = "17.7"
        self.pg_url = f"https://ftp.postgresql.org/pub/source/v{self.pg_version}/postgresql-{self.pg_version}.tar.bz2"
        
        # Determine PostgreSQL source directory
        if self.user_pg_source_dir:
            self.pg_source_dir = self.user_pg_source_dir
        else:
            self.pg_source_dir = self.work_dir / f"postgresql-{self.pg_version}"
        
        # Spock settings
        self.spock_repo = "https://github.com/pgEdge/spock.git"
        self.spock_dir = self.work_dir / "spock"
        self.spock_patches_base_url = "https://raw.githubusercontent.com/pgEdge/spock/main/patches/17"
        self.spock_patches_html_url = "https://github.com/pgEdge/spock/tree/main/patches/17"
        
        # Build directories
        self.pg_install_dir = self.install_dir / "pg17"
        self.pg_config_path = self.pg_install_dir / "bin" / "pg_config"
        
    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        
    def run_command(self, cmd: List[str], cwd: str = None, env: dict = None) -> Tuple[int, str, str]:
        """Run a shell command and return exit code, stdout, stderr"""
        if self.verbose:
            self.log(f"Running: {' '.join(cmd)}")
            
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True
        )
        
        if self.verbose and result.stdout:
            self.log(f"STDOUT: {result.stdout}")
        if result.stderr:
            self.log(f"STDERR: {result.stderr}", "WARN" if result.returncode == 0 else "ERROR")
            
        return result.returncode, result.stdout, result.stderr
    
    def download_file(self, url: str, dest: Path) -> bool:
        """Download a file from URL to destination"""
        self.log(f"Downloading {url} to {dest}")
        try:
            # Add headers to avoid rate limiting
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req) as response:
                with open(dest, 'wb') as out_file:
                    out_file.write(response.read())
            self.log(f"Successfully downloaded to {dest}")
            return True
        except Exception as e:
            self.log(f"Failed to download {url}: {e}", "ERROR")
            return False
    
    def extract_tarball(self, tarball: Path, dest_dir: Path) -> bool:
        """Extract a tar.bz2 file"""
        self.log(f"Extracting {tarball} to {dest_dir}")
        try:
            with tarfile.open(tarball, 'r:bz2') as tar:
                # Extract with filter to avoid security warnings
                if sys.version_info >= (3, 12):
                    tar.extractall(dest_dir, filter='data')
                else:
                    tar.extractall(dest_dir)
            self.log(f"Successfully extracted to {dest_dir}")
            return True
        except Exception as e:
            self.log(f"Failed to extract {tarball}: {e}", "ERROR")
            return False
    
    def validate_pg_source_dir(self, pg_dir: Path) -> bool:
        """Validate that the directory contains PostgreSQL source"""
        self.log(f"Validating PostgreSQL source directory: {pg_dir}")
        
        if not pg_dir.exists():
            self.log(f"Directory does not exist: {pg_dir}", "ERROR")
            return False
        
        if not pg_dir.is_dir():
            self.log(f"Path is not a directory: {pg_dir}", "ERROR")
            return False
        
        # Check for key PostgreSQL files
        required_files = ["configure", "src/backend/main/main.c", "src/include/postgres.h"]
        for file_path in required_files:
            if not (pg_dir / file_path).exists():
                self.log(f"Missing required file: {file_path}", "ERROR")
                self.log(f"This does not appear to be a valid PostgreSQL source directory", "ERROR")
                return False
        
        self.log(f"PostgreSQL source directory validated successfully")
        return True
    
    def validate_patches_dir(self, patches_dir: Path) -> bool:
        """Validate that the directory contains patch files (.patch or .diff)"""
        self.log(f"Validating patches directory: {patches_dir}")
        
        if not patches_dir.exists():
            self.log(f"Directory does not exist: {patches_dir}", "ERROR")
            return False
        
        if not patches_dir.is_dir():
            self.log(f"Path is not a directory: {patches_dir}", "ERROR")
            return False
        
        # Check for .patch or .diff files
        patch_files = list(patches_dir.glob("*.patch")) + list(patches_dir.glob("*.diff"))
        if not patch_files:
            self.log(f"No .patch or .diff files found in: {patches_dir}", "ERROR")
            self.log(f"Contents of directory:", "ERROR")
            try:
                for item in patches_dir.iterdir():
                    self.log(f"  - {item.name}", "ERROR")
            except:
                pass
            return False
        
        self.log(f"Found {len(patch_files)} patch/diff files in directory")
        return True
    
    def get_patches_from_local_dir(self, patches_dir: Path) -> List[str]:
        """Get list of patch/diff files from local directory"""
        self.log(f"Getting patch list from local directory: {patches_dir}")
        
        patch_files = []
        # Look for both .patch and .diff extensions
        for pattern in ["*.patch", "*.diff"]:
            for patch_file in patches_dir.glob(pattern):
                patch_files.append(patch_file.name)
        
        # Sort patches by numerical prefix (e.g., pg17-025, pg17-030, etc.)
        def get_patch_number(filename: str) -> int:
            # Extract number from patterns like pg17-025-xxx.diff
            match = re.search(r'pg17-(\d+)', filename)
            if match:
                return int(match.group(1))
            # Fallback to any number
            match = re.search(r'(\d+)', filename)
            return int(match.group(1)) if match else 999
        
        patch_files.sort(key=get_patch_number)
        
        self.log(f"Found {len(patch_files)} patches (sorted by number): {patch_files}")
        return patch_files
    
    def get_spock_patches_from_html(self) -> List[str]:
        """Scrape patch filenames from GitHub HTML page"""
        self.log("Fetching patch list from GitHub HTML page")
        try:
            req = urllib.request.Request(
                self.spock_patches_html_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req) as response:
                html = response.read().decode('utf-8')
            
            # Find all .patch and .diff files in the HTML
            patch_pattern = r'href="[^"]*\/([^"\/]+\.(?:patch|diff))"'
            matches = re.findall(patch_pattern, html)
            
            patches = []
            for filename in matches:
                if filename.endswith(('.patch', '.diff')):
                    patches.append(filename)
            
            # Remove duplicates and sort
            patches = list(set(patches))
            
            # Sort patches by numerical prefix
            def get_patch_number(filename: str) -> int:
                match = re.search(r'pg17-(\d+)', filename)
                if match:
                    return int(match.group(1))
                match = re.search(r'(\d+)', filename)
                return int(match.group(1)) if match else 999
            
            patches.sort(key=get_patch_number)
            
            self.log(f"Found {len(patches)} patches from HTML: {patches}")
            return patches
            
        except Exception as e:
            self.log(f"Failed to scrape patch list from HTML: {e}", "ERROR")
            return []
    
    def get_spock_patches_from_api(self) -> List[str]:
        """Fetch list of patch files from GitHub API for PG 17"""
        import json
        
        api_url = "https://api.github.com/repos/pgEdge/spock/contents/patches/17"
        
        self.log("Fetching patch list from GitHub API")
        try:
            req = urllib.request.Request(
                api_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
            
            patches = []
            for item in data:
                if isinstance(item, dict):
                    name = item.get('name', '')
                    if name.endswith(('.patch', '.diff')):
                        patches.append(name)
            
            # Sort patches by numerical prefix
            def get_patch_number(filename: str) -> int:
                match = re.search(r'pg17-(\d+)', filename)
                if match:
                    return int(match.group(1))
                match = re.search(r'(\d+)', filename)
                return int(match.group(1)) if match else 999
            
            patches.sort(key=get_patch_number)
            
            self.log(f"Found {len(patches)} patches from API: {patches}")
            return patches
            
        except Exception as e:
            self.log(f"Failed to fetch patch list from API: {e}", "WARN")
            return []
    
    def get_spock_patches(self) -> List[str]:
        """Fetch list of patch files, trying API first then HTML scraping"""
        # If user provided patches directory, use it
        if self.user_patches_dir:
            return self.get_patches_from_local_dir(self.user_patches_dir)
        
        # Otherwise, try to download from GitHub
        patches = self.get_spock_patches_from_api()
        
        if not patches:
            self.log("API method failed, trying HTML scraping")
            patches = self.get_spock_patches_from_html()
        
        return patches
    
    def download_patches(self, patches: List[str]) -> bool:
        """Download all patches to work directory"""
        # If user provided patches directory, just copy them
        if self.user_patches_dir:
            self.log(f"Using patches from local directory: {self.user_patches_dir}")
            return True
        
        # Otherwise download
        patches_dir = self.work_dir / "patches"
        patches_dir.mkdir(parents=True, exist_ok=True)
        
        for patch in patches:
            patch_url = f"{self.spock_patches_base_url}/{patch}"
            patch_file = patches_dir / patch
            
            if patch_file.exists():
                self.log(f"Patch already exists: {patch}")
                continue
            
            if not self.download_file(patch_url, patch_file):
                return False
                
        return True
    
    def apply_patches(self, patches: List[str]) -> bool:
        """Apply patches to PostgreSQL source in numerical order"""
        # Determine patches directory
        if self.user_patches_dir:
            patches_dir = self.user_patches_dir
        else:
            patches_dir = self.work_dir / "patches"
        
        self.log("Applying patches to PostgreSQL source in order:")
        for i, patch in enumerate(patches, 1):
            patch_file = patches_dir / patch
            
            if not patch_file.exists():
                self.log(f"Patch file not found: {patch_file}", "ERROR")
                return False
                
            self.log(f"[{i}/{len(patches)}] Applying patch: {patch}")
            
            # Use patch -p1 < patch_file
            with open(patch_file, 'r') as pf:
                result = subprocess.run(
                    ["patch", "-p1"],
                    stdin=pf,
                    cwd=str(self.pg_source_dir),
                    capture_output=True,
                    text=True
                )
                
                if self.verbose and result.stdout:
                    self.log(f"STDOUT: {result.stdout}")
                if result.stderr:
                    self.log(f"STDERR: {result.stderr}", "WARN" if result.returncode == 0 else "ERROR")
                
                if result.returncode != 0:
                    self.log(f"Failed to apply patch {patch}", "ERROR")
                    return False
                
        self.log("All patches applied successfully")
        return True
    
    def step1_download_postgresql(self) -> bool:
        """Step 1: Download PostgreSQL source (or use provided path)"""
        self.log("=" * 80)
        self.log("STEP 1: Setting up PostgreSQL source")
        self.log("=" * 80)
        
        # If user provided PostgreSQL source directory, validate and use it
        if self.user_pg_source_dir:
            self.log(f"Using user-provided PostgreSQL source: {self.user_pg_source_dir}")
            if not self.validate_pg_source_dir(self.user_pg_source_dir):
                return False
            self.log("PostgreSQL source directory validated successfully")
            return True
        
        # Otherwise, download PostgreSQL
        self.log("No PostgreSQL source provided, will download")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        
        pg_tarball = self.work_dir / f"postgresql-{self.pg_version}.tar.bz2"
        
        if pg_tarball.exists():
            self.log(f"PostgreSQL tarball already exists at {pg_tarball}")
        else:
            if not self.download_file(self.pg_url, pg_tarball):
                return False
        
        if self.pg_source_dir.exists():
            self.log(f"PostgreSQL source already extracted at {self.pg_source_dir}")
        else:
            if not self.extract_tarball(pg_tarball, self.work_dir):
                return False
                
        return True
    
    def step2_apply_spock_patches(self) -> bool:
        """Step 2: Download and apply Spock patches (or use provided path)"""
        self.log("=" * 80)
        self.log("STEP 2: Setting up and applying Spock patches")
        self.log("=" * 80)
        
        # If user provided patches directory, validate it
        if self.user_patches_dir:
            self.log(f"Using user-provided patches directory: {self.user_patches_dir}")
            if not self.validate_patches_dir(self.user_patches_dir):
                return False
        
        patches = self.get_spock_patches()
        if not patches:
            self.log("No patches found or failed to fetch patch list", "ERROR")
            self.log("Please check your internet connection and GitHub access", "ERROR")
            return False
        
        if not self.download_patches(patches):
            return False
            
        if not self.apply_patches(patches):
            return False
            
        return True
    
    def step3_configure_postgresql(self) -> bool:
        """Step 3: Configure PostgreSQL"""
        self.log("=" * 80)
        self.log("STEP 3: Configuring PostgreSQL")
        self.log("=" * 80)
        
        configure_cmd = [
            "./configure",
            f"--prefix={self.pg_install_dir}",
            "--with-openssl",
            "--with-libxml",
            "--with-libxslt"
        ]
        
        returncode, stdout, stderr = self.run_command(
            configure_cmd,
            cwd=str(self.pg_source_dir)
        )
        
        if returncode != 0:
            self.log("Configure failed", "ERROR")
            return False
            
        self.log("PostgreSQL configured successfully")
        return True
    
    def step4_build_postgresql(self) -> bool:
        """Step 4: Build PostgreSQL"""
        self.log("=" * 80)
        self.log("STEP 4: Building PostgreSQL (this may take several minutes)")
        self.log("=" * 80)
        
        try:
            import multiprocessing
            num_cores = multiprocessing.cpu_count()
        except:
            num_cores = 4
        
        returncode, stdout, stderr = self.run_command(
            ["make", "-j", str(num_cores)],
            cwd=str(self.pg_source_dir)
        )
        
        if returncode != 0:
            self.log("Build failed", "ERROR")
            return False
            
        self.log("PostgreSQL built successfully")
        return True
    
    def step5_install_postgresql(self) -> bool:
        """Step 5: Install PostgreSQL"""
        self.log("=" * 80)
        self.log("STEP 5: Installing PostgreSQL")
        self.log("=" * 80)
        
        returncode, stdout, stderr = self.run_command(
            ["make", "install"],
            cwd=str(self.pg_source_dir)
        )
        
        if returncode != 0:
            self.log("Installation failed", "ERROR")
            return False
            
        self.log(f"PostgreSQL installed successfully to {self.pg_install_dir}")
        
        if not self.pg_config_path.exists():
            self.log(f"pg_config not found at {self.pg_config_path}", "ERROR")
            return False
            
        return True
    
    def step6_clone_spock(self) -> bool:
        """Step 6: Clone Spock repository"""
        self.log("=" * 80)
        self.log("STEP 6: Cloning Spock repository")
        self.log("=" * 80)
        
        if self.spock_dir.exists():
            self.log(f"Spock directory already exists at {self.spock_dir}, removing...")
            shutil.rmtree(self.spock_dir)
        
        returncode, stdout, stderr = self.run_command(
            ["git", "clone", self.spock_repo, str(self.spock_dir)]
        )
        
        if returncode != 0:
            self.log("Failed to clone Spock repository", "ERROR")
            return False
            
        self.log("Spock repository cloned successfully")
        return True
    
    def step7_build_spock(self) -> bool:
        """Step 7: Build and install Spock extension"""
        self.log("=" * 80)
        self.log("STEP 7: Building and installing Spock extension")
        self.log("=" * 80)
        
        env = os.environ.copy()
        env['PATH'] = f"{self.pg_install_dir / 'bin'}:{env['PATH']}"
        env['PG_CONFIG'] = str(self.pg_config_path)
        
        self.log("Running make for Spock")
        returncode, stdout, stderr = self.run_command(
            ["make"],
            cwd=str(self.spock_dir),
            env=env
        )
        
        if returncode != 0:
            self.log("Spock build failed", "ERROR")
            return False
        
        self.log("Running make install for Spock")
        returncode, stdout, stderr = self.run_command(
            ["make", "install"],
            cwd=str(self.spock_dir),
            env=env
        )
        
        if returncode != 0:
            self.log("Spock installation failed", "ERROR")
            return False
            
        self.log("Spock extension built and installed successfully")
        return True
    
    def step8_create_postgresql_conf_snippet(self) -> bool:
        """Step 8: Create postgresql.conf snippet"""
        self.log("=" * 80)
        self.log("STEP 8: Creating postgresql.conf configuration snippet")
        self.log("=" * 80)
        
        conf_snippet = """
# Spock Extension Configuration
# Add these lines to your postgresql.conf file

shared_preload_libraries = 'spock'
track_commit_timestamp = on  # needed for conflict resolution

# Optional: Additional recommended settings for Spock
# wal_level = 'logical'
# max_worker_processes = 10
# max_replication_slots = 10
# max_wal_senders = 10
"""
        
        snippet_file = self.work_dir / "spock_postgresql.conf.snippet"
        snippet_file.write_text(conf_snippet)
        
        self.log(f"Configuration snippet saved to: {snippet_file}")
        self.log("")
        self.log("IMPORTANT: Add the following to your postgresql.conf:")
        self.log(conf_snippet)
        
        return True
    
    def create_initialization_script(self) -> bool:
        """Create a helper script for initializing PostgreSQL with Spock"""
        self.log("=" * 80)
        self.log("Creating initialization helper script")
        self.log("=" * 80)
        
        init_script = f"""#!/bin/bash
# Spock Initialization Script
# Generated by build_spock.py

export PATH={self.pg_install_dir}/bin:$PATH
export PGDATA={self.install_dir}/data

echo "Initializing PostgreSQL cluster..."
initdb -D $PGDATA

echo "Updating postgresql.conf with Spock settings..."
cat >> $PGDATA/postgresql.conf << EOF

# Spock Extension Configuration
shared_preload_libraries = 'spock'
track_commit_timestamp = on
wal_level = 'logical'
max_worker_processes = 10
max_replication_slots = 10
max_wal_senders = 10
EOF

echo "Starting PostgreSQL..."
pg_ctl -D $PGDATA -l $PGDATA/logfile start

sleep 3

echo "Creating Spock extension..."
createdb pgedge
psql -d pgedge -c "CREATE EXTENSION spock;"

echo ""
echo "Spock setup complete!"
echo "PostgreSQL is running on default port 5432"
echo "Database: pgedge"
echo "To connect: psql -d pgedge"
echo ""
echo "To stop: pg_ctl -D $PGDATA stop"
echo "To start: pg_ctl -D $PGDATA start"
"""
        
        init_script_file = self.work_dir / "init_spock.sh"
        init_script_file.write_text(init_script)
        init_script_file.chmod(0o755)
        
        self.log(f"Initialization script created: {init_script_file}")
        self.log(f"Run it with: {init_script_file}")
        
        return True
    
    def build(self) -> bool:
        """Execute all build steps"""
        try:
            steps = [
                ("Setup PostgreSQL source", self.step1_download_postgresql),
                ("Apply Spock patches", self.step2_apply_spock_patches),
                ("Configure PostgreSQL", self.step3_configure_postgresql),
                ("Build PostgreSQL", self.step4_build_postgresql),
                ("Install PostgreSQL", self.step5_install_postgresql),
                ("Clone Spock repository", self.step6_clone_spock),
                ("Build and install Spock", self.step7_build_spock),
                ("Create configuration snippet", self.step8_create_postgresql_conf_snippet),
                ("Create initialization script", self.create_initialization_script)
            ]
            
            for step_name, step_func in steps:
                self.log("")
                if not step_func():
                    self.log(f"Build failed at step: {step_name}", "ERROR")
                    return False
            
            self.log("=" * 80)
            self.log("BUILD COMPLETED SUCCESSFULLY!")
            self.log("=" * 80)
            self.log("")
            self.log("Summary:")
            self.log(f"  PostgreSQL source: {self.pg_source_dir}")
            self.log(f"  PostgreSQL installation: {self.pg_install_dir}")
            self.log(f"  pg_config: {self.pg_config_path}")
            self.log(f"  Spock source: {self.spock_dir}")
            self.log(f"  Work directory: {self.work_dir}")
            self.log("")
            self.log("Next steps:")
            self.log("  1. Add pg_config to PATH:")
            self.log(f"     export PATH={self.pg_install_dir}/bin:$PATH")
            self.log("  2. Initialize PostgreSQL cluster (if needed)")
            self.log("  3. Update postgresql.conf with Spock settings")
            self.log("  4. Create the Spock extension:")
            self.log("     CREATE EXTENSION spock;")
            self.log("")
            self.log(f"Or use the provided script: {self.work_dir}/init_spock.sh")
            
            return True
            
        except Exception as e:
            self.log(f"Unexpected error during build: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Build PostgreSQL 17.7 with Spock extension from source",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download everything automatically
  python build_spock.py --install-dir /opt/pgedge --verbose
  
  # Use local PostgreSQL source and downloaded patches
  python build_spock.py --install-dir /opt/pgedge --pg-source /path/to/postgresql-17.7 --verbose
  
  # Use local PostgreSQL source and local patches
  python build_spock.py --install-dir /opt/pgedge --pg-source pg17_source/postgresql-17.7 --patches-dir spock_pg17_patches --verbose
  
  # Build with custom work directory
  python build_spock.py --install-dir /usr/local/pgsql --work-dir /tmp/build --verbose
        """
    )
    
    parser.add_argument(
        '--install-dir',
        required=True,
        help='Installation directory for PostgreSQL (e.g., /opt/pgedge)'
    )
    
    parser.add_argument(
        '--pg-source',
        help='Path to PostgreSQL source directory (e.g., /path/to/postgresql-17.7). If not provided, will download automatically.'
    )
    
    parser.add_argument(
        '--patches-dir',
        help='Path to directory containing Spock patches (e.g., /path/to/spock_pg17_patches). Patches can be .patch or .diff files. If not provided, will download from GitHub.'
    )
    
    parser.add_argument(
        '--work-dir',
        help='Working directory for build files (default: ./spock_build)'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose output'
    )
    
    args = parser.parse_args()
    
    # Check for required tools
    required_tools = ['patch', 'git', 'gcc', 'make']
    missing_tools = []
    
    for tool in required_tools:
        if shutil.which(tool) is None:
            missing_tools.append(tool)
    
    if missing_tools:
        print(f"ERROR: Missing required tools: {', '.join(missing_tools)}")
        print("Please install them before running this script.")
        return 1
    
    builder = SpockBuilder(
        install_dir=args.install_dir,
        work_dir=args.work_dir,
        verbose=args.verbose,
        pg_source_dir=args.pg_source,
        patches_dir=args.patches_dir
    )
    
    success = builder.build()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
