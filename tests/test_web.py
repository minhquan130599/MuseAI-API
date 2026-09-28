from fastapi.testclient import TestClient

from muse_ai.web.app import JobRecord, app, safe_filename


def test_safe_filename_blocks_parent_components():
    assert safe_filename("../../secret.mp4") == "secret.mp4"
    assert safe_filename(r"..\..\secret.mp4") == "secret.mp4"


def test_job_public_contains_download_urls():
    job = JobRecord(
        id="abc123",
        prompt="Create a video",
        downloads=["one.mp4", "two.mp4"],
    )
    data = job.public()
    assert data["download_urls"] == [
        "/api/generations/abc123/files/one.mp4",
        "/api/generations/abc123/files/two.mp4",
    ]


def test_web_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["service"] == "MuseAI-API Web"


def test_web_index_is_served():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "MuseAI Studio" in response.text
