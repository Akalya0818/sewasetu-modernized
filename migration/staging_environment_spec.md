# Seva Setu — Disaster Recovery Linux Staging Environment Specification

## 1. Overview & Objectives

This specification defines a safe, reproducible Linux staging environment for executing the Seva Setu Disaster Recovery Staging Drill (Scenario B). It addresses the two root causes of the previous drill interruption:
1. **Windows Socket Restriction (WSAEACCES 10013):** Windows security/EDR policy blocked standalone binaries in user directories from creating outbound TCP connections to port 5432.
2. **PostgreSQL Client Incompatibility:** The bundled client binary was version 15.8, whereas the target Neon staging instance runs PostgreSQL 18.6.

---

## 2. Assessment of Existing Repository Environments

Inspection of the repository confirms that **no pre-existing approved runner image is currently configured** for the disaster recovery drill:
- **`docker-compose.yml` (`db` service):** Uses `postgres:15`. It is a development server image and does **not** contain Python, `psycopg2`, or the project drill scripts.
- **`app/Dockerfile` (`app` service):** Uses `python:3.11-slim` and installs Flask and application dependencies, but does **not** install PostgreSQL client tools (`pg_dump`, `pg_restore`, `psql`).
- **Official PostgreSQL Docker Images (`postgres:18`):** Contain PostgreSQL binaries, but do **not** include Python 3 or `psycopg2-binary`.

Therefore, a purpose-built environment combining **PostgreSQL 18 client binaries** and **Python 3.11 with `psycopg2`** is required.

---

## 3. Environment Specifications & Setup Commands

### Option A: Turnkey Containerized Runner (Recommended)

Using [migration/Dockerfile.staging_runner](file:///c:/Users/akals/Downloads/sewasetu-handout/sewasetu-handout/migration/Dockerfile.staging_runner):

```bash
# 1. Build the isolated drill runner image locally:
docker build -f migration/Dockerfile.staging_runner -t sewasetu-dr-runner:latest .

# 2. Verify installed client tool and Python versions inside the container:
docker run --rm sewasetu-dr-runner:latest pg_dump --version
docker run --rm sewasetu-dr-runner:latest python3 -c "import psycopg2; print('psycopg2 version:', psycopg2.__version__)"

# 3. Verify offline static unit test suite inside the container:
docker run --rm -v "$(pwd):/workspace" -w /workspace sewasetu-dr-runner:latest python3 -m unittest tests/test_staging_runner_static.py
```

### Option B: Native Linux VM / Bastion (Ubuntu 22.04/24.04 or Debian 12)

```bash
# 1. Install prerequisites and official PGDG repository:
sudo apt-get update && sudo apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release python3 python3-venv python3-pip netcat-traditional

sudo install -d /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg
echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list
echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg-testing main" | sudo tee -a /etc/apt/sources.list.d/pgdg.list

# 2. Install official PostgreSQL 18 client tools (Strictly required; no fallback to 17 or older):
sudo apt-get update
sudo apt-get install -y --no-install-recommends postgresql-client-18

# 3. Create isolated Python virtual environment:
python3 -m venv .venv-drill
source .venv-drill/bin/activate
pip install --upgrade pip
pip install psycopg2-binary==2.9.9 pytest==8.0.0
```

---

## 4. Preserved Safety Guards & Isolation Controls

The Linux runner environment strictly enforces all repository safety mechanisms:

1. **Target Host Allowlist:** Requires exact match against `EXPECTED_STAGING_HOST`. Rejects Render (`render.com`, `dpg-`, `onrender`), poolers (`-pooler`), and unverified hosts.
2. **Dedicated Target Database:** Exclusively restricted to `sewasetu_dr_drill_disposable`. Never restores into or touches `neondb`, `postgres`, `sewasetu_disposable_test`, or production databases.
3. **Independent Server Identity Proving:** Executes `SELECT current_database(), current_user, inet_server_addr()::text, version();` before database creation, before destructive restore, and before teardown.
4. **Pre-Existing Database Protection:** Refuses execution if `sewasetu_dr_drill_disposable` already exists.
5. **Teardown Ownership Guard:** Tracks `db_created_by_this_run == True`. Refuses to drop any database that was not created by the active execution run.
6. **Default Execution Refusal:** Refuses execution by default unless `--execute-authorized-drill` is explicitly passed.

---

## 5. Operator Preflight Checklist (Executed Before Creating Any Database)

The operator must verify each item in this checklist before authorizing drill execution:

### Step 1: Client Tool Version Check (Local / Offline)
```bash
pg_dump --version
pg_restore --version
psql --version
```
- [ ] `pg_dump` major version is `>= 18` (matches Neon staging engine).
- [ ] `pg_restore` major version is `>= 18`.
- [ ] `psql` major version is `>= 18`.

### Step 2: Python Environment & Static Tests (Local / Offline)
```bash
python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
python3 -m unittest tests/test_staging_runner_static.py
```
- [ ] `psycopg2` imports successfully.
- [ ] All 57 static unit tests pass with exit code `0`.

### Step 3: Target Host Guardrail & Environment Configuration
```bash
# Verify environment variables are populated (do NOT echo connection string or secrets)
echo "Target Host: $EXPECTED_STAGING_HOST"
```
- [ ] `EXPECTED_STAGING_HOST` is set to the exact direct Neon staging host.
- [ ] Hostname does NOT contain `-pooler`.
- [ ] Hostname does NOT contain `render.com` or `dpg-`.
- [ ] `STAGING_DATABASE_URL` connects to the administrative base DB (`neondb`), NOT production.

### Step 4: Network Egress & Port 5432 Reachability (Pre-Connection)
```bash
nc -zvw5 "$EXPECTED_STAGING_HOST" 5432
```
- [ ] TCP connection to port 5432 succeeds without OS socket restriction (`WSAEACCES 10013`).

### Step 5: Read-Only Server Identity & Database Absence Check
Execute a read-only query against the administrative base database:
```bash
python3 -c "
import os, psycopg2, urllib.parse
url = os.environ['STAGING_DATABASE_URL']
conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute('SELECT current_database(), current_user, inet_server_addr()::text, version();')
db, user, addr, ver = cur.fetchone()
print(f'Server Identity: DB={db}, User={user}, HostAddr={addr}')
print(f'Engine: {ver}')
cur.execute('SELECT 1 FROM pg_database WHERE datname = %s;', ('sewasetu_dr_drill_disposable',))
exists = cur.fetchone() is not None
cur.close()
conn.close()
assert not exists, 'SAFETY ABORT: sewasetu_dr_drill_disposable already exists!'
print('PASS: Target database does not pre-exist. Preflight verified.')
"
```
- [ ] Connected database is `neondb` (administrative base).
- [ ] Target engine confirms PostgreSQL 18.x.
- [ ] `sewasetu_dr_drill_disposable` does NOT exist in `pg_database`.

---

## 6. Current Blockers to Execution

1. **Explicit Operator Authorization Pending:** The disaster recovery drill is intentionally unexecuted.
2. **Environment Deployment:** The container image or Linux staging runner has been specified, but has not yet been booted or connected to Neon staging.
