"""test_priority6_environment_and_secrets.py - Regression tests for environment-based secrets and safe startup.

Covers:
1. Production startup failure when required secrets (SECRET_KEY, ADMIN_PASSWORD, DB_PASSWORD) are missing or default.
2. Development-mode fallback to clearly documented development-only defaults.
3. Dynamic administrative password resolution from environment variables.
4. Absence of hardcoded production credentials in tracked configuration files (app.ini).
5. Deemed approval batch job environment-based configuration safety.
"""

import os
import sys
import subprocess
import configparser
import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
CONFIG_PATH = os.path.join(APP_DIR, 'config', 'app.ini')
DEEMED_APPROVAL_PATH = os.path.join(APP_DIR, 'scripts', 'deemed_approval.py')


def test_tracked_app_ini_has_no_hardcoded_secrets():
    """
    Ensure app/config/app.ini does NOT contain hardcoded passwords or secret keys.
    """
    assert os.path.exists(CONFIG_PATH), f"Config file must exist at {CONFIG_PATH}"
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)

    # Database password must be empty in tracked config
    db_pass = cfg.get("database", "password", fallback="").strip()
    assert db_pass == "", f"app/config/app.ini must not have hardcoded database password! Found: {db_pass}"

    # App secret key must be empty in tracked config
    secret_key = cfg.get("app", "secret_key", fallback="").strip()
    assert secret_key == "", f"app/config/app.ini must not have hardcoded secret key! Found: {secret_key}"


def test_production_fails_when_secret_key_missing():
    """
    In APP_ENV=production, missing or default SECRET_KEY must trigger an immediate fatal startup error.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["SECRET_KEY"] = ""
    env["ADMIN_PASSWORD"] = "strong-admin-pass-12345"
    env["DB_PASSWORD"] = "strong-db-pass-12345"

    code = "import app"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True
    )
    assert result.returncode != 0, "Application must not start in production with missing SECRET_KEY"
    assert "FATAL [Production Security]" in result.stderr or "SECRET_KEY" in result.stderr


def test_production_fails_when_admin_password_missing():
    """
    In APP_ENV=production, missing or default ADMIN_PASSWORD must trigger an immediate fatal startup error.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["SECRET_KEY"] = "super-secret-random-production-key-987654321"
    env["ADMIN_PASSWORD"] = ""
    env["DB_PASSWORD"] = "strong-db-pass-12345"

    code = "import app"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True
    )
    assert result.returncode != 0, "Application must not start in production with missing ADMIN_PASSWORD"
    assert "FATAL [Production Security]" in result.stderr or "ADMIN_PASSWORD" in result.stderr


def test_production_fails_when_db_password_missing():
    """
    In APP_ENV=production, missing or default DB_PASSWORD must trigger an immediate fatal startup error.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["SECRET_KEY"] = "super-secret-random-production-key-987654321"
    env["ADMIN_PASSWORD"] = "strong-admin-pass-12345"
    env["DB_PASSWORD"] = ""

    code = "import app"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True
    )
    assert result.returncode != 0, "Application must not start in production with missing DB_PASSWORD"
    assert "FATAL [Production Security]" in result.stderr or "DB_PASSWORD" in result.stderr


def test_development_startup_succeeds_with_defaults():
    """
    In APP_ENV=development (or default), the application should start cleanly with documented defaults.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "development"
    env.pop("SECRET_KEY", None)
    env.pop("ADMIN_PASSWORD", None)
    env.pop("DB_PASSWORD", None)

    code = "import app; print('SUCCESS', app.app.secret_key is not None)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True
    )
    assert result.returncode == 0, f"Development mode failed to start:\n{result.stderr}"
    assert "SUCCESS True" in result.stdout


def test_admin_login_uses_configured_environment_password(client):
    """
    Verify that when ADMIN_PASSWORD is set via environment, admin_login checks it.
    """
    import app as app_module
    original_pass = app_module.ADMIN_PASSWORD
    try:
        app_module.ADMIN_PASSWORD = "custom-env-admin-pass-xyz"
        
        # Test incorrect password
        resp_bad = client.post("/admin", data={"username": "admin", "password": "wrong-password"})
        assert resp_bad.status_code == 401
        
        # Test correct password
        resp_good = client.post("/admin", data={"username": "admin", "password": "custom-env-admin-pass-xyz"}, follow_redirects=False)
        assert resp_good.status_code == 302
        assert "/admin/applications" in resp_good.headers.get("Location", "") or "/admin" in resp_good.headers.get("Location", "")
    finally:
        app_module.ADMIN_PASSWORD = original_pass


def test_deemed_approval_production_requires_database_password():
    """
    deemed_approval.py in APP_ENV=production must fail if database password is not configured.
    """
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["DB_PASSWORD"] = ""
    env["POSTGRES_PASSWORD"] = ""

    result = subprocess.run(
        [sys.executable, DEEMED_APPROVAL_PATH],
        capture_output=True,
        text=True,
        env=env
    )
    assert result.returncode == 1, "deemed_approval must fail in production without DB_PASSWORD"
    assert "Database password" in result.stderr or "Configuration error" in result.stderr
