"""test_priority7_deployment_readiness.py - Regression tests for public deployment readiness.

Covers:
1. Healthcheck probe endpoints (/health, /healthz) for platform liveness & DB monitoring.
2. Graceful healthcheck reporting when database connection is down.
3. Embedded SMS gateway fallback console and API (/__gateway/) for single-instance deployments.
4. SMS dispatch and delivered message retrieval for automated test probes.
5. Synthetic database initialization idempotency.
"""

import os
import sys
import json
import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))


def test_healthcheck_endpoint_healthy(client):
    """
    Test 1: /health and /healthz must return HTTP 200 with JSON status 'healthy'
    when database is connected.
    """
    for endpoint in ["/health", "/healthz"]:
        resp = client.get(endpoint)
        assert resp.status_code == 200, f"{endpoint} must return 200 OK"
        assert resp.mimetype == "application/json"
        data = resp.get_json()
        assert data.get("status") == "healthy"
        assert data.get("database") == "connected"
        assert "timestamp" in data


def test_healthcheck_endpoint_degraded_when_db_down(client, monkeypatch):
    """
    Test 2: When the database connection fails, /health must return HTTP 503
    with status 'degraded' and database 'unreachable', without crashing.
    """
    import app as app_module

    def mock_broken_db():
        raise ConnectionError("Simulated database failure")

    monkeypatch.setattr(app_module, "get_db", mock_broken_db)

    resp = client.get("/health")
    assert resp.status_code == 503, "Healthcheck must return 503 Service Unavailable when DB is down"
    data = resp.get_json()
    assert data.get("status") == "degraded"
    assert data.get("database") == "unreachable"


def test_embedded_sms_gateway_console(client):
    """
    Test 3: /__gateway/ must serve the PurvSMS simulation console HTML
    even without an external smsgw service container.
    """
    resp = client.get("/__gateway/")
    assert resp.status_code == 200
    assert b"PurvSMS Gateway Console" in resp.data


def test_embedded_sms_gateway_message_dispatch_and_inbox(client):
    """
    Test 4: Sending an SMS must queue the message in the embedded store,
    and /__gateway/api/messages must allow automated probes to retrieve it.
    """
    import app as app_module

    test_mobile = "9988776655"
    test_otp = "852963"
    test_text = f"Your Sewa Setu OTP is {test_otp}."

    # Send message via gateway API
    post_resp = client.post("/__gateway/api/send", json={"to": test_mobile, "text": test_text})
    assert post_resp.status_code == 200

    # Query the inbox via the gateway API
    resp = client.get(f"/__gateway/api/messages?to={test_mobile}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "messages" in data
    assert len(data["messages"]) >= 1
    latest = data["messages"][-1]
    assert latest["to"] == test_mobile
    assert test_otp in latest["text"]
    assert latest["delivered"] is True


def test_public_routes_do_not_expose_stack_traces(client):
    """
    Test 5: Accessing invalid routes or malformed requests must return clean error
    responses without leaking Python stack traces or internal environment variables.
    """
    resp = client.get("/nonexistent-page-xyz-12345")
    assert resp.status_code == 404
    assert b"Traceback" not in resp.data
    assert b"SECRET_KEY" not in resp.data
    assert b"ADMIN_PASSWORD" not in resp.data


def test_healthcheck_reveals_no_otps_or_secrets(client):
    """
    Test 6: Verify that ordinary health endpoints (/health, /healthz)
    reveal no OTP codes, passwords, or secrets.
    """
    for endpoint in ["/health", "/healthz"]:
        resp = client.get(endpoint)
        assert resp.status_code == 200
        raw_text = resp.data.decode("utf-8").lower()
        for forbidden in ["otp", "code", "password", "secret", "token"]:
            assert forbidden not in raw_text, f"{endpoint} leaked '{forbidden}': {raw_text}"


def test_production_gateway_blocks_unauthenticated_public_visitors(client, monkeypatch):
    """
    Test 7: In production (APP_ENV=production), unauthenticated public visitors
    must NOT be able to view the SMS console or harvest citizen OTPs via the API.
    Must return HTTP 403 Forbidden.
    """
    import app as app_module
    monkeypatch.setattr(app_module, "APP_ENV", "production")

    # Public visitor attempting to view console
    resp_console = client.get("/__gateway/")
    assert resp_console.status_code == 403, "Public access to SMS console must be blocked in production"

    # Public visitor attempting to read messages API
    resp_api = client.get("/__gateway/api/messages")
    assert resp_api.status_code == 403, "Public access to SMS messages API must be blocked in production"


def test_production_gateway_blocks_admin_session_and_admin_password(client, monkeypatch):
    """
    Test 8: In production, administrative sessions and ADMIN_PASSWORD credentials
    must NOT be allowed to access citizen OTPs or the telecom SMS gateway.
    Separation of duties requires that admin credentials cannot harvest OTPs.
    """
    import app as app_module
    monkeypatch.setattr(app_module, "APP_ENV", "production")
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "super-secret-admin-pass-999")
    monkeypatch.delenv("GATEWAY_AUTH_TOKEN", raising=False)

    # 1. Admin login session attempting to access console or messages API
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True

    resp_console = client.get("/__gateway/")
    assert resp_console.status_code == 403, "Admin session must NOT bypass gateway access control in production"

    resp_api = client.get("/__gateway/api/messages")
    assert resp_api.status_code == 403, "Admin session must NOT read citizen OTPs in production"

    # 2. Presenting ADMIN_PASSWORD in X-Gateway-Auth or Bearer header must be rejected
    resp_admin_hdr = client.get(
        "/__gateway/api/messages",
        headers={"X-Gateway-Auth": "super-secret-admin-pass-999"}
    )
    assert resp_admin_hdr.status_code == 403, "ADMIN_PASSWORD must NOT be accepted as gateway auth token"

    resp_admin_bearer = client.get(
        "/__gateway/api/messages",
        headers={"Authorization": "Bearer super-secret-admin-pass-999"}
    )
    assert resp_admin_bearer.status_code == 403, "ADMIN_PASSWORD must NOT be accepted via Bearer header"


