# Seva Setu: Production Database Restoration & Maintenance Plan

> [!CAUTION]
> **STATUS: NOT EXECUTED — PENDING SEPARATE HUMAN APPROVAL**
> No production database connections or modifications may be initiated without explicit user review and authorization.

---

## 1. Overview and Operational Window
- **Target System:** Seva Setu Pension Portal (Render Managed PostgreSQL & Web Service)
- **Inherited Dataset:** `seed/seed.sql` (100,002 citizen records, 100,002 portal-user accounts)
- **Current Live State:** ~240 synthetic test records (duplicates of 4 test IDs) + legitimate test submissions
- **Estimated Maintenance Window:** 10 minutes total (Active migration runtime: ~30 to 45 seconds)

---

## 2. Operator Maintenance Window Execution Checklist

The operator must execute the maintenance window steps in strict sequential order. Do not skip or reorder steps.

> [!CAUTION]
> **PRODUCTION MIGRATION REMAINS UNAUTHORIZED UNTIL EXPLICIT HUMAN OPERATOR APPROVAL.**
> Successful local static tests and isolated staging integration tests verify code and SQL logic only; they do NOT constitute production verification. No production command may be issued without an authorized maintenance window.

---

### Step 0: Target Host & Database Verification (Pre-Flight Confirmation)
*Never rely on `$DATABASE_URL` alone as proof of the target environment.*

- [ ] **Confirm Database Identity and Connection String:**
  - **Bash:**
    ```bash
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT current_database(), inet_server_addr(), current_user;"
    ```
  - **PowerShell:**
    ```powershell
    psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -c "SELECT current_database(), inet_server_addr(), current_user;"
    ```
  - **GO Condition:** Output confirms the expected production database name, server IP/host, and authorized user.
  - **ABORT Condition:** Hostname, database name, or user does not match the authorized production target, or connection fails.

---

### Step 1: Capture and Verify Pre-Maintenance Snapshot (Client Workstation)
*Do not rely on file size (> 0 bytes) alone. A truncated or aborted dump can produce an un-restorable non-zero file.*

- [ ] **1A. Capture Full Logical Binary Backup (Exact Filename):**
  - **Bash:**
    ```bash
    BACKUP_FILE="pre_migration_backup_$(date +%Y%m%d_%H%M%S).dump"
    pg_dump "$DATABASE_URL" -Fc -v -f "$BACKUP_FILE"
    ```
  - **PowerShell:**
    ```powershell
    $BACKUP_FILE = "pre_migration_backup_$((Get-Date).ToString('yyyyMMdd_HHmmss')).dump"
    pg_dump $env:DATABASE_URL -Fc -v -f $BACKUP_FILE
    ```
- [ ] **1B. Verify Archive Integrity & Table of Contents (TOC):**
  - Verify that the custom-format archive is structurally sound and readable:
    - **Bash:**
      ```bash
      pg_restore --list "$BACKUP_FILE" > backup_toc.txt
      echo "Exit code: $?"
      grep -E "TABLE DATA.*(applications|portal_users)" backup_toc.txt
      ```
    - **PowerShell:**
      ```powershell
      pg_restore --list $BACKUP_FILE | Out-File -Encoding utf8 backup_toc.txt
      $LASTEXITCODE
      Select-String -Path backup_toc.txt -Pattern "TABLE DATA.*(applications|portal_users)"
      ```
  - > [!IMPORTANT]
  - > **Archive-List Verification vs. Proven Successful Restore:**
  - > Running `pg_restore --list` proves archive readability, valid header magic, and TOC structure. It does **NOT** prove a successful restoration into a live database (which has **NOT** been performed).
  - **GO Condition:** `pg_restore --list` exits with code `0`, and output explicitly contains `TABLE DATA` entries for both `applications` and `portal_users`.
  - **ABORT Condition:** Exit code is non-zero, archive is unreadable, or table data entries for `applications` or `portal_users` are missing.

- [ ] **1C. Export Baseline Immutable Business Keys:**
  - **Bash:**
    ```bash
    psql "$DATABASE_URL" -c "\copy (SELECT application_no, mobile FROM applications) TO 'baseline_keys.csv' WITH CSV HEADER"
    ```
  - **PowerShell:**
    ```powershell
    psql $env:DATABASE_URL -c "\copy (SELECT application_no, mobile FROM applications) TO 'baseline_keys.csv' WITH CSV HEADER"
    ```
  - **GO Condition:** `baseline_keys.csv` exists and contains header + rows matching live count.
  - **ABORT Condition:** CSV creation fails or row count is 0.

