# Seva Setu: Post-Migration Application Verification & Smoke Test Checklist

> [!NOTE]
> All smoke test procedures must use **ONLY synthetic test identities** (e.g. `'APP-2026-00001'`, mobile `'9876543210'`).
> **NEVER** modify, alter, or test using real inherited citizen records or live citizen identities.

---

## 1. Startup & Initialization Verification
- [ ] **Worker Concurrency Check:** Verify Gunicorn worker startup logs (`--workers 4`). Confirm that `init_tables_if_needed` logs `retained existing 100,00X application record(s)` and skips reseeding.
- [ ] **Duplicate Index Graceful Skip:** Verify that `idx_applications_app_no` does not trigger transaction aborts or uncaught exceptions during Gunicorn startup.

---

## 2. Web Application Smoke Test Checklist

### A. Health & Diagnostics Endpoints
- [ ] **Endpoint:** `GET /health`
  - Expected: HTTP `200 OK` with JSON `{"status": "healthy", "database": "connected"}`.
  - Verifies: Database connection pool is active and responding.

### B. Public Home Page
- [ ] **Endpoint:** `GET /`
  - Expected: HTTP `200 OK`. Renders public pension portal landing page, scheme statistics, and links.

### C. Citizen Status Lookup (Read-Only Verification)
- [ ] **Unknown Identifier Probe (Negative Test):**
  - Enter unregistered mobile `9999999999` and DOB `01011960`.
  - Expected: Flash alert `"No registered application found..."`.
  - Verifies: **Zero fake applications created**; database remains strictly read-only.
- [ ] **Synthetic Citizen Login (Positive Test):**
  - Enter synthetic mobile `9876543210` and DOB `15081955`.
  - Expected: Redirects to `/application/<id>`. Displays applicant `'Ananya Sharma'`.
- [ ] **Inherited Citizen Data Integrity (Aggregate Verification Only):**
  - Do NOT authenticate using real inherited citizen records or enter live citizen PII into the browser.
  - Verification of inherited citizen credentials must be performed exclusively via Phase 6 of `migration/postflight_validation.sql` (validating all 100,002 inherited portal users in aggregate via `SHA256(DDMMYYYY)` without exposing personal identities).

### D. New Application Submission Flow
- [ ] **Application Form:** `GET /apply`
  - Fill new synthetic application details (applicant name, age > 60, village, block, bank details).
- [ ] **OTP Verification:**
  - Submit mobile and request OTP.
  - Confirm OTP input field is blank initially (zero autofill).
  - Enter valid OTP code and complete submission.
  - Expected: Confirmation page with generated application number (`SSP26...`) and assigned ID (`> 100100`).
- [ ] **Acknowledgment PDF Generation:**
  - Click "Download Acknowledgment PDF" (`/ack/<new_id>`).
  - Expected: Valid PDF document rendered and downloaded successfully with Indic font support.

### E. Administrative Evaluation Portal
- [ ] **Admin Login:** `POST /admin/login`
  - Authenticate using assessor credentials.
  - Expected: Redirects to `/admin/dashboard`.
- [ ] **Application Decision Flow:**
  - Locate synthetic pending application.
  - Approve or Reject application (`/admin/approve/<id>` or `/admin/reject/<id>`).
  - Expected: Status updates to `APPROVED` or `REJECTED`, `decided_at` timestamp recorded, `decided_by` set to admin username.
