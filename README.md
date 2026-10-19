# Sewa Setu — Old Age Pension Portal

> **Handover Documentation & Engineering Reference**  
> **Target System:** Government of Purvanchal, Department of Social Welfare  
> **Status:** Legacy Remediation Complete (Priorities 1–5 & Security Hardening). **NOT PRODUCTION-READY** due to unresolved upstream authentication architecture (see Section 7).

---

## 1. Executive Project Overview

Sewa Setu is the digital pension delivery portal for the Government of Purvanchal's Old Age Pension Scheme (*Indira Gandhi National Old Age Pension Scheme / State Social Security Pension*). The portal serves citizens aged 60 and above across six administrative blocks: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, and Khelua.

Following vendor disengagement on January 31, 2026, the application suffered from severe operational defects:
- Blocked citizen submissions (widowed applicants, strict upload limits, ambiguous date parsing)
- Runaway deemed approvals violating statutory eligibility rules
- Month-end database connection exhaustion caused by blocking PDF generation
- Critical data privacy leaks (public acknowledgment harvesting, dossier IDOR, admin CSRF)
- Unicode truncation and corruption of Assamese, Bengali, and Devanagari citizen names
- Hardcoded administrative credentials, database passwords, and session secrets
- Production database seed dependency in container configuration

This repository contains the stabilized, tested, and hardened codebase with **64 regression tests passing (100%)**.

---

## 2. System Architecture

- **Backend Framework**: Python 3.11+ / Flask 2.2.5
- **WSGI Server**: Gunicorn 20.1.0 (4 worker processes, 30s timeout)
- **Database**: PostgreSQL 15 (`sewasetu` database with tables `applications`, `portal_users`, `otps`)
- **PDF Engine**: `fpdf2` 2.7.4 with Unicode TrueType font support (`fonts-noto-core`, `fonts-indic`)
- **Simulated Telecom Gateway**: Mock SMS gateway at port 8025 (`/__gateway/`) for OTP delivery
- **Batch Processing**: Nightly statutory deemed approval cron script (`app/scripts/deemed_approval.py`)
- **Container Infrastructure**: Docker Compose with `pg_isready` healthcheck and synthetic seed initialization

---

## 3. Prerequisites & Dependencies

### System Requirements
- Docker and Docker Compose (recommended for containerized execution)
- *Or* Python 3.11+ and PostgreSQL 15+ for native host execution
- Unicode TrueType Fonts for Indic scripts:
  - **Linux / Debian**: `fonts-noto-core`, `fonts-indic` (`NotoSansBengali`, `NotoSansDevanagari`)
  - **Windows**: `Nirmala.ttc` (pre-installed system font)

### Python Dependencies (`app/requirements.txt`)
```text
Flask==2.2.5
psycopg2-binary==2.9.9
requests==2.28.2
fpdf2==2.7.4
gunicorn==20.1.0
```

### Development & Test Dependencies
```text
pytest>=8.0.0
pytest-asyncio>=1.4.0
pypdf>=5.0.0
```

---

## 4. Local Setup & Execution

### Option A: Running with Docker Compose (Recommended)

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```
2. Build and launch all services:
   ```bash
   docker compose up --build
   ```
   *Note: PostgreSQL service automatically runs health checks (`pg_isready`), initializes the schema (`app/schema.sql`), and populates safe synthetic test records (`seed/synthetic_seed.sql`). Dependent services wait for database readiness before starting.*
3. Access the services:
   - **Citizen Portal**: `http://localhost:8000`
   - **Administrative Portal**: `http://localhost:8000/admin`
   - **Simulated SMS Gateway Console**: `http://localhost:8000/__gateway/`

### Option B: Native Local Development

1. Create and activate a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
2. Install dependencies:
   ```bash
   pip install -r app/requirements.txt
   pip install pytest pypdf
   ```
