#!/usr/bin/env python3
"""
Spock Functions Availability & Smoke Test
==========================================
Validates that all documented Spock functions exist in the installed extension
and performs basic smoke tests to verify they are callable.

Functions are parsed dynamically from Spock documentation markdown files.

Usage:
    # Check function availability (reads docs from local directory)
    python test_spock_functions.py \
        --dsn "host=localhost port=5432 dbname=mydb user=postgres password=postgres" \
        --docs-dir /path/to/spock/docs/spock_functions/functions

    # Include smoke tests
    python test_spock_functions.py \
        --dsn "..." \
        --docs-dir ./spock_docs \
        --smoke-test

    # Export results to JSON
    python test_spock_functions.py \
        --dsn "..." \
        --docs-dir ./spock_docs \
        --smoke-test \
        --json results.json

    # Also parse category index files (gen_mgmt.md, node_mgmt.md, etc.)
    python test_spock_functions.py \
        --dsn "..." \
        --docs-dir ./spock/docs/spock_functions/functions \
        --index-dir ./spock/docs/spock_functions
"""

import psycopg2
import json
import sys
import os
import re
import glob
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional


# ============================================================================
# SMOKE TESTS: Safe callable tests for read-only functions
# These are manually curated since they require safe test parameters
# ============================================================================

SMOKE_TESTS = {
    "get_country": "SELECT spock.get_country()",
    "spock_version": "SELECT spock.spock_version()",
    "spock_version_num": "SELECT spock.spock_version_num()",
    "spock_max_proto_version": "SELECT spock.spock_max_proto_version()",
    "spock_min_proto_version": "SELECT spock.spock_min_proto_version()",
    "spock_gen_slot_name": "SELECT spock.spock_gen_slot_name('postgres', 'n1', 'sub_n2_n1')",
    "sub_show_status": "SELECT * FROM spock.sub_show_status()",
    "node_info": "SELECT * FROM spock.node_info()",
    "cleanup_resolutions": "SELECT spock.cleanup_resolutions()",
    "sync_event": "SELECT spock.sync_event()",
    "version": "SELECT spock.spock_version()",
    "version_num": "SELECT spock.spock_version_num()",
}


