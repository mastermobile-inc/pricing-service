import pytest
from fastapi.testclient import TestClient

from app.api.order_assembly_readonly import TestQueueSnapshot, create_app, validate_runtime
from app.core.config import get_settings
from app.services.order_assembly_queue import (
    AssemblyQueueLimitError,
    ReadOnlyAssemblyClient,
    _fetch_executing_deals,
)


class Recorder:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"result": []}


def test_ui_snapshot_coalesces_without_changing_xml_and_expires():
    from fastapi import Response

    clock = [0.0]
    cache = TestQueueSnapshot(lambda: clock[0])
    calls = []

    def refresh():
        calls.append(1)
        return Response(b'<assembly_queue generated_at="original"/>', media_type="application/xml")

    first = cache.get(2000, refresh)
    clock[0] = 14.9
    assert cache.get(2000, refresh).body == first.body
    assert len(calls) == 1
    clock[0] = 15.0
    cache.get(2000, refresh)
    assert len(calls) == 2
    cache.get(500, refresh)
    assert len(calls) == 3


def test_ui_snapshot_never_falls_back_after_error_or_slow_refresh():
    from fastapi import Response

    clock = [0.0]
    cache = TestQueueSnapshot(lambda: clock[0])
    cache.get(2000, lambda: Response(b"fresh"))
    clock[0] = 15.0
    assert cache.get(2000, lambda: Response(b"unavailable", status_code=503)).status_code == 503
    assert cache.cached is None

    def slow():
        clock[0] += 16
        return Response(b"slow")

    cache.get(2000, slow)
    assert cache.cached is None


def test_concurrent_ui_reads_share_one_complete_refresh():
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep

    from fastapi import Response

    cache = TestQueueSnapshot(lambda: 0.0)
    calls = []

    def refresh():
        calls.append(1)
        sleep(0.02)
        return Response(b"complete")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.get(2000, refresh).body, range(8)))
    assert calls == [1]
    assert results == [b"complete"] * 8


def test_complete_large_queue_requires_explicit_test_ceiling():
    class Pages:
        def call(self, method, params):
            start = int(params["start"])
            end = min(start + 50, 1272)
            page = {"result": [{"ID": str(i)} for i in range(start, end)]}
            if end < 1272:
                page["next"] = end
            return page

    with pytest.raises(AssemblyQueueLimitError):
        _fetch_executing_deals(Pages(), limit=500)
    with pytest.raises(ValueError):
        _fetch_executing_deals(Pages(), limit=2000)
    rows = _fetch_executing_deals(Pages(), limit=2000, maximum_limit=2000)
    assert len(rows) == 1272
    assert len({row["ID"] for row in rows}) == 1272


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
