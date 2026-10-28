"""Run with: pytest -v test.py
Portal calls are mocked; tests do not need a real session token or network access.
"""
import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(main, "_transformer_cache", None)
    monkeypatch.setattr(main, "_transformer_cache_lock", asyncio.Lock())


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def sample_meters():
    return [
        {"meterId": "M-001", "serialNo": "SE10001", "make": "HPL",
         "phaseType": "single", "installStatus": "Installed", "installType": "Whole Current",
         "dtCode": "DT-001",
         "hierarchy": {"zone": {"name": "Zone A", "code": "Z-01"},
                       "circle": {"name": "Circle A", "code": "C-01"},
                       "division": {"name": "Division A", "code": "D-01"},
                       "subdivision": {"name": "Subdivision A", "code": "SD-01"},
                       "substation": {"name": "Substation A", "code": "SS-01"},
                       "feeder": {"name": "Feeder A", "code": "F-001"},
                       "dt": {"name": "Transformer A", "code": "DT-001"}}},
        {"meterId": "M-002", "serialNo": "SE10002", "make": "Genus",
         "phaseType": "three", "installStatus": "Faulty", "installType": "CT Operated",
         "dtCode": "DT-002",
         "hierarchy": {"zone": {"name": "Zone B", "code": "Z-02"},
                       "circle": {"name": "Circle B", "code": "C-02"},
                       "division": {"name": "Division B", "code": "D-02"},
                       "subdivision": {"name": "Subdivision B", "code": "SD-02"},
                       "substation": {"name": "Substation B", "code": "SS-02"},
                       "feeder": {"name": "Feeder B", "code": "F-002"},
                       "dt": {"name": "Transformer B", "code": "DT-002"}}},
        {"meterId": "M-003", "serialNo": "SE10003", "make": "HPL",
         "phaseType": "single", "installStatus": "Decommissioned", "installType": "Whole Current",
         "dtCode": "DT-001",
         "hierarchy": {"zone": {"name": "Zone A", "code": "Z-01"},
                       "circle": {"name": "Circle A", "code": "C-01"},
                       "division": {"name": "Division A", "code": "D-01"},
                       "subdivision": {"name": "Subdivision A", "code": "SD-01"},
                       "substation": {"name": "Substation A", "code": "SS-01"},
                       "feeder": {"name": "Feeder A", "code": "F-001"},
                       "dt": {"name": "Transformer A", "code": "DT-001"}}},
    ]


@pytest.fixture
def replace_meters(sample_meters):
    original = main.METER_DATA
    main.build_meter_indexes(sample_meters)
    yield
    main.build_meter_indexes(original)


@pytest.fixture
def transformers():
    return [
        {"code": "DT-001", "name": "Transformer A", "feederCode": "F-001", "capacityKva": 100},
        {"code": "DT-002", "name": "Transformer B", "feederCode": "F-002", "capacityKva": 63},
        {"code": "DT-003", "name": "Transformer C", "feederCode": "F-003", "capacityKva": 250},
    ]


def paginated_mock(records, calls=None):
    async def fake(path):
        if calls is not None:
            calls.append(path)
        page = int(path.rsplit("=", 1)[1])
        page_size = 2
        start = (page - 1) * page_size
        return {"data": records[start:start + page_size], "total": len(records),
                "page": page, "pageSize": page_size}
    return fake


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_list_meters_pagination(client):
    response = client.get("/meters", params={"page": 1, "page_size": 5})
    assert response.status_code == 200
    assert len(response.json()["data"]) <= 5
    assert response.json()["total"] == len(main.METER_DATA)


def test_meter_search(client):
    if not main.METER_DATA or not main.METER_DATA[0].get("serialNo"):
        pytest.skip("No serial number available in meter.json")
    serial = str(main.METER_DATA[0]["serialNo"])
    response = client.get("/meters/search", params={"q": serial[:4].lower()})
    assert response.status_code == 200
    assert response.json()["total"] >= 1


def test_make_filter(client, replace_meters):
    response = client.get("/meters", params={"make": "hpl"})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert all(m["make"].casefold() == "hpl" for m in response.json()["data"])


def test_install_status_filter(client, replace_meters):
    response = client.get("/meters", params={"install_status": "faulty"})
    assert response.status_code == 200
    assert [m["meterId"] for m in response.json()["data"]] == ["M-002"]


def test_combined_filters(client, replace_meters):
    response = client.get("/meters", params={
        "make": "HPL", "install_status": "Installed", "feeder_code": "F-001"
    })
    assert response.status_code == 200
    assert [m["meterId"] for m in response.json()["data"]] == ["M-001"]


