-- ==============================================================================
-- Seva Setu: Preflight Database Validation Script
-- Purpose: Read-only inspection of database state before restoration.
-- Safety: Strictly read-only queries. Zero modifications. Zero PII printed.
-- ==============================================================================

\set ON_ERROR_STOP on

SELECT '======================================================' AS title;
SELECT 'SEVA SETU: PREFLIGHT DATABASE INTEGRITY VALIDATION' AS title;
SELECT '======================================================' AS title;

-- 1. Total Application, Portal User, and OTP Counts
SELECT 
    (SELECT COUNT(*) FROM applications) AS total_applications,
    (SELECT COUNT(*) FROM portal_users) AS total_portal_users,
    (SELECT COUNT(*) FROM otps) AS total_otps;

-- 2. Duplicate Application Numbers Count
SELECT 
    COUNT(application_no) AS distinct_duplicate_app_nos,
    COALESCE(SUM(cnt), 0) AS total_duplicate_rows
FROM (
    SELECT application_no, COUNT(*) AS cnt
    FROM applications
    GROUP BY application_no
    HAVING COUNT(*) > 1
) dup_summary;

-- 3. Verify All Duplicates Match Expected Synthetic Range
-- (If any non-synthetic application number is duplicated, flag immediately)
SELECT 
    COUNT(*) AS unexpected_duplicates_count
FROM (
    SELECT application_no, COUNT(*) AS cnt
    FROM applications
    WHERE application_no NOT IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
    GROUP BY application_no
    HAVING COUNT(*) > 1
) unexp;

-- 4. Check for Conflicting Decisions Among Synthetic Duplicates
-- (Flags if an application number has multiple different non-PENDING statuses)
SELECT 
    COUNT(*) AS synthetic_status_conflict_count
FROM (
    SELECT application_no
    FROM applications
    WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
      AND status != 'PENDING'
    GROUP BY application_no
    HAVING COUNT(DISTINCT status) > 1
) conflicts;

-- 5. Application Status Breakdown (Aggregate only)
SELECT 
    status,
    COUNT(*) AS count_by_status
FROM applications
GROUP BY status
ORDER BY count_by_status DESC;

-- 6. Primary Key ID Space Range & Boundary Verification
SELECT 
    MIN(id) AS min_application_id,
    MAX(id) AS max_application_id,
    COUNT(*) AS total_rows,
    (SELECT COUNT(*) FROM applications WHERE id > 100002) AS rows_exceeding_seed_max,
    (SELECT COUNT(*) FROM applications WHERE id >= 100100) AS rows_in_target_relocation_space
FROM applications;

-- 7. Current Primary Key Sequence State
SELECT 
    'applications_id_seq' AS sequence_name,
    last_value,
    is_called
FROM applications_id_seq;

-- 8. Check if Unique Index on application_no Already Exists
SELECT 
    c.relname AS index_name,
    i.indisunique AS is_unique,
    i.indisvalid AS is_valid
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname = 'applications'
  AND c.relname = 'idx_applications_app_no'
  AND n.nspname = 'public';

-- 9. Check Schema Dependencies (Foreign Keys, Triggers, Views)
SELECT 
    (SELECT COUNT(*) FROM pg_constraint WHERE confrelid = 'applications'::regclass) AS foreign_keys_referencing_applications,
    (SELECT COUNT(*) FROM pg_trigger WHERE tgrelid = 'applications'::regclass AND NOT tgisinternal) AS custom_triggers_on_applications;

-- 10. Reconciled Expected Count Projection
SELECT 
    (SELECT COUNT(*) FROM applications) AS current_total_applications,
    (SELECT COUNT(*) - 4 FROM applications WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')) AS synthetic_duplicates_to_remove,
    (SELECT COUNT(*) FROM applications) - (SELECT COUNT(*) - 4 FROM applications WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')) AS surviving_pre_migration_applications,
    100002 + ((SELECT COUNT(*) FROM applications) - (SELECT COUNT(*) - 4 FROM applications WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004'))) AS exact_expected_post_migration_applications,
    (SELECT COUNT(*) FROM portal_users) AS current_total_portal_users,
    100002 + (SELECT COUNT(*) FROM portal_users) AS exact_expected_post_migration_portal_users;

-- 11. Preflight Go-No-Go Assertion Block
DO $$
DECLARE
    v_unexpected_dups INTEGER;
    v_conflicts INTEGER;
    v_total_apps INTEGER;
    v_overflow_ids INTEGER;
    v_high_ids INTEGER;
    v_fk_count INTEGER;
    v_trigger_count INTEGER;
BEGIN
    -- Check 1: Unexpected duplicates
    SELECT COUNT(*) INTO v_unexpected_dups
    FROM (
        SELECT application_no
        FROM applications
        WHERE application_no NOT IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
        GROUP BY application_no
        HAVING COUNT(*) > 1
    ) t;

    IF v_unexpected_dups > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Unexpected duplicate application numbers detected outside synthetic test set! Manual human review required.';
    END IF;

    -- Check 2: Status conflicts
    SELECT COUNT(*) INTO v_conflicts
    FROM (
        SELECT application_no
        FROM applications
        WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
          AND status != 'PENDING'
        GROUP BY application_no
        HAVING COUNT(DISTINCT status) > 1
    ) c;

    IF v_conflicts > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Conflicting decision statuses detected across duplicate copies of the same synthetic application! Manual review required.';
    END IF;

    -- Check 3: ID space boundary check (applications with id > 100002)
    SELECT COUNT(*) INTO v_overflow_ids
    FROM applications
    WHERE id > 100002;

    IF v_overflow_ids > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Existing applications with ID > 100002 detected (found % rows)! Relocation logic requires IDs <= 100002.', v_overflow_ids;
    END IF;

    -- Check 4: ID space collision check (target space 100100+)
    SELECT COUNT(*) INTO v_high_ids
    FROM applications
    WHERE id >= 100100;

    IF v_high_ids > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Target relocation ID space (100100+) is already occupied by % rows! Manual review required.', v_high_ids;
    END IF;

    -- Check 5: Foreign key dependency check
    SELECT COUNT(*) INTO v_fk_count
    FROM pg_constraint
    WHERE confrelid = 'applications'::regclass;

    IF v_fk_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Found % foreign keys referencing applications table! Schema modification blocked.', v_fk_count;
    END IF;

    -- Check 6: Custom trigger check
    SELECT COUNT(*) INTO v_trigger_count
    FROM pg_trigger
    WHERE tgrelid = 'applications'::regclass AND NOT tgisinternal;

    IF v_trigger_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAILED: Found % custom triggers on applications table! Review required.', v_trigger_count;
    END IF;

    SELECT COUNT(*) INTO v_total_apps FROM applications;
    RAISE NOTICE 'PREFLIGHT PASSED: Database state verified safe for migration (Total applications: %).', v_total_apps;
END $$;
