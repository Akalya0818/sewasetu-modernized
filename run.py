#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sewa Setu - Unified Local Application Launcher
Supports both PostgreSQL and local standalone SQLite execution.
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(REPO_ROOT, "app")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, APP_DIR)

from tests.conftest import SCHEMA_SQL
from app import app
import app as app_module

DB_PATH = os.path.join(REPO_ROOT, "scratch", "demo_sewasetu.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_demo_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.executescript(SCHEMA_SQL)
    
    cur.execute("SELECT COUNT(*) FROM applications")
    count = cur.fetchone()[0]
    
    if count == 0:
        now = datetime.now()
        apps = [
            ('APP-2026-00001', 'Ananya Sharma', '9876543210', '1955-08-15', 'Female', 'Widowed',
             None, None, 'Sonari Gaon', 'Sonari', '123456789012', 'SBIN0001234',
             'uploads/sample1.pdf', 'PENDING', now - timedelta(days=20)),
            ('APP-2026-00002', 'Bikash Das', '9876543211', '1970-05-05', 'Male', 'Married',
             None, None, 'Borgaon Cent', 'Borgaon', '234567890123', 'SBIN0001234',
             'uploads/sample2.pdf', 'PENDING', now - timedelta(days=20)),
            ('APP-2026-00003', 'Chitra Baruah', '9876543212', '1960-01-01', 'Female', 'Married',
             'Pranab Baruah', 'Self Employed', 'Namti Ali', 'Namti', '345678901234', 'SBIN0001234',
             'uploads/sample3.pdf', 'PENDING', now - timedelta(days=2)),
            ('APP-2026-00004', 'অনন্যা শৰ্মা', '9876543213', '1962-10-10', 'Female', 'Widowed',
             None, None, 'Khelua Habi', 'Khelua', '456789012345', 'SBIN0001234',
             'uploads/sample4.pdf', 'APPROVED', now - timedelta(days=30)),
        ]
        cur.executemany("""
            INSERT INTO applications (
                application_no, applicant_name, mobile, dob, gender, marital_status,
                husband_name, husband_employer, village, block, bank_account, ifsc,
                doc_path, status, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, apps)
        
        users = [
            ('9876543210', 'a0b9ebe5274a0d5578ca812cdf19f1bca06f5f0db60d3fe5cba4722c8908c3cf'),
            ('9876543211', '0f79fc2e22a61d9d97760e4c5d5b34d92ef0e051403df055deb26e89d53f6a7f'),
            ('9876543212', 'ec4a8549c88652d72725bccbd2a167c4d3f06eba7d7305ccf35cd807b12f19e7'),
            ('9876543213', '93dc5c7bda4ffe607ec667c79061aa3ef4b6b528d57f0428f272831409604b0b'),
        ]
        cur.executemany("""
            INSERT OR IGNORE INTO portal_users (mobile, password_hash) VALUES (?, ?)
        """, users)
        
        conn.commit()
    conn.close()

class DemoSqliteCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, params=None):
        q = query.replace('%s', '?')
        if params is None:
            return self.cursor.execute(q)
        return self.cursor.execute(q, params)

    def _convert_val(self, v):
        if isinstance(v, str) and len(v) >= 10:
            if len(v) >= 19 and v[4] == '-' and v[7] == '-' and (' ' in v or 'T' in v):
                try:
                    return datetime.fromisoformat(v)
                except Exception:
                    pass
            elif len(v) == 10 and v[4] == '-' and v[7] == '-':
                try:
                    return datetime.strptime(v, "%Y-%m-%d").date()
                except Exception:
                    pass
        return v

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        return tuple(self._convert_val(v) for v in row)

    def fetchall(self):
        rows = self.cursor.fetchall()
        return [tuple(self._convert_val(v) for v in row) for row in rows]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def close(self):
        self.cursor.close()

class DemoSqliteConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self):
        return DemoSqliteCursorWrapper(self.conn.cursor())

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

def get_demo_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return DemoSqliteConnectionWrapper(conn)

def try_postgres():
    try:
        conn = app_module.get_db()
        conn.close()
        return True
    except Exception:
        return False

if __name__ == "__main__":
    if try_postgres():
        print("[+] Connected to PostgreSQL database.")
    else:
        print("[*] PostgreSQL not detected locally. Initializing standalone SQLite database.")
        init_demo_db()
        app_module.get_db = get_demo_db
        print("[+] Standalone database initialized successfully.")

    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")

    print("\n" + "=" * 70)
    print("  SEWA SETU PORTAL - RUNNING")
    print("=" * 70)
    print(f"  Citizen Portal:              http://{host}:{port}")
    print(f"  Administrative Portal:       http://{host}:{port}/admin")
    print(f"  Admin Username:              assessor_admin")
    print(f"  Admin Password:              FJFZU49EMDmeG9YDuse0hqdRKBWOqGs3l4BBS+cUZ08")
    print(f"  Telecom SMS Gateway Console: http://{host}:{port}/__gateway/")
    print(f"  Health Check:                http://{host}:{port}/health")
    print(f"  Synthetic Citizen Login:     9876543210 (Password: 15081955)")
    print("=" * 70 + "\n")

    app.run(host=host, port=port, debug=False)
