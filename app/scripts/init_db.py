"""init_db.py - Database initialization for Sewa Setu.

Applies app/schema.sql and seed/synthetic_seed.sql to the target database.
Idempotent and safe: uses IF NOT EXISTS and ON CONFLICT DO NOTHING.
Zero real citizen records or PII.
"""

import os
import sys
import psycopg2

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(BASE_DIR)


import time


def init_database(conn=None):
    """Initializes schema and synthetic seed data on the database."""
    close_conn = False
    if conn is None:
        db_url = os.environ.get("DATABASE_URL")
        connected = False
        last_err = None
        for attempt in range(15):
            try:
                if db_url:
                    conn = psycopg2.connect(db_url)
                else:
                    import configparser
                    cfg = configparser.ConfigParser()
                    cfg_path = os.path.join(BASE_DIR, "config", "app.ini")
                    if os.path.exists(cfg_path):
                        cfg.read(cfg_path)
                    conn = psycopg2.connect(
                        host=os.environ.get("DB_HOST", cfg.get("database", "host", fallback="localhost")),
                        port=int(os.environ.get("DB_PORT", cfg.get("database", "port", fallback=5432))),
                        dbname=os.environ.get("DB_NAME", cfg.get("database", "name", fallback="sewasetu")),
                        user=os.environ.get("DB_USER", cfg.get("database", "user", fallback="sewasetu")),
                        password=os.environ.get("DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "dev-db-password-change-in-production")),
                    )
                connected = True
                break
            except Exception as e:
                last_err = e
                time.sleep(2)
        if not connected:
            print(f"Warning: Could not connect to database after retries: {last_err}")
            return
        close_conn = True

    try:
        cur = conn.cursor()
        schema_path = os.path.join(BASE_DIR, "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                cur.execute(f.read())
        conn.commit()

        # Attempt to ensure uniqueness index safely on application_no
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_app_no ON applications (application_no)")
            conn.commit()
        except Exception:
            conn.rollback()
            cur = conn.cursor()

        # Check existing records before seeding
        cur.execute("SELECT count(*) FROM applications")
        existing_apps = cur.fetchone()[0]

        # Only seed synthetic test data on fresh, empty databases
        if existing_apps == 0:
            seed_path = os.path.join(ROOT_DIR, "seed", "synthetic_seed.sql")
            if not os.path.exists(seed_path):
                seed_path = os.path.join(BASE_DIR, "synthetic_seed.sql")
            if os.path.exists(seed_path):
                with open(seed_path, "r", encoding="utf-8") as f:
                    cur.execute(f.read())
            conn.commit()
            print("Database schema and synthetic seed data initialized successfully.")
        else:
            conn.commit()
            print(f"Database schema initialized; retained existing {existing_apps} application record(s).")
        cur.close()
    finally:
        if close_conn:
            conn.close()


if __name__ == "__main__":
    init_database()