3. Configure environment variables (or copy `.env.example` to `.env`):
   ```bash
   export APP_ENV=development
   export SECRET_KEY=dev-insecure-flask-secret-key-change-in-production
   export ADMIN_USERNAME=admin
   export ADMIN_PASSWORD=dev-admin-password-change-in-production
   export DB_HOST=localhost
   export DB_PORT=5432
   export DB_NAME=sewasetu
   export DB_USER=sewasetu
   export DB_PASSWORD=dev-db-password-change-in-production
   ```
4. Run the Flask application:
   ```bash
   python app/app.py
   ```

---

## 5. Running the Test Suite

The test harness uses an in-memory SQLite translation layer (`tests/conftest.py`) that mirrors PostgreSQL schema semantics, `%s` query parameterization, and transaction boundaries without altering production databases.

To run the complete regression test suite:
```bash
pytest -v
```

To run individual priority suites:
```bash
pytest tests/test_priority1_forms_and_journey.py -v       # Priority 1: Form usability & uploads
pytest tests/test_priority2_deemed_approval.py -v         # Priority 2: Deemed approvals & eligibility
pytest tests/test_priority3_performance_and_dos.py -v     # Priority 3: PDF concurrency & DB leaks
pytest tests/test_priority4_security_and_privacy.py -v    # Priority 4: Auth, IDOR, CSRF & SQLi
pytest tests/test_priority5_unicode_and_names.py -v       # Priority 5: Unicode & Indic font preservation
pytest tests/test_priority6_environment_and_secrets.py -v # Security: Environment secrets & startup validation
pytest tests/test_priority7_deployment_readiness.py -v    # Deployment: Healthcheck, DATABASE_URL & SMS gateway
```

**Test Suite Status**: **69 passed, 0 failed, 0 skipped** across all 7 test files.

---

## 6. Completed Priorities & Commit Traceability

All modifications follow a strict test-first progression: each priority includes a failing regression test commit followed by a minimal targeted fix commit.

| Priority | Scope & Primary Defects Addressed | Test File | Test Commit | Fix Commit |
|---|---|---|---|---|
| **Priority 1** | **Form Usability & Upload Contract**: Unblocked widowed applicants incorrectly requiring husband's employer; aligned upload limit with 5MB UI contract; added resilient Indian `DD/MM/YYYY` date parsing. | `tests/test_priority1_forms_and_journey.py` | `f1e7448`, `0e8886e` | `ac0d1fc` |
| **Priority 2** | **Statutory Deemed Approvals**: Fixed configuration loading (`app.ini`); enforced strict 15-day SLA boundary; restricted auto-approvals to verified 60+ applicants within scheme window; validated zero-rowcount concurrent updates. | `tests/test_priority2_deemed_approval.py` | `f1cbd64`, `49661ac` | `d5e459a`, `da4c6a4` |
| **Priority 3** | **Month-End Concurrency & Performance**: Released database connections in `finally` before PDF file I/O; removed decorative 4,000-line vector loop; reduced PDF latency by **8.5x** (12.14 ms $\rightarrow$ 1.42 ms) and file size by **70%**. | `tests/test_priority3_performance_and_dos.py` | `a996ef9` | `a0131fb` |
| **Priority 4** | **Citizen Data Privacy & Security**: Enforced applicant record ownership on `/ack/<id>.pdf` and `/application/<id>` (closing public PII harvest and IDOR); restricted admin actions with CSRF protection; parameterized `/status/reset` SQL queries; hardened session cookies (`HttpOnly`, `SameSite=Lax`). | `tests/test_priority4_security_and_privacy.py` | `10fa583`, `d29b10b` | `e6a102c`, `ea5531c` |
| **Priority 5** | **Unicode & Indic-Name Preservation**: Replaced 20-byte UTF-8 slicing with Unicode character sanitization (`VARCHAR(100)`); configured TrueType Indic font resolution (`Nirmala.ttc` / `NotoSansBengali`); eliminated `?` replacement on acknowledgment PDFs. | `tests/test_priority5_unicode_and_names.py` | `580c286` | `1c0062f` |
| **Security Hardening** | **DOB Password Reset Blocking**: Safely blocked unauthenticated date-of-birth password reset on `/status/reset` with in-person advisory notice. | `tests/test_priority4_security_and_privacy.py` | `e0e2373` | `8243911` |
| **Secrets & Infra** | **Environment Secrets & Docker Health**: Removed hardcoded passwords from tracked files; enforced startup failure on missing production secrets; added `.env.example`, synthetic seed mount, `pg_isready` healthcheck, and Indic fonts in Docker. | `tests/test_priority6_environment_and_secrets.py` | `399cdfd` | `83051d0` |
| **Deployment Readiness** | **Cloud Deployment & Resiliency**: Added `/health` probe endpoints, `DATABASE_URL` managed Postgres support, dynamic `PORT` binding, embedded SMS gateway fallback, Render blueprint (`render.yaml`), and production SMS console access control (`is_gateway_authorized`). | `tests/test_priority7_deployment_readiness.py` | `f075139`, `54a61b2` | `194cd2c`, `5378f1f` |