---

### Step 2: Stop / Suspend Incoming Application Traffic (Verified Render Procedure)
*Do not assume suspending the Render Web Service guarantees zero active database connections.*

- [ ] **2A. Suspend Web Service in Render Dashboard:**
  - Navigate to Render Dashboard -> Select Web Service (`b4b-eval-portal`).
  - Open service **Settings** (or click header `...` menu) -> Click **Suspend Web Service**.
  - Confirm suspension.
  - Verify public endpoint: Visiting the service URL immediately receives HTTP `503 Service Unavailable`.

- [ ] **2B. Inspect Active Database Sessions:**
  - Suspending the web container stops the Gunicorn processes, but lingering pooled, background, or external sessions may persist.
  - Run session inspection query:
    - **Bash:**
      ```bash
      psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
      SELECT pid, usename, client_addr, application_name, state, query 
      FROM pg_stat_activity 
      WHERE datname = current_database() AND pid <> pg_backend_pid();"
      ```
    - **PowerShell:**
      ```powershell
      psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -c "
      SELECT pid, usename, client_addr, application_name, state, query 
      FROM pg_stat_activity 
      WHERE datname = current_database() AND pid <> pg_backend_pid();"
      ```
  - > [!WARNING]
  - > **No Automatic Connection Termination:**
  - > Do NOT terminate connections automatically. The operator must investigate any unexpected sessions and obtain explicit authorization before executing `SELECT pg_terminate_backend(pid);`.
  - **GO Condition:** Public URL returns HTTP 503, and active non-operator database connections count equals `0`.
  - **ABORT Condition:** Public endpoint is still serving traffic, or unauthorized external sessions are actively writing to the database.

---

### Step 3: Run Pre-Migration Read-Only Collision Check Utility
*Offline fixture passes test tooling logic only; production collision status is UNVERIFIED until this step runs.*

- [ ] **Execute Collision Check Against Live Database:**
  - **Bash:**
    ```bash
    python migration/pre_migration_collision_check.py --database-url "$DATABASE_URL" --seed-file seed/seed.sql
    ```
  - **PowerShell:**
    ```powershell
    python migration/pre_migration_collision_check.py --database-url $env:DATABASE_URL --seed-file seed/seed.sql
    ```
  - **GO Condition:** Output explicitly reports `[STATUS: PASS] Zero unexpected application number collisions detected.` and utility exits with code `0`.
  - **ABORT Condition:** Utility exits with code `1` (collision detected) or code `2` (parsing error, unreadable file, connection failure). Immediately abort maintenance.

---

### Step 4: Execute Read-Only Preflight Verification SQL

- [ ] **Execute Preflight Script:**
  - **Bash:**
    ```bash
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migration/preflight_validation.sql
    ```
  - **PowerShell:**
    ```powershell
    psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f migration/preflight_validation.sql
    ```
  - **GO Condition:** Script completes with 0 errors; asserts 0 unexpected duplicate application numbers, 0 status conflicts, 0 IDs `>= 100100`, and 0 foreign keys locking `applications`.
  - **ABORT Condition:** Any assertion fails or script exits with non-zero error.

---

### Step 5: Execute Migration (Single Atomic Transaction)

- [ ] **Execute Atomic Migration Script:**
  - **Bash:**
    ```bash
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migration/migrate_database.sql
    ```
  - **PowerShell:**
    ```powershell
    psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f migration/migrate_database.sql
    ```
  - **GO Condition:** Transaction commits cleanly; output ends with `COMMIT` and zero errors.
  - **ABORT Condition:** Any error occurs during execution.
    - *Action on in-flight abort:* Because the transaction rolled back automatically, table rows are untouched. Execute Scenario A sequence realignment only:
      `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migration/recovery_procedure.sql`

---

### Step 6: Execute Postflight Verification SQL

- [ ] **Execute Postflight Validation Script:**
  - **Bash:**
    ```bash
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migration/postflight_validation.sql
    ```
  - **PowerShell:**
    ```powershell
    psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f migration/postflight_validation.sql
    ```
  - **GO Condition:** Exactly 100,002 inherited applications (`id <= 100002`), exactly 100,002 portal users authenticating via SHA256(DDMMYYYY), 0 duplicate application numbers, `idx_applications_app_no` valid, sequence realigned past `MAX(id)`.
  - **ABORT Condition:** Any postflight assertion fails.
    - *Action on post-commit failure:* Do not restore blindly. Halt immediately and convene human disaster-recovery decision team.

