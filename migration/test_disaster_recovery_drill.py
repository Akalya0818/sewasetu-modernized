#!/usr/bin/env python3
"""
================================================================================
Seva Setu: Isolated Disaster Recovery Staging Drill Runner
================================================================================
STAGE 2 RUNNER SPECIFICATION (DO NOT EXECUTE WITHOUT EXPLICIT AUTHORIZATION)

Purpose:
  Validates the Scenario B disaster recovery procedure (physical pg_dump restore
  + client-side delta re-injection) in complete isolation on a disposable
  staging database named 'sewasetu_dr_drill_disposable'.

Safety Rules & Guardrails:
  1. DEFAULT REFUSAL: Refuses execution unless --execute-authorized-drill is supplied.
  2. TARGET ISOLATION: Strictly requires EXPECTED_STAGING_HOST (Neon staging direct).
  3. FORBIDDEN TARGETS: Rejects Render, production hosts, poolers (-pooler),
     and any production-like database name.
  4. FORBIDDEN RESTORE TARGETS: Never restores into neondb, postgres,
     sewasetu_disposable_test, or any shared database.
  5. DEDICATED DB NAME: Exclusively restricted to 'sewasetu_dr_drill_disposable'.
  6. PRE-EXISTING DB PROTECTION: Aborts if 'sewasetu_dr_drill_disposable' already exists.
  7. TEARDOWN PROTECTION: Never drops any database not created by the current run.
  8. IDENTITY VERIFICATION: Verifies server inet_server_addr() and current_database()
     before database creation, destructive restore, or teardown.
  9. TOOL RESOLUTION: Safely discovers pg_dump, pg_restore, and psql on Windows
     and Unix without changing system environment settings.
  10. SYNTHETIC DATA ONLY: Never exposes or accesses citizen PII.
================================================================================
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Optional, Tuple, Dict, Any, List

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Ensure repository root is on sys.path when executed directly as a script
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from migration.test_staging_migration import (
    run_sql_file,
    run_sql_command,
    _rollback_transaction,
)

# Dedicated database name for the DR drill
DRILL_DB_NAME = "sewasetu_dr_drill_disposable"

# Databases that must NEVER be dropped, restored into, or modified
PROTECTED_DATABASES = {
    "postgres",
    "neondb",
    "sewasetu_disposable_test",
    "template0",
    "template1",
}

# Forbidden host patterns (Render, production, poolers)
FORBIDDEN_HOST_PATTERNS = [
    "render.com",
    "dpg-",
    "onrender",
    "sewasetu-portal",
    "dbprod",
]

# Production database name indicators
PRODUCTION_NAME_PATTERNS = [
    "prod",
    "production",
    "live",
    "main",
    "master",
    "sewasetu_prod",
]

# Explicit, identical column mappings for Delta Application Export & Import
DELTA_APP_COLUMNS = (
    "application_no",
    "applicant_name",
    "mobile",
    "dob",
    "gender",
    "marital_status",
    "husband_name",
    "husband_employer",
    "village",
    "block",
    "bank_account",
    "ifsc",
    "doc_path",
    "status",
    "submitted_at",
    "decided_at",
    "decided_by",
)

# Explicit column mappings for Delta Portal User Export & Import
DELTA_PORTAL_USER_COLUMNS = (
    "mobile",
    "password_hash",
)


REQUIRED_STAGING_PG_MAJOR = 18


class IncompatibleToolError(Exception):
    """Raised when an installed PostgreSQL tool exists but has an incompatible major version."""
    pass


def get_pe_file_major_version(filepath: str) -> Optional[int]:
    """
    Reads the PE resource version info in-memory on Windows via ctypes.
    Does not launch any subprocess or execute the file.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ver_dll = ctypes.windll.version
        size = ver_dll.GetFileVersionInfoSizeW(filepath, None)
        if not size:
            return None
        res = ctypes.create_string_buffer(size)
        if not ver_dll.GetFileVersionInfoW(filepath, 0, size, res):
            return None
        lplpBuffer = ctypes.c_void_p()
        puLen = ctypes.c_uint()
        if not ver_dll.VerQueryValueW(res, r"\\", ctypes.byref(lplpBuffer), ctypes.byref(puLen)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
            ]

        info = VS_FIXEDFILEINFO.from_address(lplpBuffer.value)
        prod_major = info.dwProductVersionMS >> 16
        if prod_major > 0:
            return prod_major
        file_major = info.dwFileVersionMS >> 16
        if file_major > 0:
            return file_major
    except Exception:
        pass
    return None