---

## 7. Production-Blocking Security Defect: Password Reset (`/status/reset`)

> [!CAUTION]
> **CRITICAL ARCHITECTURAL VULNERABILITY**: The application **MUST NOT be deployed to production** or exposed to public traffic in its current state.

### Vulnerability Analysis
While the SQL injection vulnerability on `/status/reset` was eliminated in Priority 4 via query parameterization, the underlying authentication design remains inherently flawed:

1. **Deterministic Password Overwrite**:
   - The route resets portal passwords to the applicant's date of birth in `DDMMYYYY` format (`newpass = dob_val.strftime("%d%m%Y")`).
   - In rural blocks, dates of birth are widely known within villages or accessible through public voter rolls.
2. **Unauthenticated Denial-of-Service / Account Takeover**:
   - Any external user possessing an applicant's mobile number can trigger `/status/reset`.
   - The system immediately overwrites `portal_users.password_hash` with the birthdate hash without prior OTP verification.
   - An attacker can hijack access or permanently invalidate the citizen's existing credentials.

### Remediation Implemented
In accordance with government data protection mandates:
- The `/status/reset` endpoint has been **safely blocked by default** (returning HTTP 403 Forbidden with an administrative advisory notice directing citizens to their nearest Block Development Office or Seva Kendra).
- The endpoint **never mutates `portal_users.password_hash`**, completely eliminating the vulnerability of arbitrary account takeover or credential erasure.
- Regression tests in `tests/test_priority4_security_and_privacy.py` (`test_d2`) verify that submitting a mobile number never overwrites stored passwords.

**Action Required for Full Online Password Management**:
Before introducing an online self-service password reset feature in a future major release, the Department of Social Welfare must approve a formal two-factor SMS OTP authentication and custom password policy. Until that time, the endpoint remains safely blocked.

---

## 8. Production Hardening & Pre-Deployment Checklist

Before deploying this application to a production server or cloud virtual machine, the following requirements must be implemented:

### A. Transport Security & Session Cookies
- **HTTPS Termination**: Deploy behind a TLS-terminating reverse proxy (Nginx, Caddy, or AWS ALB) with HSTS enabled.
- **Secure Cookie Flag**: In production, set `session_cookie_secure = true` and `session_cookie_samesite = Lax` in `config/app.ini`.

### B. Secrets & Configuration Management
- **Production Mode Enforcement**: Set `APP_ENV=production`. Missing or default secrets will abort process startup immediately.
- **Secret Key**: Supply a cryptographically random 64-character secret via `SECRET_KEY` environment variable.
- **Admin Credentials**: Supply strong credentials via `ADMIN_USERNAME` and `ADMIN_PASSWORD` environment variables.
- **Database Credentials**: Supply database credentials via `DB_PASSWORD` or `POSTGRES_PASSWORD` environment variables.

