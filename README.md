# Sewa Setu — Old Age Pension Portal

> **Handover Documentation & Engineering Reference**  
> **Target System:** Government of Purvanchal, Department of Social Welfare  
> **Status:** Legacy Remediation Complete (Priorities 1–5). **NOT PRODUCTION-READY** due to unresolved upstream authentication architecture (see Section 6).

---

## 1. Executive Project Overview

Sewa Setu is the digital pension delivery portal for the Government of Purvanchal's Old Age Pension Scheme (*Indira Gandhi National Old Age Pension Scheme / State Social Security Pension*). The portal serves citizens aged 60 and above across six administrative blocks: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, and Khelua.

Following vendor disengagement on January 31, 2026, the application suffered from severe operational defects:
- Blocked citizen submissions (widowed applicants, strict upload limits, ambiguous date parsing)
- Runaway deemed approvals violating statutory eligibility rules
- Month-end database connection exhaustion caused by blocking PDF generation
- Critical data privacy leaks (public acknowledgment harvesting, dossier IDOR, admin CSRF)
- Unicode truncation and corruption of Assamese, Bengali, and Devanagari citizen names

This repository contains the stabilized, tested, and hardened codebase with **57 regression tests passing (100%)**.

---

## 2. System Architecture

- **Backend Framework**: Python 3.11+ / Flask 2.2.5
- **WSGI Server**: Gunicorn 20.1.0 (4 worker processes, 30s timeout)
- **Database**: PostgreSQL 15 (`sewasetu` database with tables `applications`, `portal_users`, `otps`)
- **PDF Engine**: `fpdf2` 2.7.4 with Unicode TrueType font support
- **Simulated Telecom Gateway**: Mock SMS gateway at port 8025 (`/__gateway/`) for OTP delivery
- **Batch Processing**: Nightly statutory deemed approval cron script (`app/scripts/deemed_approval.py`)

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

1. Build and launch all services:
   ```bash
   docker compose up --build
   ```
2. Access the services:
   - **Citizen Portal**: `http://localhost:8000`
   - **Administrative Portal**: `http://localhost:8000/admin`
   - **Simulated SMS Gateway Console**: `http://localhost:8000/__gateway/`
3. Database initialization:
   PostgreSQL automatically seeds `app/schema.sql` and `seed/seed.sql` on first initialization.

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
3. Configure `app/config/app.ini` with your local database credentials:
   ```ini
   [database]
   host = localhost
   port = 5432
   name = sewasetu
   user = postgres
   password = your_password
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
pytest tests/test_priority1_forms_and_journey.py -v   # Priority 1: Form usability & uploads
pytest tests/test_priority2_deemed_approval.py -v     # Priority 2: Deemed approvals & eligibility
pytest tests/test_priority3_performance_and_dos.py -v # Priority 3: PDF concurrency & DB leaks
pytest tests/test_priority4_security_and_privacy.py -v# Priority 4: Auth, IDOR, CSRF & SQLi
pytest tests/test_priority5_unicode_and_names.py -v   # Priority 5: Unicode & Indic font preservation
```

**Test Suite Status**: **57 passed, 0 failed, 0 skipped** across all 5 test files.

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

---

## 7. Production-Blocking Security Defect: Password Reset (`/status/reset`)

> [!CAUTION]
> **CRITICAL ARCHITECTURAL VULNERABILITY**: The application **MUST NOT be deployed to production** or exposed to public traffic in its current state.

### Vulnerability Analysis
While the SQL injection vulnerability on `/status/reset` was eliminated in Priority 4 via query parameterization (`SELECT mobile FROM portal_users WHERE mobile = %s`), the underlying authentication design remains inherently flawed:

1. **Deterministic Password Overwrite**:
   - The route resets portal passwords to the applicant's date of birth in `DDMMYYYY` format (`newpass = dob_val.strftime("%d%m%Y")`).
   - In rural blocks, dates of birth are widely known within villages or accessible through public voter rolls.
2. **Unauthenticated Denial-of-Service / Account Takeover**:
   - Any external user possessing an applicant's mobile number can trigger `/status/reset`.
   - The system immediately overwrites `portal_users.password_hash` with the birthdate hash without prior OTP verification.
   - An attacker can hijack access or permanently invalidate the citizen's existing credentials.

### Feasibility Assessment: SMS OTP Password Reset
A secure replacement was assessed against the existing codebase:
- **Limitation 1 (Department Circular Mandate)**: Lines 353 and 388 of `app/app.py` document that portal credentials were intentionally mandated as date-of-birth by departmental circular. The portal has no `/change-password` route or UI for citizens to set custom passwords.
- **Limitation 2 (Multi-Step Journey Required)**: A secure reset journey requires:
  1. Citizen enters mobile number $\rightarrow$ server verifies account and issues 6-digit OTP to `otps` table.
  2. Citizen enters OTP on a verification page $\rightarrow$ server validates OTP window.
  3. Citizen submits a new password $\rightarrow$ server updates hash.
- **Decision**: Implementing an ad-hoc OTP reset flow without formal approval from the Department of Social Welfare would break existing citizen communications and UI contracts.

**Action Required Prior to Production Deployment**:
Until the Department approves a formal password-management specification, `/status/reset` should be disabled at the reverse-proxy level (HTTP 403 / 404), or password resets must be processed in-person at block Seva Kendras.

---

## 8. Production Hardening & Pre-Deployment Checklist

Before deploying this application to a production server or cloud virtual machine, the following requirements must be implemented:

### A. Transport Security & Session Cookies
- **HTTPS Termination**: Deploy behind a TLS-terminating reverse proxy (Nginx, Caddy, or AWS ALB) with HSTS enabled.
- **Secure Cookie Flag**: In production `config/app.ini`, update:
  ```ini
  [app]
  session_cookie_secure = true
  session_cookie_samesite = Lax
  ```

### B. Secrets & Configuration Management
- **Secret Key**: Replace `secret_key = ssp-prod-key-2023-donotshare` in `config/app.ini` with a cryptographically random 64-character secret generated via `python -c "import secrets; print(secrets.token_hex(32))"`.
- **Admin Credentials**: Remove hardcoded `ADMIN_USERNAME` and `ADMIN_PASSWORD` from source code and load them from environment variables or a secrets manager.
- **Database Credentials**: Use a dedicated, least-privilege PostgreSQL database user.

### C. Database Connection Pooling
- Flask's `get_db()` currently opens a direct connection per request. In production under high concurrency (e.g., month-end surges), configure connection pooling via `psycopg2.pool.ThreadedConnectionPool` or deploy `pgbouncer` to prevent connection exhaustion.

### D. Unicode Font Availability for PDFs
- Ensure the production server or Docker container has Indic TrueType fonts installed:
  ```bash
  apt-get update && apt-get install -y fonts-noto-core fonts-indic
  ```
- Alternatively, set `indic_font_path` in `config/app.ini` pointing to a verified TTF font file.

### E. Nightly Deemed Approval Cron Scheduling
- Ensure the approval script runs nightly at 02:00 AM via system crontab:
  ```cron
  0 2 * * * /usr/local/bin/python /app/scripts/deemed_approval.py >> /var/log/sewasetu/approval.log 2>&1
  ```

---

## 9. Final Git Status & Verification

All changes are committed cleanly to the local repository:
```text
$ git status
On branch master
nothing to commit, working tree clean

$ pytest -v
======================= 57 passed, 61 warnings in 5.76s =======================
```
