#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seva Setu: Pre-Migration Read-Only Collision Validation Utility

Purpose:
    Safely verifies that no application numbers in the inherited dataset (seed/seed.sql)
    collide with existing live-database application numbers prior to migration execution.

Safety Guarantees:
    1. Read-Only DB Session: Enforces readonly=True session; executes zero INSERT/UPDATE/DELETE.
    2. Zero PII Exposure: Extracts and logs ONLY application numbers (e.g. 'SSP26...').
       Never queries, logs, or prints citizen names, mobile numbers, dates of birth, or bank details.
    3. Fail-Safe: Fails with non-zero exit code (1) if any unexpected collision is found.
    4. Whitelists synthetic development records ('APP-2026-00001'..'4') handled by Phase 2 deduplication.
"""

import sys
import os
import re
import argparse
from typing import Set, Tuple, Optional, Iterable, List
from dataclasses import dataclass

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# Regex for valid application numbers in Seva Setu:
# - Inherited production keys: e.g. 'SSP25825708', 'SSP26389510'
# - Synthetic development keys: e.g. 'APP-2026-00001', 'APP-2026-00004'
# - Generated application numbers: e.g. 'SSP26999905'
APPLICATION_NO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{3,29}$")


def is_valid_application_no(val: str) -> bool:
    """Validate that a string strictly conforms to Seva Setu application number format."""
    if not val or not isinstance(val, str):
        return False
    return bool(APPLICATION_NO_PATTERN.match(val.strip()))


# Allowed synthetic test application numbers that Phase 2 of migrate_database.sql
# explicitly resolves and deduplicates during migration.
DEFAULT_ALLOWED_EXCEPTIONS: Set[str] = {
    "APP-2026-00001",
    "APP-2026-00002",
    "APP-2026-00003",
    "APP-2026-00004",
}


@dataclass(frozen=True)
class CollisionCheckResult:
    """Read-only result summary containing solely identifier counts and colliding keys (zero PII)."""
    is_safe: bool
    seed_count: int
    db_count: int
    collision_count: int
    colliding_keys: List[str]


def parse_copy_columns(copy_line: str) -> List[str]:
    """Parse column names from a COPY <table> (<col1>, <col2>, ...) statement."""
    match = re.search(r"\(([^)]+)\)", copy_line)
    if not match:
        return []
    cols = [c.strip().strip('"').lower() for c in match.group(1).split(",")]
    return cols


def extract_seed_application_numbers(seed_source) -> Set[str]:
    """
    Extract application numbers from seed/seed.sql COPY block without loading citizen PII into memory.

    Args:
        seed_source: File path (str) or open readable text stream.

    Returns:
        Set of application number strings.

    Raises:
        FileNotFoundError: If seed file does not exist.
        ValueError: If file is empty, missing COPY block, or contains malformed numbers.
    """
    if isinstance(seed_source, str):
        if not os.path.exists(seed_source):
            raise FileNotFoundError(f"Seed file not found: '{seed_source}'")
        if os.path.getsize(seed_source) == 0:
            raise ValueError(f"Seed file is empty (0 bytes): '{seed_source}'")
        with open(seed_source, "r", encoding="utf-8") as f:
            return _extract_from_stream(f, source_name=seed_source)
    else:
        return _extract_from_stream(seed_source, source_name="<stream>")


def _extract_from_stream(stream: Iterable[str], source_name: str = "<stream>") -> Set[str]:
    app_numbers: Set[str] = set()
    in_applications_copy = False
    app_no_col_idx = 1  # Default to column 1 based on schema
    line_num = 0

    for line in stream:
        line_num += 1
        line_clean = line.strip()
        if not in_applications_copy:
            if line_clean.lower().startswith("copy applications"):
                cols = parse_copy_columns(line_clean)
                if "application_no" in cols:
                    app_no_col_idx = cols.index("application_no")
                in_applications_copy = True
            continue

        # Check for end of COPY stdin stream
        if line_clean == r"\." or line_clean.startswith(r"\."):
            break

        if not line:
            continue

        parts = line.split("\t")
        if len(parts) > app_no_col_idx:
            val = parts[app_no_col_idx].strip()
            if val and val != r"\N":
                if not is_valid_application_no(val):
                    raise ValueError(
                        f"Malformed application number on line {line_num} in '{source_name}': '{val}'"
                    )
                app_numbers.add(val)

    if not in_applications_copy:
        raise ValueError(f"Seed source '{source_name}' does not contain a valid 'COPY applications' block")

    if len(app_numbers) == 0:
        raise ValueError(f"Zero valid application numbers extracted from seed source '{source_name}'")

    return app_numbers


def fetch_db_application_numbers(database_url: str) -> Set[str]:
    """
    Query existing database application numbers under a strict read-only session.

    Safety Guarantees:
        - Sets session readonly=True.
        - Queries only `application_no` from `applications`.
        - Zero mutations or PII retrieval.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required to connect to the database")

    conn = psycopg2.connect(database_url)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            # Verify table existence first
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = 'applications'
                );
            """)
            table_exists = cur.fetchone()[0]
            if not table_exists:
                return set()

            cur.execute("SELECT application_no FROM applications WHERE application_no IS NOT NULL;")
            rows = cur.fetchall()
            keys = set()
            for row in rows:
                if row and row[0]:
                    key = str(row[0]).strip()
                    if not is_valid_application_no(key):
                        raise ValueError(f"Database returned malformed application_no: '{key}'")
                    keys.add(key)
            return keys
    finally:
        conn.close()


def load_keys_from_file(file_path: str) -> Set[str]:
    """
    Load application keys from a plain text (one per line) or CSV file for offline testing.

    Safety:
        - Strictly validates application number format for every record.
        - Fails safely on malformed lines, code syntax, empty files, or unreadable inputs.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Keys file not found: '{file_path}'")

    if os.path.getsize(file_path) == 0:
        raise ValueError(f"Keys file is empty (0 bytes): '{file_path}'")

    keys: Set[str] = set()
    line_num = 0
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line_num += 1
            line_str = line.strip()
            if not line_str or line_str.startswith("#") or line_str.startswith("--"):
                continue

            # First column extraction (handling comma or tab delimited)
            first_col = re.split(r"[,\t]", line_str)[0].strip().strip('"')

            # Skip header if present
            if first_col.lower() in ("application_no", "app_no"):
                continue

            if not is_valid_application_no(first_col):
                raise ValueError(
                    f"Malformed input on line {line_num} in '{file_path}': "
                    f"'{first_col}' is not a valid application number format."
                )

            keys.add(first_col)

    if len(keys) == 0:
        raise ValueError(f"No valid application numbers found in keys file: '{file_path}'")

    return keys