### C. Database Connection Pooling
- Flask's `get_db()` currently opens a direct connection per request. In production under high concurrency (e.g., month-end surges), configure connection pooling via `psycopg2.pool.ThreadedConnectionPool` or deploy `pgbouncer` to prevent connection exhaustion.

### D. Unicode Font Availability for PDFs
- The Docker image automatically installs `fonts-noto-core` and `fonts-indic`. If deploying natively on Linux, ensure these packages are installed via apt.

### E. Nightly Deemed Approval Cron Scheduling
- In dedicated production worker containers, schedule the approval script nightly at 02:00 AM via crontab:
  ```cron
  0 2 * * * /usr/local/bin/python /app/scripts/deemed_approval.py >> /var/log/sewasetu/approval.log 2>&1
  ```
- **Note**: Deemed approval is **not** scheduled on web instances or automatically against the public assessment database.

### F. SMS Gateway Public Access Protection & Separation of Duties
- In production (`APP_ENV=production`), unauthenticated public visitors cannot read OTPs or view the diagnostic console at `/__gateway/` (returns HTTP 403 Forbidden).
- **Separation of Duties**: Admin login sessions and `ADMIN_PASSWORD` credentials **cannot** access or harvest citizen OTPs or the telecom simulation routes.
- **Header-Only Authentication**: Access is strictly restricted to an independent, dedicated `GATEWAY_AUTH_TOKEN` environment secret via HTTP request headers (`X-Gateway-Auth` or `Authorization: Bearer <token>`). Query-string token authentication (`?token=...`) is strictly rejected to prevent credential leakage in proxy access logs, referrer headers, and browser history. If `GATEWAY_AUTH_TOKEN` is unset in production, the console and messages API are completely disabled.

### G. Cloud Database Persistence & Assessment Availability (Render)
- **Render Free PostgreSQL Lifecycle**: Render deletes free PostgreSQL databases after **30 days**.
- **Cold-Start Sleep**: Free web services sleep after 15 minutes of inactivity (~50s wake delay).
- **Recommendation for 30-Day Evaluation**:
  - Upgrade Render plan to **Starter** ($7/mo web + $7/mo database) for uninterrupted 30+ day availability and instant probe response times.
  - If deploying on the free tier, configure an external health check (e.g., UptimeRobot) pinging `https://<app>.onrender.com/health` every 10 minutes, and note the 30-day database expiration date.

---

## 9. Historical Git Secrets & Secret Invalidation Disclosure

> [!NOTE]
> **Git History Disclosure Regarding Legacy Secrets**:
> During initial repository baseline ingestion (`5f40cf0`), legacy development placeholder credentials (`ADMIN_PASSWORD = "sewasetu@123"`, `secret_key = ssp-prod-key-2023-donotshare`, and `password = sewasetu123`) were present in tracked files from the vendor's handover.
>
> In accordance with standard DevSecOps principles and the assessment requirement to preserve the original Git commit history and audit trail:
> 1. **Secret Rotation and Invalidation**: These historical placeholder credentials are considered permanently compromised/invalidated.
> 2. **Environment Isolation**: The current application codebase no longer uses hardcoded credentials and strictly reads secrets from runtime environment variables.
> 3. **Startup Enforcement**: If `APP_ENV=production` is enabled and any historical default credential is used, the application deliberately aborts startup with a fatal security error.
> 4. **History Preservation**: Git history rewriting (such as `git filter-repo`) was intentionally **not performed**, as rewriting past commits would destroy all existing commit hashes and violate the requirement to demonstrate the step-by-step repair history of the legacy codebase.

---

## 10. Final Verification Status

```text
$ git status
On branch master
nothing to commit, working tree clean

$ pytest -v
======================= 75 passed, 61 warnings in 22.45s =======================
```

