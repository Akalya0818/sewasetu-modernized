import os
import sys
import tempfile
import sqlite3
import pytest

# Ensure app directory is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app')))

from app import app as flask_app


class SqliteCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, params=None):
        # Convert Postgres %s placeholders to SQLite ? placeholders
        q = query.replace('%s', '?')
        # SQLite compatibility for RETURNING id
        if params is None:
            return self.cursor.execute(q)
        # Convert any date/datetime/string objects as needed
        return self.cursor.execute(q, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def close(self):
        self.cursor.close()


class SqliteConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn
        self.close_count = 0
        self.closed = False

    def cursor(self):
        return SqliteCursorWrapper(self.conn.cursor())

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.close_count += 1
        self.closed = True


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_no  VARCHAR(20),
    applicant_name  VARCHAR(100),
    mobile          VARCHAR(15),
    dob             DATE,
    gender          VARCHAR(10),
    marital_status  VARCHAR(20),
    husband_name    VARCHAR(100),
    husband_employer VARCHAR(100),
    village         VARCHAR(100),
    block           VARCHAR(50),
    bank_account    VARCHAR(30),
    ifsc            VARCHAR(15),
    doc_path        VARCHAR(200),
    status          VARCHAR(20) DEFAULT 'PENDING',
    submitted_at    TIMESTAMP,
    decided_at      TIMESTAMP,
    decided_by      VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS portal_users (
    mobile        VARCHAR(15) PRIMARY KEY,
    password_hash VARCHAR(80)
);

CREATE TABLE IF NOT EXISTS otps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mobile     VARCHAR(15),
    code       VARCHAR(6),
    created_at TIMESTAMP
);
"""


@pytest.fixture
def test_db():
    """Isolated in-memory SQLite database mimicking PostgreSQL for safe testing."""
    raw_conn = sqlite3.connect(':memory:', check_same_thread=False)
    raw_conn.executescript(SCHEMA_SQL)
    wrapped = SqliteConnectionWrapper(raw_conn)
    yield wrapped
    raw_conn.close()


@pytest.fixture
def client(test_db, monkeypatch, tmp_path):
    """Flask test client configured with isolated DB and temporary upload dir."""
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    
    # Isolate upload dir
    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr('app.UPLOAD_DIR', upload_dir)
    
    # Patch get_db to return our isolated test DB
    monkeypatch.setattr('app.get_db', lambda: test_db)
    
    # Patch send_sms to avoid external HTTP requests during tests
    monkeypatch.setattr('app.send_sms', lambda mobile, text: None)

    with flask_app.test_client() as c:
        yield c