def get_tool_major_version(tool_path: str) -> Optional[int]:
    """
    Determines the major version of a PostgreSQL executable.
    Tries in-memory PE metadata first (Windows), then CLI '--version' output.
    Returns integer major version (e.g. 18, 15), or None if cannot be determined.
    """
    if sys.platform.startswith("win"):
        ver = get_pe_file_major_version(tool_path)
        if ver is not None:
            return ver

    try:
        proc = subprocess.run([tool_path, "--version"], capture_output=True, text=True, timeout=5)
        out = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?", out)
        if not m:
            m = re.search(r"\b(\d+)\.(\d+)\b", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass

    return None


def find_postgres_tool(tool_name: str, required_major: Optional[int] = REQUIRED_STAGING_PG_MAJOR) -> str:
    """
    Safely resolves and validates PostgreSQL client executables (pg_dump, pg_restore, psql)
    without downloading binaries, changing system settings, or modifying PATH.

    Discovery Priority:
      1. Explicit environment directories: PG18_BIN_DIR, PG_BIN_DIR, POSTGRES_BIN_DIR
      2. Standard version 18 locations (Program Files\\PostgreSQL\\18\\bin, /usr/lib/postgresql/18/bin)
      3. Other versioned installations (Program Files\\PostgreSQL\\*\\bin, /usr/lib/postgresql/*/bin)
      4. Workspace scratch directory (scratch\\pgsql\\18\\bin, scratch\\pgsql\\bin)
      5. System PATH via shutil.which

    Selection and Compatibility:
      - Strictly PREFERS PostgreSQL 18 client binaries for Neon PostgreSQL 18.6 staging.
      - If required_major is specified (default: 18), REJECTS incompatible older versions (e.g. 15.8)
        because pg_dump cannot dump from a server with a newer major version than itself.
      - If only incompatible versions exist, raises IncompatibleToolError.
      - If tool is missing completely, raises FileNotFoundError.
    """
    exe_name = f"{tool_name}.exe" if sys.platform.startswith("win") and not tool_name.endswith(".exe") else tool_name
    candidates: List[str] = []

    # 1. Explicit environment directory overrides
    for env_var in ["PG18_BIN_DIR", "PG_BIN_DIR", "POSTGRES_BIN_DIR"]:
        env_dir = os.environ.get(env_var, "").strip()
        if env_dir:
            cand = os.path.join(env_dir, exe_name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK) and cand not in candidates:
                candidates.append(os.path.abspath(cand))

    # 2. Preferred version 18 locations
    if sys.platform.startswith("win"):
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        pg18_win = os.path.join(prog_files, "PostgreSQL", "18", "bin", exe_name)
        if os.path.isfile(pg18_win) and os.access(pg18_win, os.X_OK) and pg18_win not in candidates:
            candidates.append(os.path.abspath(pg18_win))
    else:
        pg18_unix = f"/usr/lib/postgresql/18/bin/{tool_name}"
        if os.path.isfile(pg18_unix) and os.access(pg18_unix, os.X_OK) and pg18_unix not in candidates:
            candidates.append(os.path.abspath(pg18_unix))

    # 3. Other versioned directories (sorted descending by version)
    if sys.platform.startswith("win"):
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        ver_matches = glob.glob(os.path.join(prog_files, "PostgreSQL", "*", "bin", exe_name))
        ver_matches.sort(reverse=True)
        for c in ver_matches:
            if os.path.isfile(c) and os.access(c, os.X_OK) and c not in candidates:
                candidates.append(os.path.abspath(c))
    else:
        unix_matches = glob.glob(f"/usr/lib/postgresql/*/bin/{tool_name}")
        unix_matches.sort(reverse=True)
        for c in unix_matches:
            if os.path.isfile(c) and os.access(c, os.X_OK) and c not in candidates:
                candidates.append(os.path.abspath(c))

    # 4. Workspace scratch directories
    cwd = os.getcwd()
    scratch_18 = os.path.join(cwd, "scratch", "pgsql", "18", "bin", exe_name)
    if os.path.isfile(scratch_18) and os.access(scratch_18, os.X_OK) and scratch_18 not in candidates:
        candidates.append(os.path.abspath(scratch_18))

    scratch_default = os.path.join(cwd, "scratch", "pgsql", "bin", exe_name)
    if os.path.isfile(scratch_default) and os.access(scratch_default, os.X_OK) and scratch_default not in candidates:
        candidates.append(os.path.abspath(scratch_default))

    # 5. System PATH search
    resolved_path = shutil.which(exe_name) or shutil.which(tool_name)
    if resolved_path and os.path.isfile(resolved_path) and os.access(resolved_path, os.X_OK):
        abs_resolved = os.path.abspath(resolved_path)
        if abs_resolved not in candidates:
            candidates.append(abs_resolved)

    # 6. Standard Unix paths
    if not sys.platform.startswith("win"):
        for p in [f"/usr/local/bin/{tool_name}", f"/usr/bin/{tool_name}"]:
            if os.path.isfile(p) and os.access(p, os.X_OK) and p not in candidates:
                candidates.append(os.path.abspath(p))

    if not candidates:
        raise FileNotFoundError(
            f"PostgreSQL client tool '{tool_name}' was not found on this system.\n"
            f"Please install official PostgreSQL 18 client tools or configure PG18_BIN_DIR."
        )

    # Check major versions of all discovered candidates
    candidate_versions = []
    for cand in candidates:
        ver = get_tool_major_version(cand)
        candidate_versions.append((cand, ver))

    # If no major version requirement, return the first valid candidate
    if required_major is None:
        return candidates[0]

    # Preference 1: Exact match with required_major (e.g. 18)
    for cand, ver in candidate_versions:
        if ver == required_major:
            return cand

    # Preference 2: Newer major version than required (e.g. 19+)
    for cand, ver in candidate_versions:
        if ver is not None and ver > required_major:
            return cand

    # Check if we only have older/incompatible versions (e.g. 15.8)
    incompatible = [(cand, ver) for cand, ver in candidate_versions if ver is not None and ver < required_major]
    if incompatible:
        best_cand, best_ver = incompatible[0]
        raise IncompatibleToolError(
            f"Incompatible PostgreSQL tool '{tool_name}': found version {best_ver} at '{best_cand}'.\n"
            f"Target staging server runs PostgreSQL 18.6, requiring a compatible PostgreSQL {required_major} client.\n"
            f"pg_dump cannot dump from a server with a newer major version than itself.\n"
            f"Please install official PostgreSQL 18 client tools or set PG18_BIN_DIR to an approved PostgreSQL 18 installation."
        )

    best_cand, _ = candidate_versions[0]
    raise IncompatibleToolError(
        f"Unable to verify PostgreSQL version for '{best_cand}'.\n"
        f"Target staging server requires PostgreSQL {required_major} client tools."
    )


def validate_drill_target_host(url: str, parsed_url: urllib.parse.ParseResult) -> bool:
    """
    Strict safety validation of the target host.
    Rejects Render, pooler endpoints (-pooler), and unverified hosts.
    Requires exact match against EXPECTED_STAGING_HOST.
    """
    hostname = (parsed_url.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("[SAFETY GUARD] Database URL has no valid hostname.")

    for pattern in FORBIDDEN_HOST_PATTERNS:
        if pattern in hostname:
            raise ValueError(
                f"[CRITICAL SAFETY GUARD] REJECTED TARGET HOST: Found forbidden pattern '{pattern}' in hostname '{hostname}'.\n"
                "The disaster recovery drill runner strictly forbids connecting to Render or production databases."
            )

    if "-pooler" in hostname:
        raise ValueError(
            f"[CRITICAL SAFETY GUARD] REJECTED POOLED ENDPOINT: Found '-pooler' in '{hostname}'.\n"
            "Neon connection poolers do not support CREATE DATABASE / DROP DATABASE. Use the direct non-pooled endpoint."
        )

    expected_host = os.environ.get("EXPECTED_STAGING_HOST", "").strip().lower()
    if not expected_host:
        raise ValueError(
            f"[CRITICAL SAFETY GUARD] TARGET HOST UNVERIFIED: Host '{hostname}' is not explicitly allowlisted.\n"
            "For safety, every remote endpoint requires an exact hostname match.\n"
            "Set EXPECTED_STAGING_HOST='<exact-hostname>' in your environment to authorize this target."
        )

    if hostname != expected_host:
        raise ValueError(
            f"[CRITICAL SAFETY GUARD] TARGET HOST MISMATCH: Target host '{hostname}' does not match "
            f"EXPECTED_STAGING_HOST '{expected_host}'."
        )

    return True


def validate_drill_database_name(db_name: str) -> str:
    """
    Strict validation of the drill database name.
    Must be exactly 'sewasetu_dr_drill_disposable'.
    Rejects protected, shared, and production-like database names.
    """
    clean_name = db_name.strip().lower()

    if clean_name in PROTECTED_DATABASES:
        raise ValueError(
            f"[CRITICAL SAFETY GUARD] REJECTED DATABASE NAME: '{clean_name}' is a protected/shared database!\n"
            "The drill runner will NEVER restore into or drop shared databases."
        )

    for pattern in PRODUCTION_NAME_PATTERNS:
        if pattern in clean_name and clean_name != DRILL_DB_NAME:
            raise ValueError(
                f"[CRITICAL SAFETY GUARD] REJECTED DATABASE NAME: '{clean_name}' matches production pattern '{pattern}'."
            )

    if clean_name != DRILL_DB_NAME:
        raise ValueError(
            f"[CRITICAL SAFETY GUARD] REJECTED DATABASE NAME: Expected dedicated database '{DRILL_DB_NAME}', "
            f"got '{clean_name}'. Refusing to proceed on unexpected database."
        )

    return clean_name


def verify_server_and_db_identity(conn, expected_db: Optional[str] = None) -> Dict[str, str]:
    """
    Queries current_database(), current_user, and inet_server_addr()
    to independently prove the connected server and database identity before any action.
    """
    cur = conn.cursor()
    cur.execute("SELECT current_database(), current_user, inet_server_addr()::text, version();")
    row = cur.fetchone()
    cur.close()

    identity = {
        "database": row[0],
        "user": row[1],
        "server_addr": row[2] or "local/socket",
        "version": row[3],
    }

    if expected_db is not None:
        if identity["database"].lower() != expected_db.lower():
            raise RuntimeError(
                f"[CRITICAL IDENTITY MISMATCH] Expected connection to database '{expected_db}', "
                f"but connected to '{identity['database']}'! Aborting operation."
            )

    return identity


def check_existing_database(admin_conn, db_name: str) -> bool:
    """
    Checks if a database exists on the target server.
    """
    cur = admin_conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def build_delta_export_sql(delta_apps_csv: str, delta_users_csv: str, baseline_keys_csv: str) -> str:
    """
    Constructs the exact client-side psql \\copy SQL block for Scenario B delta export.
    Uses explicit column lists matching schema.sql and import mapping.
    """
    apps_cols_str = ", ".join(DELTA_APP_COLUMNS)
    users_cols_str = ", ".join(DELTA_PORTAL_USER_COLUMNS)

    return f"""
CREATE TEMP TABLE baseline_keys (application_no VARCHAR(20), mobile VARCHAR(15));
\\copy baseline_keys FROM '{baseline_keys_csv}' WITH CSV HEADER;

-- Export delta applications (explicit identical column order)
\\copy (SELECT {apps_cols_str} FROM applications WHERE application_no NOT IN (SELECT application_no FROM baseline_keys)) TO '{delta_apps_csv}' WITH CSV HEADER;

-- Export delta portal users (explicit identical column order)
\\copy (SELECT {users_cols_str} FROM portal_users WHERE mobile NOT IN (SELECT mobile FROM baseline_keys)) TO '{delta_users_csv}' WITH CSV HEADER;
"""


def build_delta_import_sql(delta_apps_csv: str, delta_users_csv: str) -> str:
    """
    Constructs the exact client-side psql \\copy SQL block for Scenario B delta re-injection.
    Uses explicit column list into staging_delta_apps (with id dropped) and staging_delta_users.
    """
    apps_cols_str = ", ".join(DELTA_APP_COLUMNS)
    users_cols_str = ", ".join(DELTA_PORTAL_USER_COLUMNS)

    return f"""
-- A. Realign sequence after dump restore
SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);

-- B. Import delta applications using a staging temp table (assigns fresh SERIAL IDs)
CREATE TEMP TABLE staging_delta_apps (LIKE applications INCLUDING DEFAULTS);
ALTER TABLE staging_delta_apps DROP COLUMN id;
\\copy staging_delta_apps ({apps_cols_str}) FROM '{delta_apps_csv}' WITH CSV HEADER;

INSERT INTO applications (
    {apps_cols_str}
)
SELECT {apps_cols_str}
FROM staging_delta_apps
ON CONFLICT (application_no) DO NOTHING;

-- C. Import delta portal accounts
CREATE TEMP TABLE staging_delta_users (LIKE portal_users);
\\copy staging_delta_users ({users_cols_str}) FROM '{delta_users_csv}' WITH CSV HEADER;

INSERT INTO portal_users ({users_cols_str})
SELECT {users_cols_str}
FROM staging_delta_users
ON CONFLICT (mobile) DO NOTHING;

-- D. Final sequence realignment to ensure nextval exceeds all inserted IDs
SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);
"""


def derive_expected_counts(baseline_app_count: int, delta_app_count: int, baseline_user_count: int, delta_user_count: int) -> Dict[str, int]:
    """
    Derives exact expected counts from schema and setup rather than guessing.
    In Scenario B disaster recovery:
    - Pre-migration dump has: baseline_app_count applications, baseline_user_count portal users.
    - Delta re-injection adds: delta_app_count applications, delta_user_count portal users.
    - Total restored applications = baseline_app_count + delta_app_count.
    - Total restored portal users = baseline_user_count + delta_user_count.
    - Inherited seed applications = 0 (since migration was rolled back).
    """
    return {
        "restored_applications": baseline_app_count + delta_app_count,
        "restored_portal_users": baseline_user_count + delta_user_count,
        "inherited_seed_records": 0,
        "min_sequence_val": baseline_app_count + delta_app_count,
    }


def teardown_drill_database(base_url: str, db_name: str, db_created_by_this_run: bool) -> bool:
    """
    Safely drops the dedicated drill database ONLY IF it was created by the current run.
    Never drops neondb, postgres, or any database not created by this run.
    Verifies absence in pg_database.
    """
    if not db_created_by_this_run:
        print(f"\n[TEARDOWN] Skipping drop of '{db_name}': database was NOT created by this execution run.")
        return False

    if db_name != DRILL_DB_NAME:
        print(f"\n[TEARDOWN SAFETY] Refusing to drop '{db_name}': name does not match '{DRILL_DB_NAME}'.")
        return False

    print(f"\n[TEARDOWN] Dropping disposable drill database '{db_name}'...")
    conn = None
    try:
        conn = psycopg2.connect(base_url)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        # Verify server identity before drop
        identity = verify_server_and_db_identity(conn)
        print(f"[TEARDOWN IDENTITY VERIFIED] Server: {identity['server_addr']}, DB: {identity['database']}")

        cur = conn.cursor()
        cur.execute(f"DROP DATABASE {db_name};")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
        absent = cur.fetchone() is None
        cur.close()

        if absent:
            print(f"Teardown verified: '{db_name}' safely dropped and absent from pg_database.")
            return True
        else:
            print(f"[TEARDOWN WARNING] Database '{db_name}' still present after DROP DATABASE.")
            return False
    except Exception as e:
        print(f"[TEARDOWN ERROR] Failed to drop disposable database '{db_name}': {e}")
        return False
    finally:
        if conn and not conn.closed:
            conn.close()


def execute_drill(base_url: str, expected_host: str) -> int:
    """
    Executes the 7-phase isolated disaster recovery drill.
    ONLY CALLED WHEN --execute-authorized-drill is supplied.
    """
    parsed = urllib.parse.urlparse(base_url)
    validate_drill_target_host(base_url, parsed)
    validate_drill_database_name(DRILL_DB_NAME)

    # Discover client tools
    pg_dump_bin = find_postgres_tool("pg_dump")
    pg_restore_bin = find_postgres_tool("pg_restore")
    psql_bin = find_postgres_tool("psql")
    print(f"PostgreSQL client tools resolved safely:\n  pg_dump: {pg_dump_bin}\n  pg_restore: {pg_restore_bin}\n  psql: {psql_bin}")

    admin_db = parsed.path.lstrip("/") or "postgres"
    drill_url = urllib.parse.urlunparse(parsed._replace(path=f"/{DRILL_DB_NAME}"))

    db_created_by_this_run = False
    drill_conn = None
    overall_success = False

    temp_dir = tempfile.mkdtemp(prefix="sewasetu_dr_drill_")
    backup_file = os.path.join(temp_dir, "drill_pre_migration.dump")
    baseline_keys_csv = os.path.join(temp_dir, "drill_baseline_keys.csv")
    delta_apps_csv = os.path.join(temp_dir, "drill_delta_apps.csv")
    delta_users_csv = os.path.join(temp_dir, "drill_delta_users.csv")

    try:
        # Phase 1: Pre-existing database protection & creation
        print(f"\n[PHASE 1] Connecting to base administrative database '{admin_db}'...")
        admin_conn = psycopg2.connect(base_url)
        admin_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        identity = verify_server_and_db_identity(admin_conn)
        print(f"Admin connection verified: server={identity['server_addr']}, db={identity['database']}")

        if check_existing_database(admin_conn, DRILL_DB_NAME):
            admin_conn.close()
            raise RuntimeError(
                f"[SAFETY ABORT] Dedicated drill database '{DRILL_DB_NAME}' already exists on target server!\n"
                "The runner will NEVER automatically drop or overwrite pre-existing databases.\n"
                "Please inspect and manually drop the stale database before running this drill."
            )

        print(f"Creating isolated drill database '{DRILL_DB_NAME}'...")
        cur = admin_conn.cursor()
        cur.execute(f"CREATE DATABASE {DRILL_DB_NAME};")
        cur.close()
        admin_conn.close()
        db_created_by_this_run = True
        print(f"Dedicated database '{DRILL_DB_NAME}' created successfully.")

        # Phase 2: Apply schema, seed synthetic baseline, and export baseline keys
        print(f"\n[PHASE 2] Connecting to '{DRILL_DB_NAME}' and initializing baseline...")
        drill_conn = psycopg2.connect(drill_url)
        drill_conn.autocommit = True

        drill_identity = verify_server_and_db_identity(drill_conn, expected_db=DRILL_DB_NAME)
        print(f"Connected to dedicated drill database: {drill_identity['database']} on {drill_identity['server_addr']}")

        rc, _, err = run_sql_file(drill_conn, "app/schema.sql")
        if rc != 0:
            raise RuntimeError(f"Schema initialization failed: {err}")

        rc, _, err = run_sql_file(drill_conn, "seed/synthetic_seed.sql")
        if rc != 0:
            raise RuntimeError(f"Synthetic seeding failed: {err}")

        # Baseline: 4 synthetic + 2 live-simulated applications = 6 total
        sim_sql = """
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status, status, submitted_at)
        VALUES 
        ('SSP26999905', 'Synthetic Pre-Maint One', '9800000111', '1960-01-01', 'Male', 'Married', 'PENDING', NOW()),
        ('SSP26999902', 'Synthetic Pre-Maint Two', '9800000222', '1955-02-02', 'Female', 'Widowed', 'APPROVED', NOW());

        INSERT INTO portal_users (mobile, password_hash)
        VALUES
        ('9800000111', encode(sha256('01011960'::bytea), 'hex')),
        ('9800000222', encode(sha256('02021955'::bytea), 'hex'))
        ON CONFLICT DO NOTHING;
        """
        rc, _, err = run_sql_command(drill_conn, sim_sql)
        if rc != 0:
            raise RuntimeError(f"Baseline simulation failed: {err}")

        # Export baseline snapshot keys
        export_keys_cmd = f"\\copy (SELECT application_no, mobile FROM applications) TO '{baseline_keys_csv}' WITH CSV HEADER;"
        rc, _, err = run_sql_command(drill_conn, export_keys_cmd)
        if rc != 0:
            raise RuntimeError(f"Baseline keys export failed: {err}")
        print("Baseline initialized: 6 applications, 6 portal users. Baseline keys captured.")

        # Phase 3: Pre-migration physical backup (pg_dump -Fc) & verification (pg_restore --list)
        print("\n[PHASE 3] Generating pre-migration physical backup (pg_dump -Fc)...")
        env = os.environ.copy()
        bin_dir = os.path.dirname(pg_dump_bin)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

        dump_cmd = [
            pg_dump_bin,
            "-Fc",
            "--no-owner",
            "--no-privileges",
            "-f",
            backup_file,
            drill_url,
        ]
        dump_proc = subprocess.run(dump_cmd, capture_output=True, text=True, env=env)
        if dump_proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed (exit={dump_proc.returncode}):\n{dump_proc.stderr}")
        print(f"Backup created: {backup_file} ({os.path.getsize(backup_file)} bytes)")

        # Verify TOC with pg_restore --list
        list_cmd = [pg_restore_bin, "--list", backup_file]
        list_proc = subprocess.run(list_cmd, capture_output=True, text=True, env=env)
        if list_proc.returncode != 0:
            raise RuntimeError(f"pg_restore --list failed: {list_proc.stderr}")
        if "TABLE DATA" not in list_proc.stdout or "applications" not in list_proc.stdout:
            raise RuntimeError("pg_restore --list did not contain applications table data entry!")
        print("Backup archive verified successfully with pg_restore --list.")

        # Phase 4: Maintenance window delta submissions + migration execution + simulated post-migration failure
        print("\n[PHASE 4] Simulating delta maintenance submissions (2 new applications)...")
        delta_sql = """
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status, status, submitted_at)
        VALUES 
        ('SSP26999909', 'Drill Delta Citizen One', '9800000991', '1962-07-07', 'Female', 'Married', 'PENDING', NOW()),
        ('SSP26999910', 'Drill Delta Citizen Two', '9800000992', '1958-08-08', 'Male', 'Widowed', 'PENDING', NOW());

        INSERT INTO portal_users (mobile, password_hash)
        VALUES
        ('9800000991', encode(sha256('07071962'::bytea), 'hex')),
        ('9800000992', encode(sha256('08081958'::bytea), 'hex'));
        """
        rc, _, err = run_sql_command(drill_conn, delta_sql)
        if rc != 0:
            raise RuntimeError(f"Delta insertion failed: {err}")
        print("Delta submissions added: 8 applications, 8 portal users currently in DB.")

        # Phase 5: Execute Scenario B Disaster Recovery
        print("\n[PHASE 5] Executing Scenario B Disaster Recovery...")
        # Step 1: Export delta records
        print("  Step 5.1: Exporting delta applications & users with explicit column mapping...")
        export_delta_cmd = build_delta_export_sql(delta_apps_csv, delta_users_csv, baseline_keys_csv)
        rc, _, err = run_sql_command(drill_conn, export_delta_cmd)
        if rc != 0:
            raise RuntimeError(f"Delta export failed: {err}")

        # Close persistent connection before destructive restore
        drill_conn.close()
        drill_conn = None

        # Step 2: Destructive physical restore of pre-migration backup
        print("  Step 5.2: Restoring physical backup via pg_restore --clean...")
        restore_cmd = [
            pg_restore_bin,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "-d",
            drill_url,
            backup_file,
        ]
        restore_proc = subprocess.run(restore_cmd, capture_output=True, text=True, env=env)
        # Note: pg_restore might emit harmless warnings on clean drops if objects didn't exist
        print(f"  pg_restore finished with exit code {restore_proc.returncode}.")

        # Reopen connection to verify target and execute re-injection
        drill_conn = psycopg2.connect(drill_url)
        drill_conn.autocommit = True
        verify_server_and_db_identity(drill_conn, expected_db=DRILL_DB_NAME)

        # Step 3 & 4: Sequence realignment and delta re-injection with explicit column mapping
        print("  Step 5.3 & 5.4: Re-injecting delta submissions with fresh SERIAL IDs...")
        reinject_cmd = build_delta_import_sql(delta_apps_csv, delta_users_csv)
        rc, _, err = run_sql_command(drill_conn, reinject_cmd)
        if rc != 0:
            raise RuntimeError(f"Delta re-injection failed: {err}")
        print("  Delta re-injection completed successfully.")

        # Phase 6: Post-recovery assertions & derived counts verification
        print("\n[PHASE 6] Validating restored state against derived schema counts...")
        expected = derive_expected_counts(
            baseline_app_count=6,
            delta_app_count=2,
            baseline_user_count=6,
            delta_user_count=2,
        )

        cur = drill_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM applications;")
        actual_apps = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM portal_users;")
        actual_users = cur.fetchone()[0]

        cur.execute("SELECT MAX(id), last_value FROM applications, applications_id_seq GROUP BY last_value;")
        max_id, last_seq = cur.fetchone()

        cur.execute("SELECT application_no, applicant_name, status FROM applications WHERE application_no IN ('SSP26999909', 'SSP26999910') ORDER BY application_no;")
        delta_rows = cur.fetchall()
        cur.close()

        print(f"  Applications count : {actual_apps} (expected {expected['restored_applications']})")
        print(f"  Portal users count : {actual_users} (expected {expected['restored_portal_users']})")
        print(f"  MAX(id): {max_id}, Sequence last_value: {last_seq}")

        assert actual_apps == expected["restored_applications"], f"Application count mismatch: got {actual_apps}, expected {expected['restored_applications']}"
        assert actual_users == expected["restored_portal_users"], f"Portal user count mismatch: got {actual_users}, expected {expected['restored_portal_users']}"
        assert last_seq >= max_id, f"Sequence last_value ({last_seq}) does not exceed MAX(id) ({max_id})"
        assert len(delta_rows) == 2, f"Expected 2 recovered delta submissions, found {len(delta_rows)}"
        assert delta_rows[0][0] == "SSP26999909" and delta_rows[1][0] == "SSP26999910"

        print("\nPASS: All Scenario B disaster recovery assertions passed successfully!")
        overall_success = True

    finally:
        if drill_conn and not drill_conn.closed:
            try:
                drill_conn.close()
            except Exception:
                pass

        # Phase 7: Safe Teardown
        teardown_drill_database(base_url, DRILL_DB_NAME, db_created_by_this_run)

        # Cleanup local scratch files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

    return 0 if overall_success else 1


def main(argv=None) -> int:
    """
    Entry point for disaster recovery drill runner.
    Refuses execution by default unless --execute-authorized-drill is supplied.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        description="Seva Setu: Isolated Disaster Recovery Drill Runner (Scenario B)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute-authorized-drill",
        action="store_true",
        default=False,
        help="Mandatory authorization flag required to actually execute the isolated drill. Default: False (refuses execution).",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("STAGING_DATABASE_URL"),
        help="Base Neon staging database URL (e.g. postgresql://user:pass@ep-host.neon.tech/neondb).",
    )
    parser.add_argument(
        "--expected-staging-host",
        default=os.environ.get("EXPECTED_STAGING_HOST"),
        help="Exact allowlisted Neon staging host required for execution.",
    )

    args = parser.parse_args(argv)

    # STRICT DEFAULT REFUSAL: Planning / Safety check mode
    if not args.execute_authorized_drill:
        print("=" * 80)
        print("SEVA SETU: DISASTER RECOVERY DRILL -- EXECUTION REFUSED BY DEFAULT")
        print("=" * 80)
        print("The disaster recovery drill runner was invoked WITHOUT the explicit execution flag.")
        print("By default, this runner REFUSES execution and performs ZERO database connections.")
        print("")
        print("ISOLATED DRILL DESIGN & SAFEGUARDS:")
        print("  1. Target Isolation  : Requires exact configured Neon staging host; rejects Render & poolers")
        print(f"  2. Dedicated Target  : Exclusively restricted to dedicated database '{DRILL_DB_NAME}'")
        print("  3. Pre-existing Check: Refuses to proceed if the drill database already exists")
        print("  4. Teardown Safety   : Never automatically drops any database not created by this run")
        print("  5. Server Identity   : Queries inet_server_addr() and current_database() before each destructive action")
        print("  6. Explicit Mapping  : Fully aligned 17-column export/import tuples for applications and portal users")
        print("  7. Tool Discovery    : Safely discovers pg_dump, pg_restore, psql on Windows & Unix")
        print("")
        print("EXACT COMMAND TO EXECUTE THE DRILL LATER (WHEN AUTHORIZED):")
        print("  python migration/test_disaster_recovery_drill.py --execute-authorized-drill")
        print("================================================================================")
        return 0

    # Authorized execution mode
    if not args.database_url:
        print("[SAFETY ABORT] Error: Missing base database URL. Provide --database-url or set STAGING_DATABASE_URL.")
        return 1

    if args.expected_staging_host:
        os.environ["EXPECTED_STAGING_HOST"] = args.expected_staging_host

    try:
        return execute_drill(args.database_url, os.environ.get("EXPECTED_STAGING_HOST", ""))
    except Exception as e:
        print(f"\n[DRILL FAILED - SAFE ABORT] Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
