import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

load_dotenv()

app = FastAPI(
    title="Flock Energy API",
    description="API over the Urja Meter Ops portal.",
    version="1.1.0",
)

BASE_URL = "https://urja-ops.flockenergy.tech"
SESSION_COOKIE_NAME = "__Secure-better-auth.session_token"
SESSION_COOKIE = os.getenv("PORTAL_SESSION_COOKIE")
METER_FILE = Path(__file__).parent / "meter.json"
TRANSFORMER_CACHE_TTL = 30 * 60

# The cache is retained after expiry for stale-if-refresh-fails behavior.
_transformer_cache: dict[str, Any] | None = None
_transformer_cache_lock = asyncio.Lock()

METER_DATA: list[dict[str, Any]] = []
METERS_BY_ID: dict[str, dict[str, Any]] = {}
METERS_BY_SERIAL: dict[str, list[dict[str, Any]]] = {}
METERS_BY_TRANSFORMER: dict[str, list[dict[str, Any]]] = {}
METERS_BY_FEEDER: dict[str, list[dict[str, Any]]] = {}
METERS_BY_MAKE: dict[str, list[dict[str, Any]]] = {}
METERS_BY_INSTALL_STATUS: dict[str, list[dict[str, Any]]] = {}


def norm(value: Any) -> str:
    return str(value).strip().casefold() if value is not None else ""


def get_meter_transformer_code(meter: dict[str, Any]) -> str | None:
    hierarchy = meter.get("hierarchy") or {}
    dt = hierarchy.get("dt") or {}
    return meter.get("dtCode") or dt.get("code")


def hierarchy_node(meter: dict[str, Any], level: str) -> dict[str, Any]:
    value = (meter.get("hierarchy") or {}).get(level) or {}
    return value if isinstance(value, dict) else {}


def _index_add(index: dict[str, list[dict[str, Any]]], key: Any,
               meter: dict[str, Any]) -> None:
    key = norm(key)
    if key:
        index.setdefault(key, []).append(meter)


def build_meter_indexes(records: list[dict[str, Any]]) -> None:
    """Rebuild local lookup indexes; useful at startup and in tests."""
    global METER_DATA, METERS_BY_ID, METERS_BY_SERIAL
    global METERS_BY_TRANSFORMER, METERS_BY_FEEDER
    global METERS_BY_MAKE, METERS_BY_INSTALL_STATUS

    METER_DATA = records
    METERS_BY_ID = {}
    METERS_BY_SERIAL = {}
    METERS_BY_TRANSFORMER = {}
    METERS_BY_FEEDER = {}
    METERS_BY_MAKE = {}
    METERS_BY_INSTALL_STATUS = {}

    for meter in records:
        meter_id = norm(meter.get("meterId"))
        if meter_id:
            METERS_BY_ID[meter_id] = meter
        _index_add(METERS_BY_SERIAL, meter.get("serialNo"), meter)
        _index_add(METERS_BY_TRANSFORMER, get_meter_transformer_code(meter), meter)
        _index_add(METERS_BY_FEEDER, hierarchy_node(meter, "feeder").get("code"), meter)
        _index_add(METERS_BY_MAKE, meter.get("make"), meter)
        _index_add(METERS_BY_INSTALL_STATUS, meter.get("installStatus"), meter)


try:
    with METER_FILE.open("r", encoding="utf-8") as f:
        loaded_meters = json.load(f)
    if not isinstance(loaded_meters, list):
        raise ValueError("meter.json must contain a JSON array.")
    build_meter_indexes(loaded_meters)
except FileNotFoundError:
    raise RuntimeError(f"Required file not found: {METER_FILE}")
except json.JSONDecodeError as exc:
    raise RuntimeError(f"Invalid JSON in meter.json: {exc}")


async def portal_get(path: str) -> dict[str, Any]:
    """Make an authenticated portal request and normalize upstream errors."""
    if not SESSION_COOKIE:
        raise HTTPException(
            status_code=500,
            detail="PORTAL_SESSION_COOKIE is not configured in .env.",
        )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{BASE_URL}{path}",
                cookies={SESSION_COOKIE_NAME: SESSION_COOKIE},
                headers={"Accept": "application/json"},
            )
        if response.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="Portal session expired or is invalid. Log in again and update PORTAL_SESSION_COOKIE.",
            )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Portal returned invalid JSON.")
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="Portal returned an unexpected response format.")
        return result
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to the portal timed out.")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Unable to retrieve data from the portal.")


