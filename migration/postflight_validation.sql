-- ==============================================================================
-- Seva Setu: Post-Migration Database Integrity Validation Script
-- Purpose: Verify restoration completeness, sequence state, and index validity.
-- Safety: Aggregate queries only. Zero personal data or credentials printed.
-- ==============================================================================

\set ON_ERROR_STOP on

SELECT '======================================================' AS title;
SELECT 'SEVA SETU: POST-MIGRATION INTEGRITY VALIDATION' AS title;
SELECT '======================================================' AS title;

-- 1. Total Record Counts
SELECT 
    (SELECT COUNT(*) FROM applications) AS total_applications,
    (SELECT COUNT(*) FROM portal_users) AS total_portal_users,
    (SELECT COUNT(*) FROM otps) AS total_otps;

-- 2. Duplicate Application Numbers Verification (Must be 0)
SELECT 
    COUNT(application_no) AS duplicate_groups_count,
    COALESCE(SUM(cnt), 0) AS duplicate_rows_count
FROM (
    SELECT application_no, COUNT(*) AS cnt
    FROM applications
    GROUP BY application_no
    HAVING COUNT(*) > 1
) dups;

-- 3. Application Status Distribution (Aggregate only)
SELECT 
    status,
    COUNT(*) AS count_by_status
FROM applications
GROUP BY status
ORDER BY count_by_status DESC;

-- 4. Primary Key Range & Sequence Alignment
SELECT 
    MIN(a.id) AS min_id,
    MAX(a.id) AS max_id,
    s.last_value AS current_sequence_value,
    s.is_called AS sequence_is_called,
    CASE WHEN s.is_called THEN s.last_value + 1 ELSE s.last_value END AS next_generated_id,
    CASE 
        WHEN (CASE WHEN s.is_called THEN s.last_value + 1 ELSE s.last_value END) > MAX(a.id) THEN 'ALIGNED (SAFE - Next ID exceeds MAX(id))'
        ELSE 'MISALIGNED (RISK OF ID COLLISION)'
    END AS sequence_status
FROM applications a
CROSS JOIN applications_id_seq s
GROUP BY s.last_value, s.is_called;

-- 5. Unique Index Verification
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

-- 6. Inherited Citizen Authentication Verification (Aggregate Check)
-- Validates that SHA256(DOB as DDMMYYYY) matches portal_users.password_hash
-- for the 100,002 inherited applications, confirming citizen login will succeed.
-- Zero citizen names, mobiles, DOBs, or hashes are output.
SELECT 
    COUNT(*) AS authenticated_inherited_records_count,
    CASE 
        WHEN COUNT(*) = 100002 THEN '100% CREDENTIAL INTEGRITY VERIFIED (EXACT 100,002)'
        WHEN COUNT(*) > 100002 THEN '100% CREDENTIAL INTEGRITY VERIFIED (EXCEEDS 100,002)'
        ELSE 'CREDENTIAL MISMATCH DETECTED'
    END AS auth_verification_status
FROM applications a
JOIN portal_users u ON a.mobile = u.mobile
WHERE a.id <= 100002
  AND encode(sha256(to_char(a.dob, 'DDMMYYYY')::bytea), 'hex') = u.password_hash;

-- 7. Verification of Preserved Legitimate Test Submissions & Relocated Range
SELECT 
    COUNT(*) AS preserved_relocated_applications_count,
    MIN(id) AS min_relocated_id,
    MAX(id) AS max_relocated_id
FROM applications
WHERE id >= 100100;

-- 8. Final Go-No-Go Assertion Block
DO $$
DECLARE
    v_apps INTEGER;
    v_users INTEGER;
    v_dups INTEGER;
    v_auth_matches INTEGER;
    v_idx_count INTEGER;
    v_inherited_apps INTEGER;
    v_relocated_apps INTEGER;
    v_seq_last BIGINT;
    v_seq_called BOOLEAN;
    v_max_id INTEGER;
    v_next_val BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_apps FROM applications;
    SELECT COUNT(*) INTO v_users FROM portal_users;
    SELECT COUNT(*) INTO v_inherited_apps FROM applications WHERE id <= 100002;
    SELECT COUNT(*) INTO v_relocated_apps FROM applications WHERE id >= 100100;
    
    SELECT COUNT(*) INTO v_dups FROM (SELECT application_no FROM applications GROUP BY application_no HAVING COUNT(*) > 1) d;
    
    SELECT COUNT(*) INTO v_auth_matches FROM applications a JOIN portal_users u ON a.mobile = u.mobile
        WHERE a.id <= 100002 AND encode(sha256(to_char(a.dob, 'DDMMYYYY')::bytea), 'hex') = u.password_hash;
        
    SELECT COUNT(*) INTO v_idx_count 
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_class t ON t.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname = 'applications' AND c.relname = 'idx_applications_app_no' AND n.nspname = 'public'
      AND i.indisunique = true AND i.indisvalid = true;

    SELECT last_value, is_called INTO v_seq_last, v_seq_called FROM applications_id_seq;
    SELECT MAX(id) INTO v_max_id FROM applications;
    IF v_seq_called THEN
        v_next_val := v_seq_last + 1;
    ELSE
        v_next_val := v_seq_last;
    END IF;

    -- Assertions:
    IF v_inherited_apps != 100002 THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Expected exactly 100,002 inherited applications (IDs <= 100002), found %', v_inherited_apps;
    END IF;

    IF v_apps != (100002 + v_relocated_apps) THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Total applications (%) != 100,002 + relocated (%)', v_apps, v_relocated_apps;
    END IF;

    IF v_dups > 0 THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Duplicate application numbers found (% duplicates)!', v_dups;
    END IF;

    IF v_idx_count = 0 THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Unique index idx_applications_app_no is missing or invalid!';
    END IF;

    IF v_auth_matches != 100002 THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Credential verification failed! Expected 100,002 matches, found %', v_auth_matches;
    END IF;

    IF v_next_val <= v_max_id THEN
        RAISE EXCEPTION 'POST-VALIDATION FAILED: Next sequence value (%) does not exceed MAX(id) (%)', v_next_val, v_max_id;
    END IF;

    RAISE NOTICE 'POST-VALIDATION COMPLETE: All checks passed. Exactly % applications (100,002 inherited + % relocated), % portal_users, 0 duplicates. Next sequence value: % (MAX(id): %).',
        v_apps, v_relocated_apps, v_users, v_next_val, v_max_id;
END $$;