class MarkdownDocParser:
    """Parse Spock function documentation from markdown files"""

    def __init__(self, docs_dir: str, index_dir: str = None, verbose: bool = False):
        self.docs_dir = Path(docs_dir)
        self.index_dir = Path(index_dir) if index_dir else None
        self.verbose = verbose
        self.functions = []
        self.parse_errors = []

    def log(self, msg: str):
        if self.verbose:
            print(f"  [parser] {msg}")

    def parse_all(self) -> List[Dict]:
        """Parse all markdown files and return function definitions"""
        if not self.docs_dir.exists():
            print(f"\033[91mERROR: Documentation directory not found: {self.docs_dir}\033[0m")
            sys.exit(1)

        # Parse individual function doc files
        md_files = sorted(self.docs_dir.glob("*.md"))
        if not md_files:
            print(f"\033[91mERROR: No .md files found in {self.docs_dir}\033[0m")
            sys.exit(1)

        self.log(f"Found {len(md_files)} markdown files in {self.docs_dir}")

        for md_file in md_files:
            try:
                funcs = self.parse_function_file(md_file)
                if funcs:
                    self.functions.extend(funcs)
                else:
                    self.log(f"No function found in {md_file.name}")
            except Exception as e:
                self.parse_errors.append({"file": md_file.name, "error": str(e)})
                self.log(f"Error parsing {md_file.name}: {e}")

        # Parse index files for category info
        if self.index_dir and self.index_dir.exists():
            self._enrich_categories_from_index()

        # Deduplicate by function name
        seen = set()
        unique_funcs = []
        for f in self.functions:
            key = f["func_name"]
            if key not in seen:
                seen.add(key)
                unique_funcs.append(f)
            else:
                self.log(f"Duplicate function skipped: {key} from {f['doc_file']}")

        self.functions = unique_funcs
        self.log(f"Total unique functions parsed: {len(self.functions)}")
        return self.functions

    def parse_function_file(self, filepath: Path) -> List[Dict]:
        """Parse a single markdown file and extract function definition(s)"""
        content = filepath.read_text(encoding="utf-8", errors="replace")
        functions = []

        # Strategy 1: Parse ## NAME / ### SYNOPSIS blocks
        name_blocks = re.split(r'(?:^##\s+NAME\s*$|^#\s+spock\.)', content, flags=re.MULTILINE)

        for block in name_blocks:
            func = self._extract_from_block(block, filepath.name)
            if func:
                functions.append(func)

        # Strategy 2: If no NAME blocks found, try parsing title-based format
        # e.g., "# spock.cleanup_resolutions" or "## spock.sub_create"
        if not functions:
            func = self._extract_from_title_format(content, filepath.name)
            if func:
                functions.append(func)

        # Strategy 3: Try to find function name from SYNOPSIS alone
        if not functions:
            func = self._extract_from_synopsis_only(content, filepath.name)
            if func:
                functions.append(func)

        return functions

    def _extract_from_block(self, block: str, filename: str) -> Optional[Dict]:
        """Extract function info from a NAME/SYNOPSIS block"""
        # Find function name
        # Patterns: spock.func_name(), `spock.func_name()`, spock.func_name
        name_match = re.search(
            r'`?spock\.(\w+)\s*\(`?|'
            r'spock\.(\w+)\s*$|'
            r'^spock\.(\w+)',
            block, re.MULTILINE
        )
        if not name_match:
            return None

        func_name = name_match.group(1) or name_match.group(2) or name_match.group(3)
        if not func_name:
            return None

        # Extract synopsis/signature
        synopsis = ""
        synopsis_match = re.search(
            r'(?:###?\s*SYNOPSIS|```sql)\s*\n(.*?)(?:\n###|\n```|\n##\s|\Z)',
            block, re.DOTALL | re.IGNORECASE
        )
        if synopsis_match:
            synopsis = synopsis_match.group(1).strip()
            # Clean up the synopsis
            synopsis = re.sub(r'\s+', ' ', synopsis)
            synopsis = synopsis.replace('```', '').strip()

        # Extract RETURNS
        returns = ""
        returns_match = re.search(
            r'###?\s*RETURNS\s*\n(.*?)(?:\n###|\n##\s|\Z)',
            block, re.DOTALL | re.IGNORECASE
        )
        if returns_match:
            returns = returns_match.group(1).strip().split('\n')[0].strip()
            returns = re.sub(r'^[\s\-\*]+', '', returns)

        # Extract DESCRIPTION (first paragraph)
        description = ""
        desc_match = re.search(
            r'(?:###?\s*DESCRIPTION|##\s*Description)\s*\n(.*?)(?:\n###|\n##\s|\Z)',
            block, re.DOTALL | re.IGNORECASE
        )
        if desc_match:
            desc_text = desc_match.group(1).strip()
            # Take first paragraph only
            first_para = desc_text.split('\n\n')[0]
            description = re.sub(r'\s+', ' ', first_para).strip()[:200]

        # Extract ARGUMENTS
        arguments = []
        args_match = re.search(
            r'(?:###?\s*ARGUMENTS|##\s*Arguments)\s*\n(.*?)(?:\n###|\n##\s|###?\s*EXAMPLE|\Z)',
            block, re.DOTALL | re.IGNORECASE
        )
        if args_match:
            args_text = args_match.group(1).strip()
            # Parse argument names from various formats
            # Format 1: "- `param_name` - description"
            # Format 2: "param_name\n    description"
            # Format 3: "- param_name is ..."
            arg_patterns = [
                re.findall(r'[`\-]\s*`?(\w+)`?\s*[\-–]\s*(.+?)(?:\n|$)', args_text),
                re.findall(r'^(\w+)\s*\n\s+(.+?)(?:\n|$)', args_text, re.MULTILINE),
            ]
            for matches in arg_patterns:
                for name, desc in matches:
                    if name.lower() not in ('the', 'a', 'an', 'if', 'this', 'use', 'when',
                                            'default', 'true', 'false', 'null', 'returns'):
                        arguments.append({"name": name, "description": desc.strip()[:100]})

        return {
            "func_name": func_name,
            "full_name": f"spock.{func_name}",
            "synopsis": synopsis,
            "returns": returns,
            "description": description,
            "arguments": arguments,
            "category": "Uncategorized",
            "doc_file": filename,
            "check_sql": f"SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid WHERE n.nspname = 'spock' AND p.proname = '{func_name}'",
        }

    def _extract_from_title_format(self, content: str, filename: str) -> Optional[Dict]:
        """Extract function from title-based format like '# spock.func_name'"""
        title_match = re.search(
            r'^#\s+spock\.(\w+)',
            content, re.MULTILINE
        )
        if not title_match:
            return None

        func_name = title_match.group(1)

        # Try to find synopsis in ```sql blocks
        synopsis = ""
        sql_match = re.search(r'```sql\s*\n(.*?)\n```', content, re.DOTALL)
        if sql_match:
            synopsis = sql_match.group(1).strip().split('\n')[0]

        # Find description
        description = ""
        desc_match = re.search(r'##\s*Description\s*\n(.*?)(?:\n##|\Z)', content, re.DOTALL)
        if desc_match:
            first_para = desc_match.group(1).strip().split('\n\n')[0]
            description = re.sub(r'\s+', ' ', first_para).strip()[:200]

        # Find returns
        returns = ""
        ret_match = re.search(r'##\s*Returns?\s*\n(.*?)(?:\n##|\Z)', content, re.DOTALL)
        if ret_match:
            returns = ret_match.group(1).strip().split('\n')[0].strip()

        return {
            "func_name": func_name,
            "full_name": f"spock.{func_name}",
            "synopsis": synopsis,
            "returns": returns,
            "description": description,
            "arguments": [],
            "category": "Uncategorized",
            "doc_file": filename,
            "check_sql": f"SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid WHERE n.nspname = 'spock' AND p.proname = '{func_name}'",
        }

    def _extract_from_synopsis_only(self, content: str, filename: str) -> Optional[Dict]:
        """Last resort: extract function name from any spock.xxx pattern"""
        match = re.search(r'spock\.(\w+)\s*\(', content)
        if not match:
            return None

        func_name = match.group(1)
        # Skip common non-function references
        if func_name in ('node', 'subscription', 'replication_set', 'local_node',
                         'exception_log', 'resolutions', 'tables', 'progress',
                         'lag_tracker', 'local_sync_status', 'depend', 'queue',
                         'replication_set_table', 'replication_set_seq',
                         'node_interface', 'sequence_state', 'country'):
            return None

        return {
            "func_name": func_name,
            "full_name": f"spock.{func_name}",
            "synopsis": "",
            "returns": "",
            "description": "",
            "arguments": [],
            "category": "Uncategorized",
            "doc_file": filename,
            "check_sql": f"SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid WHERE n.nspname = 'spock' AND p.proname = '{func_name}'",
        }

    def _enrich_categories_from_index(self):
        """Read index/category files to assign categories to functions"""
        category_map = {}

        # Known index files and their categories
        index_patterns = {
            "gen_mgmt": "General Management",
            "node_mgmt": "Node Management",
            "sub_mgmt": "Subscription Management",
            "repset_mgmt": "Replication Set Management",
        }

        for pattern, category in index_patterns.items():
            index_files = list(self.index_dir.glob(f"{pattern}*"))
            for idx_file in index_files:
                try:
                    content = idx_file.read_text(encoding="utf-8", errors="replace")
                    # Find function references: [spock.func_name] or spock.func_name()
                    func_refs = re.findall(r'spock\.(\w+)', content)
                    for fname in func_refs:
                        if fname not in ('node', 'subscription', 'replication_set',
                                         'tables', 'country'):
                            category_map[fname] = category
                    self.log(f"Index {idx_file.name}: {len(func_refs)} refs → {category}")
                except Exception as e:
                    self.log(f"Error reading index {idx_file.name}: {e}")

        # Apply categories
        for func in self.functions:
            if func["func_name"] in category_map:
                func["category"] = category_map[func["func_name"]]

        # Auto-categorize remaining by name prefix
        for func in self.functions:
            if func["category"] == "Uncategorized":
                name = func["func_name"]
                if name.startswith("node_"):
                    func["category"] = "Node Management"
                elif name.startswith("sub_"):
                    func["category"] = "Subscription Management"
                elif name.startswith("repset_"):
                    func["category"] = "Replication Set Management"
                elif name.startswith("replicate_"):
                    func["category"] = "DDL Replication"
                elif name.startswith("sync_") or name.startswith("wait_"):
                    func["category"] = "Sync & Coordination"
                elif name.startswith("table_wait"):
                    func["category"] = "Sync & Coordination"
                elif name.startswith("cleanup_") or name.startswith("xact_"):
                    func["category"] = "Conflict Resolution"
                elif name.startswith("spock_") or name.startswith("get_"):
                    func["category"] = "General Management"


