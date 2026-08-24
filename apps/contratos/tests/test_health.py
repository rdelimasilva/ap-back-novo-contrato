import pytest
from django.test import Client


@pytest.mark.django_db
def test_health_returns_ok():
    response = Client().get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