def check_application_collisions(
    seed_keys: Set[str],
    db_keys: Set[str],
    allowed_exceptions: Optional[Set[str]] = None
) -> CollisionCheckResult:
    """
    Compare seed and database application numbers, filtering out allowed synthetic test keys.

    Returns:
        CollisionCheckResult with safety status and colliding keys (zero PII).
    """
    if allowed_exceptions is None:
        allowed_exceptions = DEFAULT_ALLOWED_EXCEPTIONS

    intersection = seed_keys.intersection(db_keys)
    unresolved_collisions = intersection - allowed_exceptions
    colliding_list = sorted(list(unresolved_collisions))

    return CollisionCheckResult(
        is_safe=len(colliding_list) == 0,
        seed_count=len(seed_keys),
        db_count=len(db_keys),
        collision_count=len(colliding_list),
        colliding_keys=colliding_list,
    )


def format_report(result: CollisionCheckResult) -> str:
    """Format report string strictly without PII."""
    lines = [
        "================================================================================",
        "Seva Setu: Pre-Migration Application Number Collision Report",
        "================================================================================",
        f"Inherited Seed Application Numbers Extracted : {result.seed_count:,}",
        f"Live Database Application Numbers Counted   : {result.db_count:,}",
        f"Unexpected Application Number Collisions    : {result.collision_count}",
        "--------------------------------------------------------------------------------",
    ]

    if result.is_safe:
        lines.append("[STATUS: PASS] Zero unexpected application number collisions detected.")
        lines.append("Preflight key compatibility verified. Safe to proceed to migration preflight SQL.")
    else:
        lines.append("[STATUS: FAIL - COLLISION DETECTED]")
        lines.append("The following application number(s) exist in both datasets and would violate uniqueness:")
        for k in result.colliding_keys:
            lines.append(f"  - Key: {k}")
        lines.append("CRITICAL: Maintenance window must be aborted or investigated before running migration.")

    lines.append("================================================================================")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seva Setu: Read-only pre-migration application number collision validator."
    )
    parser.add_argument(
        "--seed-file",
        default="seed/seed.sql",
        help="Path to seed/seed.sql (default: seed/seed.sql)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL connection string (defaults to DATABASE_URL environment variable)",
    )
    parser.add_argument(
        "--offline-db-keys-file",
        help="Path to file containing DB application numbers for offline verification / testing",
    )

    args = parser.parse_args(argv)

    try:
        print(f"Extracting application numbers from seed file: {args.seed_file}...")
        seed_keys = extract_seed_application_numbers(args.seed_file)
        print(f"Extracted {len(seed_keys):,} seed application numbers.")

        db_keys: Set[str] = set()
        if args.offline_db_keys_file:
            print(f"Loading live application keys from offline file: {args.offline_db_keys_file}...")
            db_keys = load_keys_from_file(args.offline_db_keys_file)
            print(f"Loaded {len(db_keys):,} valid live application keys.")
        elif args.database_url:
            print("Connecting to database in STRICT READ-ONLY mode...")
            db_keys = fetch_db_application_numbers(args.database_url)
            print(f"Retrieved {len(db_keys):,} application numbers from live database.")
        else:
            print(
                "[ERROR] Neither --database-url nor --offline-db-keys-file provided.\n"
                "Provide --database-url (or set DATABASE_URL) or --offline-db-keys-file.",
                file=sys.stderr
            )
            return 2

        result = check_application_collisions(seed_keys, db_keys)
        report = format_report(result)
        print(report)

        return 0 if result.is_safe else 1

    except Exception as e:
        print(f"[VALIDATION FAILED - SAFE ABORT] Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