class SpockFunctionTester:
    """Test framework for validating Spock functions against documentation"""

    def __init__(self, dsn: str, documented_functions: List[Dict], verbose: bool = False):
        self.dsn = dsn
        self.verbose = verbose
        self.documented_functions = documented_functions
        self.conn = None
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "spock_version": None,
            "pg_version": None,
            "total_documented": len(documented_functions),
            "found": 0,
            "missing": 0,
            "smoke_passed": 0,
            "smoke_failed": 0,
            "smoke_skipped": 0,
            "categories": {},
            "details": [],
            "undocumented": [],
            "parse_stats": {},
        }

    def connect(self):
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True

    def close(self):
        if self.conn:
            self.conn.close()

    def log(self, msg: str, status: str = "INFO"):
        icons = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠", "INFO": "·", "HEADER": "═"}
        icon = icons.get(status, " ")
        colors = {
            "PASS": "\033[92m",
            "FAIL": "\033[91m",
            "WARN": "\033[93m",
            "INFO": "\033[94m",
            "HEADER": "\033[95m\033[1m",
        }
        end = "\033[0m"
        color = colors.get(status, "")
        print(f"{color}  {icon} {msg}{end}")

    def get_spock_info(self):
        """Get Spock and PostgreSQL version info"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT version()")
            self.results["pg_version"] = cur.fetchone()[0].split(",")[0]

            try:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'spock'")
                row = cur.fetchone()
                self.results["spock_version"] = row[0] if row else "NOT INSTALLED"
            except:
                self.results["spock_version"] = "NOT INSTALLED"

    def check_function_exists(self, func_def: Dict) -> Tuple[bool, Optional[str]]:
        """Check if a function exists in the database"""
        try:
            with self.conn.cursor() as cur:
                cur.execute(func_def["check_sql"])
                rows = cur.fetchall()
                if rows:
                    return True, None
                return False, "Function not found in pg_proc"
        except Exception as e:
            return False, str(e)

    def run_smoke_test(self, func_name: str) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
        """Run a smoke test for a function"""
        if func_name not in SMOKE_TESTS:
            return None, "No smoke test defined", None

        sql = SMOKE_TESTS[func_name]
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                result = cur.fetchone()
                return True, None, str(result[0]) if result else "(empty)"
        except Exception as e:
            return False, str(e), None

    def get_actual_spock_functions(self) -> List[Dict]:
        """Get all functions actually present in the spock schema with details"""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.proname,
                    CASE p.prokind
                        WHEN 'f' THEN 'function'
                        WHEN 'p' THEN 'procedure'
                        WHEN 'a' THEN 'aggregate'
                        WHEN 'w' THEN 'window'
                    END AS kind,
                    pg_get_function_arguments(p.oid) AS arguments,
                    pg_get_function_result(p.oid) AS return_type
                FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'spock'
                ORDER BY p.proname
            """)
            return [
                {"name": row[0], "kind": row[1], "arguments": row[2], "return_type": row[3]}
                for row in cur.fetchall()
            ]

    def find_undocumented_functions(self):
        """Find functions in spock schema that are not in the documented list"""
        actual_funcs_list = self.get_actual_spock_functions()
        actual_names = set(f["name"] for f in actual_funcs_list)
        documented_names = set(f["func_name"] for f in self.documented_functions)

        undocumented_names = actual_names - documented_names
        internal_prefixes = ("_", "pg_", "queue_")
        notable = [f for f in actual_funcs_list
                   if f["name"] in undocumented_names
                   and not any(f["name"].startswith(p) for p in internal_prefixes)]
        notable.sort(key=lambda x: x["name"])

        self.results["undocumented"] = [
            {"name": f["name"], "kind": f["kind"], "arguments": f["arguments"],
             "return_type": f["return_type"]}
            for f in notable
        ]

        total_actual = len(actual_funcs_list)
        func_count = sum(1 for f in actual_funcs_list if f["kind"] == "function")
        proc_count = sum(1 for f in actual_funcs_list if f["kind"] == "procedure")
        agg_count = sum(1 for f in actual_funcs_list if f["kind"] == "aggregate")

        self.results["total_installed"] = total_actual
        self.results["installed_functions"] = func_count
        self.results["installed_procedures"] = proc_count
        self.results["installed_aggregates"] = agg_count
        self.results["total_undocumented"] = len(self.results["undocumented"])
        self.results["all_installed_functions"] = [
            {"name": f["name"], "kind": f["kind"], "arguments": f["arguments"],
             "return_type": f["return_type"]}
            for f in actual_funcs_list
        ]

    def run_availability_check(self):
        """Check availability of all documented functions"""
        print()
        self.log("SPOCK FUNCTION AVAILABILITY CHECK", "HEADER")
        self.log(f"PostgreSQL: {self.results['pg_version']}", "INFO")
        self.log(f"Spock: {self.results['spock_version']}", "INFO")
        self.log(f"Documented functions to check: {len(self.documented_functions)}", "INFO")
        print()

        current_category = None
        for func_def in self.documented_functions:
            category = func_def.get("category", "Uncategorized")
            if category != current_category:
                current_category = category
                print()
                self.log(f"── {category} ──", "HEADER")
                if category not in self.results["categories"]:
                    self.results["categories"][category] = {"found": 0, "missing": 0}

            exists, error = self.check_function_exists(func_def)

            detail = {
                "name": func_def["full_name"],
                "func_name": func_def["func_name"],
                "category": category,
                "synopsis": func_def.get("synopsis", ""),
                "doc_file": func_def.get("doc_file", ""),
                "exists": exists,
                "error": error,
            }

            if exists:
                self.results["found"] += 1
                self.results["categories"][category]["found"] += 1
                sig = func_def.get("synopsis", "")
                sig_short = f" — {sig[:60]}..." if sig and len(sig) > 60 else (f" — {sig}" if sig else "")
                self.log(f"{func_def['full_name']}{sig_short}", "PASS")
            else:
                self.results["missing"] += 1
                self.results["categories"][category]["missing"] += 1
                self.log(f"{func_def['full_name']} — {error}", "FAIL")

            self.results["details"].append(detail)

    def run_smoke_tests(self):
        """Run smoke tests on read-only functions"""
        print()
        self.log("SMOKE TESTS (read-only functions)", "HEADER")
        print()

        for func_def in self.documented_functions:
            func_name = func_def["func_name"]

            if func_name not in SMOKE_TESTS:
                self.results["smoke_skipped"] += 1
                continue

            passed, error, result_val = self.run_smoke_test(func_name)

            if passed is None:
                self.results["smoke_skipped"] += 1
                continue
            elif passed:
                self.results["smoke_passed"] += 1
                display = f" → {result_val}" if result_val else ""
                self.log(f"spock.{func_name}(){display}", "PASS")
            else:
                self.results["smoke_failed"] += 1
                self.log(f"spock.{func_name}() — {error}", "FAIL")

            for d in self.results["details"]:
                if d["func_name"] == func_name:
                    d["smoke_passed"] = passed
                    d["smoke_error"] = error
                    d["smoke_result"] = result_val
                    break

    def print_summary(self):
        """Print the test summary"""
        print()
        self.log("TEST SUMMARY", "HEADER")
        print()

        total = self.results["total_documented"]
        found = self.results["found"]
        missing = self.results["missing"]
        pct = (found / total * 100) if total > 0 else 0

        total_installed = self.results.get("total_installed", 0)
        installed_funcs = self.results.get("installed_functions", 0)
        installed_procs = self.results.get("installed_procedures", 0)
        installed_aggs = self.results.get("installed_aggregates", 0)
        total_undoc = self.results.get("total_undocumented", 0)

        self.log("INSTALLED IN spock SCHEMA", "HEADER")
        self.log(f"Total objects:         {total_installed}", "INFO")
        self.log(f"  Functions:           {installed_funcs}", "INFO")
        self.log(f"  Procedures:          {installed_procs}", "INFO")
        if installed_aggs > 0:
            self.log(f"  Aggregates:          {installed_aggs}", "INFO")
        print()

        self.log("DOCUMENTATION COVERAGE", "HEADER")
        self.log(f"Documented functions:   {total}", "INFO")
        self.log(f"Found in database:      {found}/{total} ({pct:.1f}%)",
                 "PASS" if missing == 0 else "WARN")
        self.log(f"Missing from database:  {missing}", "FAIL" if missing > 0 else "PASS")
        self.log(f"Undocumented (in DB):   {total_undoc}",
                 "WARN" if total_undoc > 0 else "PASS")

        doc_coverage = (total / total_installed * 100) if total_installed > 0 else 0
        self.log(f"Doc coverage:           {total}/{total_installed} ({doc_coverage:.1f}%)",
                 "PASS" if doc_coverage >= 80 else "WARN")

        # Parse stats
        if self.results.get("parse_stats"):
            ps = self.results["parse_stats"]
            print()
            self.log("DOCUMENTATION PARSING", "HEADER")
            self.log(f"Files scanned:          {ps.get('files_scanned', 0)}", "INFO")
            self.log(f"Functions extracted:     {ps.get('functions_extracted', 0)}", "INFO")
            if ps.get("parse_errors", 0) > 0:
                self.log(f"Parse errors:           {ps['parse_errors']}", "WARN")

        smoke_total = self.results["smoke_passed"] + self.results["smoke_failed"]
        if smoke_total > 0:
            print()
            self.log("SMOKE TESTS", "HEADER")
            self.log(f"Tests run:              {smoke_total}", "INFO")
            self.log(f"Passed:                 {self.results['smoke_passed']}", "PASS")
            self.log(f"Failed:                 {self.results['smoke_failed']}",
                     "FAIL" if self.results["smoke_failed"] > 0 else "PASS")
            self.log(f"Skipped:                {self.results['smoke_skipped']}", "INFO")

        # Category breakdown
        print()
        self.log("CATEGORY BREAKDOWN", "HEADER")
        print()
        for cat, stats in sorted(self.results["categories"].items()):
            cat_total = stats["found"] + stats["missing"]
            status = "PASS" if stats["missing"] == 0 else "FAIL"
            self.log(f"{cat}: {stats['found']}/{cat_total}", status)

        # Undocumented functions
        if self.results["undocumented"]:
            print()
            self.log(f"UNDOCUMENTED FUNCTIONS ({len(self.results['undocumented'])} in spock schema, not in docs)", "WARN")
            print()
            for fn in self.results["undocumented"]:
                kind_label = f"[{fn['kind']}]" if fn.get('kind') else ""
                args = fn.get('arguments', '')
                args_short = args[:60] + "..." if len(args) > 60 else args
                self.log(f"spock.{fn['name']}({args_short}) {kind_label}", "WARN")

        # Missing functions detail
        missing_funcs = [d for d in self.results["details"] if not d["exists"]]
        if missing_funcs:
            print()
            self.log("MISSING FUNCTIONS DETAIL", "FAIL")
            print()
            for mf in missing_funcs:
                self.log(f"{mf['name']} (doc: {mf['doc_file']})", "FAIL")
                if mf.get("error"):
                    self.log(f"  Error: {mf['error']}", "INFO")

        # Final verdict
        print()
        if missing == 0 and self.results["smoke_failed"] == 0 and total_undoc == 0:
            self.log("ALL DOCUMENTED FUNCTIONS AVAILABLE, WORKING, AND FULLY DOCUMENTED", "PASS")
        elif missing == 0 and self.results["smoke_failed"] == 0:
            self.log(f"ALL DOCUMENTED FUNCTIONS AVAILABLE AND WORKING "
                     f"({total_undoc} undocumented functions found)", "WARN")
        elif missing == 0:
            self.log("ALL FUNCTIONS AVAILABLE, SOME SMOKE TESTS FAILED", "WARN")
        else:
            self.log(f"{missing} DOCUMENTED FUNCTIONS ARE MISSING FROM THE INSTALLATION", "FAIL")
        print()

    def export_json(self, filepath: str = None):
        """Export results as JSON"""
        output = json.dumps(self.results, indent=2, default=str)
        if filepath and filepath != "stdout":
            with open(filepath, "w") as f:
                f.write(output)
            self.log(f"Results exported to {filepath}", "INFO")
        else:
            print(output)