def test_transformer_and_hierarchy_filters(client, replace_meters):
    response = client.get("/meters", params={"transformer_code": "dt-001", "zone": "Zone A"})
    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_meter_lookup_404_and_success(client, replace_meters):
    assert client.get("/meters/M-001").status_code == 200
    assert client.get("/meters/unknown").status_code == 404


def test_energy_geo_and_unknown_meter(client, monkeypatch, replace_meters):
    async def fake_portal_get(path):
        if path.endswith("/energy"):
            return {"data": [{"timestamp": "2026-06-30T23:30:00", "kwh": 12.3}]}
        if path.endswith("/geo"):
            return {"data": {"latitude": "26.8", "longitude": "75.9"}}
        raise AssertionError(path)
    monkeypatch.setattr(main, "portal_get", fake_portal_get)
    assert client.get("/meters/M-001/energy").json()["data"][0]["kwh"] == 12.3
    assert client.get("/meters/M-001/geo").json()["data"]["latitude"] == "26.8"
    assert client.get("/meters/UNKNOWN/energy").status_code == 404
    assert client.get("/meters/UNKNOWN/geo").status_code == 404


def test_network_hierarchy(client, replace_meters):
    response = client.get("/network/hierarchy")
    assert response.status_code == 200
    assert any(item["code"] == "F-001" for item in response.json()["feeder"])


def test_transformers_fetch_all_pages(client, monkeypatch, transformers):
    calls = []
    monkeypatch.setattr(main, "portal_get", paginated_mock(transformers, calls))
    response = client.get("/transformers")
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert len(response.json()["data"]) == 3
    assert len(calls) == 2


def test_transformer_cache_hit(client, monkeypatch, transformers):
    calls = []
    monkeypatch.setattr(main, "portal_get", paginated_mock(transformers, calls))
    assert client.get("/transformers").status_code == 200
    assert client.get("/transformers").status_code == 200
    assert len(calls) == 2  # two pages fetched once; second request uses cache


def test_expired_cache_refreshes(client, monkeypatch, transformers):
    monkeypatch.setattr(main, "portal_get", paginated_mock(transformers))
    assert client.get("/transformers").status_code == 200
    main._transformer_cache["expires_at_monotonic"] = time.monotonic() - 1
    updated = [{"code": "DT-NEW", "name": "New", "feederCode": "F-NEW", "capacityKva": 500}]

    async def fake_fetch():
        return updated
    monkeypatch.setattr(main, "fetch_all_transformers", fake_fetch)
    response = client.get("/transformers")
    assert response.status_code == 200
    assert response.json()["data"] == updated
    assert response.json()["stale"] is False


def test_expired_cache_serves_stale_if_refresh_fails(client, monkeypatch, transformers):
    monkeypatch.setattr(main, "portal_get", paginated_mock(transformers))
    first = client.get("/transformers")
    assert first.status_code == 200
    old_data = first.json()["data"]
    fetched_at = first.json()["fetchedAt"]
    main._transformer_cache["expires_at_monotonic"] = time.monotonic() - 1

    async def fail_fetch():
        raise HTTPException(status_code=401, detail="Portal session expired.")
    monkeypatch.setattr(main, "fetch_all_transformers", fail_fetch)

    response = client.get("/transformers")
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["data"] == old_data
    assert response.json()["fetchedAt"] == fetched_at
    assert "refreshError" in response.json()


def test_no_cache_and_refresh_failure_returns_error(client, monkeypatch):
    async def fail_fetch():
        raise HTTPException(status_code=401, detail="Portal session expired.")
    monkeypatch.setattr(main, "fetch_all_transformers", fail_fetch)
    assert client.get("/transformers").status_code == 401


def test_transformer_meter_relationship(client, monkeypatch, transformers, replace_meters):
    monkeypatch.setattr(main, "portal_get", paginated_mock(transformers))
    response = client.get("/transformers/DT-001/meters")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {m["meterId"] for m in response.json()["data"]} == {"M-001", "M-003"}
    assert client.get("/transformers/UNKNOWN/meters").status_code == 404


def test_invalid_pagination_from_portal_returns_502(client, monkeypatch):
    async def invalid(_path):
        return {"data": [{"code": "DT-001"}], "total": 2, "page": 1, "pageSize": 20}
    monkeypatch.setattr(main, "portal_get", invalid)
    assert client.get("/transformers").status_code == 502


def test_unknown_route_returns_404(client):
    assert client.get("/not-a-real-route").status_code == 404