---

### Step 7: Restore Application Traffic (Verified Render Procedure)
*CRITICAL GATE: Only execute traffic restoration after Step 6 postflight validation passes completely.*

- [ ] **Resume Web Service in Render Dashboard:**
  - Render Dashboard -> Web Service (`b4b-eval-portal`) -> Click **Resume Web Service**.
  - Inspect startup logs: confirm `init_tables_if_needed: retained existing 100,00X application record(s), skipping seed`.
  - **GO Condition:** Service status transitions to `Available` (green); startup logs confirm zero schema collisions and zero re-seeding.
  - **ABORT Condition:** Container fails to start or crashes in crash-loop.

---

### Step 8: Perform Post-Migration Synthetic Smoke Tests

- [ ] **Execute Smoke Tests (Synthetic Identities Only):**
  - Verify `GET /health` returns HTTP `200 OK` with JSON `{"status": "healthy", "database": "connected"}`.
  - Verify `GET /` renders public landing page.
  - Verify synthetic citizen login (`9876543210` / `15081955`) displays Ananya Sharma.
  - Verify new synthetic submission flow and PDF download.
  - Verify admin assessor login and application decision flow.
  - *Note: NEVER enter or test real citizen credentials.*
  - **GO Condition:** All synthetic checks succeed; zero citizen PII queried or exposed.
  - **ABORT Condition:** Health check fails, application returns 500 errors, or database operations timeout.

---

### Step 9: Maintenance Sign-Off

- [ ] **Complete Maintenance Window:**
  - Archive `pre_migration_backup_*.dump` and validation output logs to secure cold storage.
  - Confirm portal operational status and notify stakeholders.

---

## 3. Phase Details & Reference Commands

### Phase A: Pre-Maintenance Snapshot & Baseline Capture (Client-Side)
Executed in Step 1 of Operator Checklist.

### Phase B: Read-Only Preflight Verification
Executed in Step 4 of Operator Checklist.
**Go / No-Go Decision Rules:**
1. **Zero Unexpected Duplicates:** Duplicate `application_no` count outside `APP-2026-00001` through `APP-2026-00004` must be **0**.
2. **Zero Status Conflicts:** Duplicate copies of the 4 synthetic records must have identical or non-conflicting statuses.
3. **ID Boundary Check:** Zero applications with `id > 100002` and zero applications with `id >= 100100`.
4. **Dependency Check:** Zero foreign keys and zero custom triggers referencing `applications`.
*If any assertion fails, the maintenance window is immediately aborted.*

### Phase C: Migration Execution (Atomic Transaction)
Executed in Step 5 of Operator Checklist.
**Internal Transaction Phases Executed Atomically:**
- **Phase 1:** Re-verifies all preconditions inside transaction.
- **Phase 2:** Deduplicates the 4 synthetic application numbers (retaining decided status over PENDING, latest decision timestamp, highest ID).
- **Phase 3:** Shifts surviving records by `+100,100` (moving live IDs to `100101..100340`), freeing IDs `1..100002`.
- **Phase 4:** Streams original `seed/seed.sql` (100,002 applications + 100,002 portal users) via native `psql` CopyIn protocol.
- **Phase 5:** Realigns sequence counter past all existing records via `SELECT setval(..., MAX(id), true);`.
- **Phase 6:** Drops invalid index and constructs `CREATE UNIQUE INDEX idx_applications_app_no ON applications (application_no)`.
- **Phase 7:** Evaluates exact mathematical assertions before issuing `COMMIT;`.

### Phase D: Postflight Verification & Smoke Tests
Executed in Steps 6 and 8 of Operator Checklist.

---

## 6. Phase E: Disaster Recovery & Rollback Plan

### Scenario 1: Transaction Aborted Mid-Flight (In-Flight Failure — NOT A RESTORE)
- **Context:** If an error occurs during `migrate_database.sql`, PostgreSQL rolls back all row modifications automatically due to `BEGIN ... COMMIT` with `ON_ERROR_STOP=1`. Table data remains in its pre-migration state.
- **Why this is NOT a restore:** No backup dump or restore commands are needed. The only artifact surviving the rollback is the non-transactional PostgreSQL sequence counter (`setval`).
- **Remediation Action:** Run the sequence realignment query directly:
```sql
SELECT setval(
    pg_get_serial_sequence('applications', 'id'),
    (SELECT COALESCE(MAX(id), 1) FROM applications),
    true
);
```