def main():
    parser = argparse.ArgumentParser(
        description="Spock Functions Availability & Smoke Test (doc-driven)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check against local Spock docs
  python test_spock_functions.py \\
      --dsn "host=localhost port=5432 dbname=mydb user=postgres password=postgres" \\
      --docs-dir /path/to/spock/docs/spock_functions/functions

  # Include category index files for better categorization
  python test_spock_functions.py \\
      --dsn "..." \\
      --docs-dir ./spock/docs/spock_functions/functions \\
      --index-dir ./spock/docs/spock_functions

  # Include smoke tests
  python test_spock_functions.py --dsn "..." --docs-dir ./docs --smoke-test

  # Export results to JSON
  python test_spock_functions.py --dsn "..." --docs-dir ./docs --json results.json

  # Verbose mode (shows parsing details)
  python test_spock_functions.py --dsn "..." --docs-dir ./docs -v

  # Clone spock repo and test against it
  git clone --depth 1 https://github.com/pgEdge/spock.git /tmp/spock
  python test_spock_functions.py \\
      --dsn "..." \\
      --docs-dir /tmp/spock/docs/spock_functions/functions \\
      --index-dir /tmp/spock/docs/spock_functions \\
      --smoke-test
        """,
    )

    parser.add_argument("--dsn", required=True, help="PostgreSQL connection string")
    parser.add_argument("--docs-dir", required=True,
                        help="Path to spock function docs directory (contains .md files)")
    parser.add_argument("--index-dir",
                        help="Path to spock_functions directory with index files "
                             "(gen_mgmt.md, node_mgmt.md, etc.) for category enrichment")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run smoke tests on read-only functions")
    parser.add_argument("--json", nargs="?", const="stdout", default=None,
                        help="Export results as JSON (to file or stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Phase 1: Parse documentation
    print()
    print("\033[95m\033[1m  ═ PARSING SPOCK DOCUMENTATION\033[0m")
    print(f"\033[94m  · Docs directory: {args.docs_dir}\033[0m")
    if args.index_dir:
        print(f"\033[94m  · Index directory: {args.index_dir}\033[0m")

    doc_parser = MarkdownDocParser(args.docs_dir, args.index_dir, verbose=args.verbose)
    documented_functions = doc_parser.parse_all()

    if not documented_functions:
        print("\033[91m  ERROR: No functions parsed from documentation!\033[0m")
        print("  Check that the docs directory contains Spock function .md files")
        sys.exit(1)

    md_files = list(Path(args.docs_dir).glob("*.md"))
    print(f"\033[92m  ✓ Parsed {len(documented_functions)} functions from "
          f"{len(md_files)} markdown files\033[0m")

    if doc_parser.parse_errors:
        print(f"\033[93m  ⚠ {len(doc_parser.parse_errors)} files had parse errors\033[0m")
        if args.verbose:
            for err in doc_parser.parse_errors:
                print(f"\033[93m    - {err['file']}: {err['error']}\033[0m")

    # Show parsed functions by category
    categories = {}
    for f in documented_functions:
        cat = f.get("category", "Uncategorized")
        categories.setdefault(cat, []).append(f["func_name"])

    print()
    for cat in sorted(categories.keys()):
        funcs = categories[cat]
        print(f"\033[94m  · {cat}: {len(funcs)} functions\033[0m")
        if args.verbose:
            for fn in funcs:
                print(f"\033[94m      - spock.{fn}\033[0m")

    # Phase 2: Test against database
    tester = SpockFunctionTester(args.dsn, documented_functions, verbose=args.verbose)
    tester.results["parse_stats"] = {
        "files_scanned": len(md_files),
        "functions_extracted": len(documented_functions),
        "parse_errors": len(doc_parser.parse_errors),
        "parse_error_details": doc_parser.parse_errors,
        "categories_found": len(categories),
    }

    try:
        tester.connect()
        tester.get_spock_info()

        if tester.results["spock_version"] == "NOT INSTALLED":
            print("\n\033[91mERROR: Spock extension not installed!\033[0m")
            print("Run: CREATE EXTENSION spock;")
            sys.exit(1)

        tester.run_availability_check()
        tester.find_undocumented_functions()

        if args.smoke_test:
            tester.run_smoke_tests()

        tester.print_summary()

        if args.json:
            tester.export_json(args.json)

        sys.exit(1 if tester.results["missing"] > 0 else 0)

    except psycopg2.Error as e:
        print(f"\033[91mDatabase connection error: {e}\033[0m")
        sys.exit(1)
    finally:
        tester.close()


if __name__ == "__main__":
    main()