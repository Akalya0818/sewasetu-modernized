-- ==============================================================================
-- Sewa Setu — Synthetic Seed Data for Development and Testing
-- ==============================================================================
-- This file contains ONLY synthetic test records.
-- It contains ZERO real citizen records or personally identifiable information.
-- ==============================================================================

INSERT INTO applications (
    application_no, applicant_name, mobile, dob, gender, marital_status,
    husband_name, husband_employer, village, block, bank_account, ifsc,
    doc_path, status, submitted_at
) VALUES
-- 1. Eligible pending applicant (age 70, submitted 20 days ago, past 15-day SLA -> eligible for deemed approval)
('APP-2026-00001', 'Ananya Sharma', '9876543210', '1955-08-15', 'Female', 'Widowed',
 NULL, NULL, 'Sonari Gaon', 'Sonari', '123456789012', 'SBIN0001234',
 'uploads/sample1.pdf', 'PENDING', NOW() - INTERVAL '20 days'),

-- 2. Ineligible pending applicant (age 55, submitted 20 days ago, underage -> deemed approval must skip)
('APP-2026-00002', 'Bikash Das', '9876543211', '1970-05-05', 'Male', 'Married',
 NULL, NULL, 'Borgaon Cent', 'Borgaon', '234567890123', 'SBIN0001234',
 'uploads/sample2.pdf', 'PENDING', NOW() - INTERVAL '20 days'),

-- 3. Recent pending applicant (age 66, submitted 2 days ago, within 15-day SLA -> deemed approval must skip)
('APP-2026-00003', 'Chitra Baruah', '9876543212', '1960-01-01', 'Female', 'Married',
 'Pranab Baruah', 'Self Employed', 'Namti Ali', 'Namti', '345678901234', 'SBIN0001234',
 'uploads/sample3.pdf', 'PENDING', NOW() - INTERVAL '2 days'),

-- 4. Approved applicant with Assamese Unicode name (tests Indic font PDF generation)
('APP-2026-00004', 'অনন্যা শৰ্মা', '9876543213', '1962-10-10', 'Female', 'Widowed',
 NULL, NULL, 'Khelua Habi', 'Khelua', '456789012345', 'SBIN0001234',
 'uploads/sample4.pdf', 'APPROVED', NOW() - INTERVAL '30 days');

-- Portal user accounts matching synthetic applications (password = DOB in DDMMYYYY format)
INSERT INTO portal_users (mobile, password_hash) VALUES
('9876543210', 'a0b9ebe5274a0d5578ca812cdf19f1bca06f5f0db60d3fe5cba4722c8908c3cf'), -- DOB 15081955
('9876543211', '0f79fc2e22a61d9d97760e4c5d5b34d92ef0e051403df055deb26e89d53f6a7f'), -- DOB 05051970
('9876543212', 'ec4a8549c88652d72725bccbd2a167c4d3f06eba7d7305ccf35cd807b12f19e7'), -- DOB 01011960
('9876543213', '93dc5c7bda4ffe607ec667c79061aa3ef4b6b528d57f0428f272831409604b0b')  -- DOB 10101962
ON CONFLICT (mobile) DO NOTHING;