async def fetch_all_transformers() -> list[dict[str, Any]]:
    """Fetch every transformer page and reject incomplete results."""
    first = await portal_get("/portal/dts?page=1")
    data, total, page_size = first.get("data"), first.get("total"), first.get("pageSize")

    if (
        not isinstance(data, list)
        or not isinstance(total, int) or isinstance(total, bool) or total < 0
        or not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0
    ):
        raise HTTPException(status_code=502, detail="Invalid transformer pagination data.")

    records = list(data)
    if len(records) > total:
        raise HTTPException(status_code=502, detail="Portal returned more transformers than reported.")

    pages = (total + page_size - 1) // page_size
    for page in range(2, pages + 1):
        result = await portal_get(f"/portal/dts?page={page}")
        page_data = result.get("data")
        if not isinstance(page_data, list):
            raise HTTPException(status_code=502, detail=f"Invalid transformer data on page {page}.")
        if (
            result.get("total") != total
            or result.get("pageSize") != page_size
            or result.get("page") != page
        ):
            raise HTTPException(status_code=502, detail="Transformer pagination changed during retrieval.")
        records.extend(page_data)

    if len(records) != total:
        raise HTTPException(
            status_code=502,
            detail=f"Incomplete transformer dataset: expected {total}, received {len(records)}.",
        )
    return records


def cache_is_fresh(cache: dict[str, Any]) -> bool:
    return time.monotonic() < cache["expires_at_monotonic"]


def cache_response(cache: dict[str, Any], stale: bool,
                   refresh_error: str | None = None) -> dict[str, Any]:
    response = {
        "data": cache["data"],
        "total": len(cache["data"]),
        "cachedForSeconds": TRANSFORMER_CACHE_TTL,
        "fetchedAt": cache["fetched_at"],
        "stale": stale,
    }
    if stale:
        response["refreshError"] = refresh_error or "Fresh data could not be retrieved."
    return response


async def get_transformer_cache() -> dict[str, Any]:
    """Use fresh cache; on refresh failure, serve the last copy as stale."""
    global _transformer_cache

    if _transformer_cache is not None and cache_is_fresh(_transformer_cache):
        print("TRANSFORMER CACHE HIT")
        return cache_response(_transformer_cache, stale=False)

    async with _transformer_cache_lock:
        if _transformer_cache is not None and cache_is_fresh(_transformer_cache):
            print("TRANSFORMER CACHE HIT")
            return cache_response(_transformer_cache, stale=False)

        previous = _transformer_cache
        print("TRANSFORMER CACHE MISS — fetching from portal")
        try:
            fresh_data = await fetch_all_transformers()
        except HTTPException as exc:
            if previous is None:
                raise
            print(f"Transformer refresh failed; serving stale cache: {exc.detail}")
            return cache_response(previous, stale=True, refresh_error=str(exc.detail))
        except httpx.HTTPError:
            if previous is None:
                raise HTTPException(status_code=502, detail="Portal request failed.")
            print("Transformer refresh failed; serving stale cache.")
            return cache_response(previous, stale=True, refresh_error="Portal request failed.")

        _transformer_cache = {
            "data": fresh_data,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "expires_at_monotonic": time.monotonic() + TRANSFORMER_CACHE_TTL,
        }
        return cache_response(_transformer_cache, stale=False)


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "meter_records_loaded": len(METER_DATA)}


