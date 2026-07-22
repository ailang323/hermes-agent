from datetime import datetime, timezone

from fastapi.testclient import TestClient

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli import web_server


def test_codex_usage_requires_dashboard_auth(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)

    client = TestClient(web_server.app)
    response = client.get("/api/codex/usage")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_codex_usage_serializes_account_snapshot(monkeypatch):
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        account_email="codex@example.test",
        plan="Plus",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=13.0,
                reset_at=datetime(2026, 7, 22, 5, tzinfo=timezone.utc),
            ),
        ),
    )
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", lambda provider: snapshot)

    client = TestClient(web_server.app)
    response = client.get(
        "/api/codex/usage",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "provider": "openai-codex",
        "source": "usage_api",
        "fetched_at": "2026-07-22T00:00:00+00:00",
        "account_email": "codex@example.test",
        "title": "Account limits",
        "plan": "Plus",
        "windows": [
            {
                "label": "Session",
                "used_percent": 13.0,
                "remaining_percent": 87.0,
                "reset_at": "2026-07-22T05:00:00+00:00",
                "detail": None,
            }
        ],
        "details": [],
        "error": None,
    }