---

### Scenario 2: Complete Disaster Recovery (Post-Commit Rollback — DESTRUCTIVE & UNTESTED)

> [!CAUTION]
> **CRITICAL WARNING: SCENARIO 2 RESTORE IS DESTRUCTIVE AND UNTESTED.**
> `pg_restore --clean` drops and replaces all tables. A physical restore drill of this procedure has **NOT BEEN PERFORMED** against a live database instance.
> **DO NOT ASSUME DELTA-EXPORT AND RE-INJECTION ARE PROVEN SAFE.**
> This procedure must NEVER be triggered automatically or without explicit human authorization.
>
> **OPERATIONAL BLOCKER & RECOMMENDATION:**
> Because the physical restore and delta re-injection workflows have not been proven in an end-to-end drill, this is classified as a pre-production risk/blocker. A complete dry-run restore drill must be conducted on an isolated test database prior to production cutover.

#### Mandatory Pre-Conditions for Scenario 2:
1. **Explicit Incident Authorization:** An authorized human incident commander must formally declare a disaster and authorize full database restoration.
2. **Target Host Confirmation:** Execute `SELECT current_database(), inet_server_addr();` to independently prove the target is the intended database.
3. **Backup Archive Verification:** Confirm `pg_restore --list "$BACKUP_FILE"` exits with code `0`.
4. **Delta Preservation Verification:** Confirm `delta_applications.csv` and `delta_portal_users.csv` were exported successfully before dropping tables.

#### Step-by-Step Operator Runbook (Terminal Shell Commands Only):

```bash
# Step 1: Export delta records submitted during maintenance (client-side \copy, no server /tmp)
# Must execute BEFORE running pg_restore:
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
CREATE TEMP TABLE baseline_keys (application_no VARCHAR(20), mobile VARCHAR(15));
\copy baseline_keys FROM 'baseline_keys.csv' WITH CSV HEADER;

-- Export delta applications submitted after baseline snapshot (explicit column order)
\copy (SELECT application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by FROM applications WHERE application_no NOT IN (SELECT application_no FROM baseline_keys)) TO 'delta_applications.csv' WITH CSV HEADER;

-- Export delta portal users created after baseline snapshot (explicit column order)
\copy (SELECT mobile, password_hash FROM portal_users WHERE mobile NOT IN (SELECT mobile FROM baseline_keys)) TO 'delta_portal_users.csv' WITH CSV HEADER;
"

# Step 2: Destructive restore of pre-migration physical dump
# WARNING: Drops existing tables. Specify exact backup filename, never a wildcard.
pg_restore --clean --if-exists --no-owner --no-privileges -d "$DATABASE_URL" "$BACKUP_FILE"

# Step 3: Realign sequence after restore
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);"

# Step 4: Re-inject delta submissions with fresh SERIAL IDs
# Assigns fresh serial IDs to avoid primary key collisions with restored baseline
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
CREATE TEMP TABLE staging_delta_apps (LIKE applications INCLUDING DEFAULTS);
ALTER TABLE staging_delta_apps DROP COLUMN id;
\copy staging_delta_apps (application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by) FROM 'delta_applications.csv' WITH CSV HEADER;

INSERT INTO applications (
    application_no, applicant_name, mobile, dob, gender, marital_status,
    husband_name, husband_employer, village, block, bank_account,
    ifsc, doc_path, status, submitted_at, decided_at, decided_by
)
SELECT application_no, applicant_name, mobile, dob, gender, marital_status,
       husband_name, husband_employer, village, block, bank_account,
       ifsc, doc_path, status, submitted_at, decided_at, decided_by
FROM staging_delta_apps
ON CONFLICT (application_no) DO NOTHING;

CREATE TEMP TABLE staging_delta_users (LIKE portal_users);
\copy staging_delta_users (mobile, password_hash) FROM 'delta_portal_users.csv' WITH CSV HEADER;
INSERT INTO portal_users (mobile, password_hash)
SELECT mobile, password_hash FROM staging_delta_users
ON CONFLICT (mobile) DO NOTHING;

SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);
"
```
