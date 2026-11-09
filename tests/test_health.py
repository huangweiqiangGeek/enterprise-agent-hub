from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.api.app import create_app


def test_liveness_has_server_generated_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-Id": "untrusted"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    request_id = response.json()["requestId"]
    assert str(UUID(request_id)) == request_id
    assert response.headers["X-Request-Id"] == request_id


def test_readiness_reports_unavailable_without_leaking_database_error() -> None:
    failure = OperationalError("private-host", {}, Exception("secret-password"))
    with patch("backend.api.app.database_ready", new=AsyncMock(side_effect=failure)):
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_NOT_READY"
    assert "secret-password" not in response.text
    assert "private-host" not in response.text


def test_readiness_requires_completed_initialization() -> None:
    for ready, expected in [(False, 503), (True, 200)]:
        with patch("backend.api.app.database_ready", new=AsyncMock(return_value=ready)):
            with TestClient(create_app()) as client:
                response = client.get("/api/v1/health/ready")
        assert response.status_code == expected
