-- ==============================================================================
-- Seva Setu: Database Restoration & Reconciliation Migration Script
--
-- ATOMICITY & RECOVERY SPECIFICATION:
-- Executed inside a single PostgreSQL transaction block (BEGIN ... COMMIT).
-- Uses \set ON_ERROR_STOP on. Any failure at any step immediately aborts
-- and triggers a complete PostgreSQL ROLLBACK of all table modifications.
--
-- CRITICAL NOTE ON POSTGRESQL SEQUENCES:
-- PostgreSQL sequences are non-transactional. If this migration transaction
-- aborts after seed/seed.sql executes its setval(100012) command, all table
-- rows roll back to their pre-migration state, but applications_id_seq will
-- remain set to 100012. If a rollback occurs, run the recovery cleanup:
-- SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);
--
-- TARGET DATASET: seed/seed.sql (100,002 applications, IDs 1 to 100002)
-- PRESERVED DATA: Legitimate test submissions and surviving synthetic records
--                 relocated safely above the seed space (IDs >= 100100).
-- ==============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ------------------------------------------------------------------------------
-- PHASE 1: PRECONDITION ASSERTION & BASELINE COUNT RECORDING
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    v_unexpected_dups INTEGER;
    v_conflicts INTEGER;
    v_overflow_ids INTEGER;
    v_conflicting_ids INTEGER;
    v_fk_count INTEGER;
    v_trigger_count INTEGER;
    v_pre_apps INTEGER;
    v_synth_dups INTEGER;
    v_expected_apps INTEGER;
    v_pre_users INTEGER;
    v_expected_users INTEGER;
BEGIN
    -- Check A: Verify no duplicates exist outside the known synthetic application numbers
    SELECT COUNT(*) INTO v_unexpected_dups
    FROM (
        SELECT application_no
        FROM applications
        WHERE application_no NOT IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
        GROUP BY application_no
        HAVING COUNT(*) > 1
    ) unexp;

    IF v_unexpected_dups > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: Found unexpected duplicates outside synthetic range. Review required.';
    END IF;

    -- Check B: Verify no conflicting decision statuses exist across duplicate copies of the same synthetic application
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
        RAISE EXCEPTION 'MIGRATION ABORTED: Conflicting decision statuses detected across duplicate synthetic records! Manual review required.';
    END IF;

    -- Check C: Verify that no applications currently exist with ID > 100002
    SELECT COUNT(*) INTO v_overflow_ids
    FROM applications
    WHERE id > 100002;

    IF v_overflow_ids > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: Applications with ID > 100002 exist (% rows). Relocation requires IDs <= 100002.', v_overflow_ids;
    END IF;

    -- Check D: Verify that high ID space (100100+) is currently unoccupied
    SELECT COUNT(*) INTO v_conflicting_ids
    FROM applications
    WHERE id >= 100100;

    IF v_conflicting_ids > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: ID space 100100+ is already occupied (found % rows).', v_conflicting_ids;
    END IF;

    -- Check E: Verify no foreign keys reference applications
    SELECT COUNT(*) INTO v_fk_count
    FROM pg_constraint
    WHERE confrelid = 'applications'::regclass;

    IF v_fk_count > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: Found % foreign keys referencing applications! Schema modification blocked.', v_fk_count;
    END IF;

    -- Check F: Verify no custom triggers exist on applications
    SELECT COUNT(*) INTO v_trigger_count
    FROM pg_trigger
    WHERE tgrelid = 'applications'::regclass AND NOT tgisinternal;

    IF v_trigger_count > 0 THEN
        RAISE EXCEPTION 'MIGRATION ABORTED: Found % custom triggers on applications table! Review required.', v_trigger_count;
    END IF;

    -- Calculate exact expected post-migration counts dynamically
    SELECT COUNT(*) INTO v_pre_apps FROM applications;
    SELECT COUNT(*) - 4 INTO v_synth_dups FROM applications WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004');
    v_expected_apps := 100002 + (v_pre_apps - v_synth_dups);

    SELECT COUNT(*) INTO v_pre_users FROM portal_users;
    v_expected_users := 100002 + v_pre_users;

    RAISE NOTICE 'Phase 1: Preconditions verified. Expected post-migration applications: % (100,002 + % surviving), expected portal_users: % (100,002 + % existing).',
        v_expected_apps, (v_pre_apps - v_synth_dups), v_expected_users, v_pre_users;
END $$;

-- ------------------------------------------------------------------------------
-- PHASE 2: SYNTHETIC DEDUPLICATION (PRESERVING STATUS UPDATES & LEGITIMATE APPS)
-- ------------------------------------------------------------------------------
-- For the 4 synthetic application numbers, retain the single best copy:
-- Priority 1: Decided status (APPROVED or REJECTED) over PENDING.
-- Priority 2: Most recent decided_at timestamp.
-- Priority 3: Highest id (most recent submission).
-- All other duplicate copies are removed.
WITH ranked_dups AS (
    SELECT 
        id,
        application_no,
        ROW_NUMBER() OVER (
            PARTITION BY application_no
            ORDER BY 
                CASE WHEN status != 'PENDING' THEN 0 ELSE 1 END,
                decided_at DESC NULLS LAST,
                id DESC
        ) AS rn
    FROM applications
    WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002', 'APP-2026-00003', 'APP-2026-00004')
)
DELETE FROM applications
WHERE id IN (
    SELECT id FROM ranked_dups WHERE rn > 1
);