@app.get("/meters", tags=["Meters"])
async def list_meters(
    q: str | None = Query(None, description="Partial meter ID or serial number."),
    make: str | None = None,
    install_status: str | None = None,
    transformer_code: str | None = None,
    feeder_code: str | None = None,
    phase_type: str | None = None,
    install_type: str | None = None,
    zone: str | None = None,
    circle: str | None = None,
    division: str | None = None,
    subdivision: str | None = None,
    substation: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Filter local meter data and return paginated results."""
    candidates = []
    for value, index in (
        (make, METERS_BY_MAKE),
        (install_status, METERS_BY_INSTALL_STATUS),
        (transformer_code, METERS_BY_TRANSFORMER),
        (feeder_code, METERS_BY_FEEDER),
    ):
        if value is not None:
            candidates.append(index.get(norm(value), []))

    if candidates:
        candidates.sort(key=len)
        allowed_ids = {id(m) for m in candidates[0]}
        for candidate_list in candidates[1:]:
            allowed_ids.intersection_update(id(m) for m in candidate_list)
        results = [m for m in candidates[0] if id(m) in allowed_ids]
    else:
        results = METER_DATA

    q_norm = norm(q)
    exact_fields = ((phase_type, "phaseType"), (install_type, "installType"))
    hierarchy_fields = (
        (zone, "zone"), (circle, "circle"), (division, "division"),
        (subdivision, "subdivision"), (substation, "substation"),
    )
    filtered = []
    for meter in results:
        if q_norm and q_norm not in norm(meter.get("meterId")) and q_norm not in norm(meter.get("serialNo")):
            continue
        if any(value is not None and norm(meter.get(field)) != norm(value)
               for value, field in exact_fields):
            continue
        mismatch = False
        for value, level in hierarchy_fields:
            if value is not None:
                node = hierarchy_node(meter, level)
                if norm(node.get("code")) != norm(value) and norm(node.get("name")) != norm(value):
                    mismatch = True
                    break
        if mismatch:
            continue
        filtered.append(meter)

    total = len(filtered)
    start = (page - 1) * page_size
    return {
        "data": filtered[start:start + page_size],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


@app.get("/meters/search", tags=["Meters"])
async def search_meters(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return await list_meters(q=q, page=page, page_size=page_size)


@app.get("/meters/search-by-serial/{serial_no}", tags=["Meters"])
async def search_by_serial(serial_no: str):
    records = METERS_BY_SERIAL.get(norm(serial_no), [])
    return {"data": records, "total": len(records)}


@app.get("/meters/{meter_id}/energy", tags=["Energy"])
async def get_meter_energy(meter_id: str):
    if norm(meter_id) not in METERS_BY_ID:
        raise HTTPException(status_code=404, detail=f"Meter '{meter_id}' was not found.")
    return await portal_get(f"/portal/meters/{meter_id}/energy")


@app.get("/meters/{meter_id}/geo", tags=["Meters"])
async def get_meter_geo(meter_id: str):
    if norm(meter_id) not in METERS_BY_ID:
        raise HTTPException(status_code=404, detail=f"Meter '{meter_id}' was not found.")
    return await portal_get(f"/portal/meters/{meter_id}/geo")


@app.get("/meters/{meter_id}", tags=["Meters"])
async def get_meter(meter_id: str):
    meter = METERS_BY_ID.get(norm(meter_id))
    if meter is None:
        raise HTTPException(status_code=404, detail=f"Meter '{meter_id}' was not found.")
    return meter


@app.get("/transformers", tags=["Transformers"])
async def list_transformers():
    return await get_transformer_cache()


@app.get("/transformers/{code}/meters", tags=["Transformers"])
async def get_transformer_meters(code: str):
    cached = await get_transformer_cache()
    transformer = next(
        (item for item in cached["data"] if norm(item.get("code")) == norm(code)),
        None,
    )
    if transformer is None:
        raise HTTPException(status_code=404, detail=f"Transformer '{code}' was not found.")
    meters = METERS_BY_TRANSFORMER.get(norm(code), [])
    return {
        "transformer": transformer,
        "data": meters,
        "total": len(meters),
        "stale": cached["stale"],
        "fetchedAt": cached["fetchedAt"],
    }


@app.get("/network/hierarchy", tags=["Network"])
async def get_network_hierarchy():
    levels = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
    result: dict[str, dict[str, dict[str, Any]]] = {level: {} for level in levels}
    for meter in METER_DATA:
        for level in levels:
            node = hierarchy_node(meter, level)
            name, code = node.get("name"), node.get("code")
            if name or code:
                result[level].setdefault(str(code or name), {"name": name, "code": code})
    return {level: list(nodes.values()) for level, nodes in result.items()}