def test_production_gateway_blocks_missing_or_invalid_tokens(client, monkeypatch):
    """
    Test 9: In production, requests without a token or with an invalid token
    must be rejected with HTTP 403 Forbidden.
    """
    import app as app_module
    monkeypatch.setattr(app_module, "APP_ENV", "production")
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "strong-dedicated-gateway-secret-54321")

    # Missing token
    resp_none = client.get("/__gateway/api/messages")
    assert resp_none.status_code == 403

    # Invalid header token
    resp_bad = client.get(
        "/__gateway/api/messages",
        headers={"X-Gateway-Auth": "invalid-token-123"}
    )
    assert resp_bad.status_code == 403

    # Invalid query token
    resp_bad_q = client.get("/__gateway/?token=wrong-token")
    assert resp_bad_q.status_code == 403


def test_production_gateway_permits_only_dedicated_gateway_auth_token(client, monkeypatch):
    """
    Test 10: In production, access is granted ONLY when the dedicated strong
    GATEWAY_AUTH_TOKEN environment secret is presented via X-Gateway-Auth,
    Authorization Bearer header, or query parameter.
    """
    import app as app_module
    monkeypatch.setattr(app_module, "APP_ENV", "production")
    monkeypatch.setenv("GATEWAY_AUTH_TOKEN", "strong-dedicated-gateway-secret-54321")

    # Valid X-Gateway-Auth header
    resp_hdr = client.get(
        "/__gateway/api/messages",
        headers={"X-Gateway-Auth": "strong-dedicated-gateway-secret-54321"}
    )
    assert resp_hdr.status_code == 200
    assert "messages" in resp_hdr.get_json()

    # Valid Authorization Bearer header
    resp_bearer = client.get(
        "/__gateway/api/messages",
        headers={"Authorization": "Bearer strong-dedicated-gateway-secret-54321"}
    )
    assert resp_bearer.status_code == 200

    # Valid query param for console
    resp_console = client.get("/__gateway/?token=strong-dedicated-gateway-secret-54321")
    assert resp_console.status_code == 200
    assert b"PurvSMS Gateway Console" in resp_console.data