-- ------------------------------------------------------------------------------
-- PHASE 3: PRIMARY KEY RELOCATION (CLEARING ID SPACE 1..100002 FOR SEED IMPORT)
-- ------------------------------------------------------------------------------
-- Shift surviving applications (4 synthetic + legitimate test submissions)
-- to a high ID space starting at 100,101.
UPDATE applications
SET id = id + 100100
WHERE id <= 100002;

-- ------------------------------------------------------------------------------
-- PHASE 4: IMPORT ORIGINAL INHERITED DATASET (100,002 RECORDS)
-- ------------------------------------------------------------------------------
-- Uses \ir to resolve seed/seed.sql relative to this script directory.
-- seed/seed.sql contains:
--   COPY applications (...) FROM stdin;  (IDs 1 to 100002)
--   COPY portal_users (...) FROM stdin;  (100,002 citizen accounts)
--   SELECT setval('applications_id_seq', 100012);
\ir ../seed/seed.sql

-- ------------------------------------------------------------------------------
-- PHASE 5: REALIGN PRIMARY KEY SEQUENCE PAST ALL EXISTING RECORDS
-- ------------------------------------------------------------------------------
-- PostgreSQL setval(seq, val, true) behavior:
-- Setting val to MAX(id) with is_called=true guarantees that the next call
-- to nextval() returns MAX(id) + 1, strictly exceeding MAX(id).
SELECT setval(
    pg_get_serial_sequence('applications', 'id'),
    (SELECT COALESCE(MAX(id), 1) FROM applications),
    true
);

-- ------------------------------------------------------------------------------
-- PHASE 6: CONSTRUCT PERMANENT UNIQUE INDEX ON application_no
-- ------------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_applications_app_no;
CREATE UNIQUE INDEX idx_applications_app_no ON applications (application_no);

-- ------------------------------------------------------------------------------
-- PHASE 7: POST-IMPORT INTEGRITY ASSERTION CHECK (EXACT RECONCILED COUNTS)
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    v_actual_apps INTEGER;
    v_actual_users INTEGER;
    v_actual_dups INTEGER;
    v_relocated_apps INTEGER;
    v_expected_apps INTEGER;
    v_expected_users INTEGER;
    v_pre_users_count INTEGER;
    v_seq_last BIGINT;
    v_seq_called BOOLEAN;
    v_max_id INTEGER;
    v_next_val BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_actual_apps FROM applications;
    SELECT COUNT(*) INTO v_actual_users FROM portal_users;
    SELECT COUNT(*) INTO v_relocated_apps FROM applications WHERE id >= 100100;
    
    -- The exact expected count of applications is 100,002 (seed) + surviving relocated records
    v_expected_apps := 100002 + v_relocated_apps;

    SELECT COUNT(*) INTO v_actual_dups
    FROM (
        SELECT application_no FROM applications GROUP BY application_no HAVING COUNT(*) > 1
    ) d;

    SELECT last_value, is_called INTO v_seq_last, v_seq_called FROM applications_id_seq;
    SELECT MAX(id) INTO v_max_id FROM applications;

    IF v_seq_called THEN
        v_next_val := v_seq_last + 1;
    ELSE
        v_next_val := v_seq_last;
    END IF;

    -- Exact Assertions:
    IF v_actual_apps != v_expected_apps THEN
        RAISE EXCEPTION 'MIGRATION ASSERTION FAILED: Expected exactly % applications (100002 + % relocated), found %',
            v_expected_apps, v_relocated_apps, v_actual_apps;
    END IF;

    IF v_actual_users < 100006 THEN
        RAISE EXCEPTION 'MIGRATION ASSERTION FAILED: Expected at least 100,006 portal_users, found %', v_actual_users;
    END IF;

    IF v_actual_dups > 0 THEN
        RAISE EXCEPTION 'MIGRATION ASSERTION FAILED: Duplicate application numbers still present (% duplicates)', v_actual_dups;
    END IF;

    IF v_next_val <= v_max_id THEN
        RAISE EXCEPTION 'MIGRATION ASSERTION FAILED: Next sequence value (%) does not exceed MAX(id) (%)', v_next_val, v_max_id;
    END IF;

    RAISE NOTICE 'MIGRATION SUCCESS: Exactly % applications, % portal_users, 0 duplicates. Next generated ID will be % (MAX(id): %).',
        v_actual_apps, v_actual_users, v_next_val, v_max_id;
END $$;

COMMIT;
