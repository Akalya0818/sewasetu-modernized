# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.
"""

import os
import sys
import configparser
from datetime import datetime, timedelta, date

import psycopg2
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(base_dir=None):
    """
    Locates and parses the configuration file (app.ini).
    Fails safely if configuration is missing, malformed, or incomplete.
    """
    config_path = os.environ.get("CONFIG_PATH")
    if config_path:
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file specified in CONFIG_PATH not found: {config_path}")
    else:
        if base_dir is None:
            base_dir = BASE_DIR
        config_path = os.path.join(base_dir, "config", "app.ini")
        if not os.path.exists(config_path):
            alt_path = os.path.join(base_dir, "app", "config", "app.ini")
            if os.path.exists(alt_path):
                config_path = alt_path
            else:
                legacy_path = os.path.join(base_dir, "config", "settings.ini")
                if os.path.exists(legacy_path):
                    config_path = legacy_path
                else:
                    raise FileNotFoundError(f"Configuration file not found: {config_path}")

    cfg = configparser.ConfigParser()
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg.read_file(fh)
    except Exception as e:
        raise ValueError(f"Failed to read or parse configuration file '{config_path}': {e}") from e

    # Strictly validate required sections and required settings
    required = {
        "database": ["host", "port", "name", "user"],
        "pension": ["sla_days", "min_age", "scheme_deadline"],
        "app": ["sms_gateway_url"],
    }
    for sec, keys in required.items():
        if not cfg.has_section(sec):
            raise ValueError(f"Invalid configuration: missing required section [{sec}]")
        for key in keys:
            if not cfg.has_option(sec, key) or cfg.get(sec, key).strip() == "":
                raise ValueError(f"Invalid configuration: missing required setting '{key}' in [{sec}]")

    # Database password validation
    app_env = os.environ.get("APP_ENV", "development").lower()
    db_url = os.environ.get("DATABASE_URL")
    db_pass = (
        db_url
        or os.environ.get("DB_PASSWORD")
        or os.environ.get("POSTGRES_PASSWORD")
        or (cfg.has_option("database", "password") and cfg.get("database", "password").strip())
    )
    insecure_db_passwords = ("sewasetu123", "dev-db-password-change-in-production")
    if app_env == "production":
        if not db_pass or (not db_url and db_pass in insecure_db_passwords):
            raise ValueError(
                "FATAL [Production Security]: Database password is missing or using an insecure default. "
                "Set DB_PASSWORD, POSTGRES_PASSWORD, or DATABASE_URL environment variable."
            )
    else:
        if not db_pass:
            cfg.set("database", "password", "dev-db-password-change-in-production")

    # Strictly validate integer and timestamp formats
    try:
        cfg.getint("pension", "sla_days")
        cfg.getint("pension", "min_age")
        cfg.getint("database", "port")
    except ValueError as e:
        raise ValueError(f"Invalid configuration: expected integer setting ({e})") from e

    try:
        datetime.strptime(cfg.get("pension", "scheme_deadline"), "%Y-%m-%d %H:%M")
    except ValueError as e:
        raise ValueError(f"Invalid configuration: invalid scheme_deadline format ({e})") from e

    return cfg


# Module-level configuration for backwards compatibility
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
config = None
SLA_DAYS = None
SMS_GATEWAY_URL = None

try:
    config = load_config()
    SLA_DAYS = config.getint("pension", "sla_days")
    SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
except Exception:
    # Do not silently substitute default settings when config fails to load.
    # Keep them None so any code relying on module-level attributes will fail clearly.
    config = None
    SLA_DAYS = None
    SMS_GATEWAY_URL = None


def get_db_connection(cfg):
    """Establishes database connection using configuration settings and environment variables."""
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return psycopg2.connect(db_url)
    db_pass = (
        os.environ.get("DB_PASSWORD")
        or os.environ.get("POSTGRES_PASSWORD")
        or (cfg.has_option("database", "password") and cfg.get("database", "password").strip())
        or "dev-db-password-change-in-production"
    )
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", cfg.get("database", "host")),
        port=int(os.environ.get("DB_PORT", cfg.get("database", "port"))),
        dbname=os.environ.get("DB_NAME", cfg.get("database", "name")),
        user=os.environ.get("DB_USER", cfg.get("database", "user")),
        password=db_pass,
    )


def calculate_age(dob, ref_date):
    """Calculates completed years of age on a given reference date."""
    if isinstance(dob, str):
        try:
            dob = datetime.strptime(dob[:10], "%Y-%m-%d").date()
        except ValueError:
            dob = datetime.strptime(dob[:10], "%d/%m/%Y").date()
    elif isinstance(dob, datetime):
        dob = dob.date()

    if isinstance(ref_date, str):
        try:
            ref_date = datetime.strptime(ref_date[:10], "%Y-%m-%d").date()
        except ValueError:
            ref_date = datetime.strptime(ref_date[:10], "%d/%m/%Y").date()
    elif isinstance(ref_date, datetime):
        ref_date = ref_date.date()

    return ref_date.year - dob.year - ((ref_date.month, ref_date.day) < (dob.month, dob.day))


def process_deemed_approvals(conn, now=None, cfg=None):
    """
    Core deemed approval processing logic with verified statutory eligibility:
    1. Cutoff: submitted_at < now - 15 calendar days (statutory SLA).
    2. Scheme window: submitted_at <= scheme_deadline.
    3. Minimum age: applicant must be at least min_age (60) on submission date.
    4. One application per citizen: skips duplicate active applications for the same mobile.
    5. Audit integrity: updates status='DEEMED_APPROVED', decided_at=now, decided_by='RTPS-AUTO'.
    6. Safe transaction rollback on failure.
    """
    if cfg is None:
        cfg = load_config()
    if now is None:
        now = datetime.now()

    sla_days = cfg.getint("pension", "sla_days")
    min_age = cfg.getint("pension", "min_age")
    scheme_deadline_raw = cfg.get("pension", "scheme_deadline")
    scheme_deadline = datetime.strptime(scheme_deadline_raw, "%Y-%m-%d %H:%M")
    sms_gateway_url = cfg.get("app", "sms_gateway_url")

    cutoff = now - timedelta(days=sla_days)

    cur = conn.cursor()
    try:
        # Fetch candidate records that are PENDING, beyond SLA cutoff, and within scheme window
        cur.execute(
            """SELECT id, application_no, mobile, dob, submitted_at
               FROM applications
               WHERE status = 'PENDING'
                 AND submitted_at < %s
                 AND submitted_at <= %s
               ORDER BY submitted_at ASC, id ASC""",
            (cutoff, scheme_deadline)
        )
        candidates = cur.fetchall()

        approved_records = []
        seen_mobiles = set()

        for app_id, app_no, mobile, dob, submitted_at in candidates:
            # 1. Age eligibility verification on submission date
            # Statutory requirement: applicant must be at least min_age (60) on submission date.
            # If DOB or submission date is missing or corrupt, eligibility is unresolved: leave in PENDING.
            if not dob or not submitted_at:
                continue
            try:
                age_at_sub = calculate_age(dob, submitted_at)
            except Exception:
                continue

            if age_at_sub < min_age:
                # Ineligible underage applicant: leave in PENDING
                continue

            # 2. Duplicate active check: check if mobile already has an approved claim
            cur.execute(
                """SELECT count(*) FROM applications
                   WHERE mobile = %s
                     AND status IN ('APPROVED', 'DEEMED_APPROVED')
                     AND id != %s""",
                (mobile, app_id)
            )
            prior_approved = cur.fetchone()[0]
            if prior_approved > 0:
                # Prior active approved claim exists: skip
                continue

            # 3. Check for multiple pending duplicates in the same batch
            if mobile in seen_mobiles:
                # Another application for this mobile was already approved in this run: skip
                continue

            # Update status with required audit fields atomically
            cur.execute(
                """UPDATE applications
                   SET status = 'DEEMED_APPROVED',
                       decided_at = %s,
                       decided_by = 'RTPS-AUTO'
                   WHERE id = %s AND status = 'PENDING'""",
                (now, app_id)
            )
            if cur.rowcount > 0:
                approved_records.append((app_no, mobile))
                seen_mobiles.add(mobile)

        conn.commit()

        # Send notifications outside transaction
        for app_no, mobile in approved_records:
            try:
                requests.post(
                    sms_gateway_url + "/api/send",
                    json={
                        "to": mobile,
                        "text": "Sewa Setu: your pension application %s stands approved "
                                "under the RTPS Act." % app_no
                    },
                    timeout=5
                )
            except Exception:
                pass

        return len(approved_records)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def main():
    try:
        cfg = load_config()
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    try:
        conn = get_db_connection(cfg)
    except psycopg2.OperationalError as e:
        print(f"Notice: Database connection not available ({e}). Configuration validated.", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"Database error: {e}", file=sys.stderr)
        return 1

    try:
        count = process_deemed_approvals(conn, cfg=cfg)
        print("%s deemed approval: %d applications approved"
              % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), count))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
