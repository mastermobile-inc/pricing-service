import pytest
from fastapi.testclient import TestClient

from app.api.order_assembly_readonly import create_app, validate_runtime
from app.core.config import get_settings
from app.services.order_assembly_queue import ReadOnlyAssemblyClient


class Recorder:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"result": []}


@pytest.mark.parametrize("method", ["crm.deal.update", "crm.deal.add", "batch", "im.message.add"])
def test_disallows_crm_writes(method):
    delegate = Recorder()
    with pytest.raises(ValueError, match="read_only"):
        ReadOnlyAssemblyClient(delegate).call(method, {})
    assert delegate.calls == []


def test_only_executing_deals_are_read():
    delegate = Recorder()
    client = ReadOnlyAssemblyClient(delegate)
    with pytest.raises(ValueError, match="filter"):
        client.call("crm.deal.list", {"filter": {}})
    params = {"filter": {"=STAGE_ID": "EXECUTING"}}
    assert client.call("crm.deal.list", params) == {"result": []}
    assert len(delegate.calls) == 1


@pytest.mark.parametrize(
    "environment,url",
    [
        ("production", "sqlite:////opt/MM/.local/task46-test-assembly/queue.sqlite"),
        ("test", "postgresql://localhost/production"),
        ("test", "sqlite:////tmp/unapproved.sqlite"),
        ("test", None),
    ],
)
def test_rejects_nonisolated_runtime(environment, url):
    with pytest.raises(ValueError):
        validate_runtime(environment, url)


def test_only_authenticated_queue_route_is_mounted(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////opt/MM/.local/task46-test-assembly/queue.sqlite")
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "test-secret")
    monkeypatch.setenv("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL", "https://test.example/rest/1/test")
    monkeypatch.setenv("MM_TASK46_CRM_SOURCE", "test")
    get_settings.cache_clear()
    try:
        app = create_app()
        assert [route.path for route in app.routes] == ["/api/order-fulfillment/assembly-queue"]
        with TestClient(app) as client:
            assert client.get("/api/order-fulfillment/assembly-queue").status_code == 401
            assert client.post("/api/order-fulfillment/assembly-events").status_code == 404
            assert client.post("/api/order-fulfillment/assembly-queue").status_code == 405
    finally:
        get_settings.cache_clear()


def test_no_implicit_production_crm_from_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////opt/MM/.local/task46-test-assembly/queue.sqlite")
    monkeypatch.setenv(
        "ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL", "https://crm.master-mobile.ru/rest/1/test"
    )
    monkeypatch.delenv("MM_TASK46_CRM_SOURCE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="approval_required"):
            create_app()
        monkeypatch.setenv("MM_TASK46_CRM_SOURCE", "test")
        with pytest.raises(ValueError, match="production_crm_is_not_test"):
            create_app()
    finally:
        get_settings.cache_clear()
