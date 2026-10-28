"""
Flock Energy API: FastAPI adapter for the Urja Meter Ops portal.

Portal credentials are read from environment variables. Do not commit .env.
"""

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
    version="1.2.0",
)

BASE_URL = "https://urja-ops.flockenergy.tech"
LOGIN_PATH = "/login"
SESSION_COOKIE_NAME = "__Secure-better-auth.session_token"
PORTAL_EMAIL = os.getenv("PORTAL_EMAIL")
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD")
METER_FILE = Path(__file__).parent / "meter.json"
TRANSFORMER_CACHE_TTL = 30 * 60
REQUEST_TIMEOUT = 20.0

# The token is held in memory only; the password is never logged or returned.
_session_cookie: str | None = os.getenv("PORTAL_SESSION_COOKIE") or None
_auth_lock = asyncio.Lock()
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
    """Normalize values for case-insensitive matching."""
    return str(value).strip().casefold() if value is not None else ""


def get_meter_transformer_code(meter: dict[str, Any]) -> str | None:
    hierarchy = meter.get("hierarchy") or {}
    dt = hierarchy.get("dt") or {}
    return meter.get("dtCode") or dt.get("code")


def hierarchy_node(meter: dict[str, Any], level: str) -> dict[str, Any]:
    value = (meter.get("hierarchy") or {}).get(level) or {}
    return value if isinstance(value, dict) else {}


def _index_add(
    index: dict[str, list[dict[str, Any]]],
    key: Any,
    meter: dict[str, Any],
) -> None:
    normalized_key = norm(key)
    if normalized_key:
        index.setdefault(normalized_key, []).append(meter)


def build_meter_indexes(records: list[dict[str, Any]]) -> None:
    """Replace the local meter snapshot and rebuild all lookup indexes."""
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
    with METER_FILE.open("r", encoding="utf-8") as file:
        loaded_meters = json.load(file)
    if not isinstance(loaded_meters, list):
        raise ValueError("meter.json must contain a JSON array.")
    if not all(isinstance(item, dict) for item in loaded_meters):
        raise ValueError("Every item in meter.json must be a JSON object.")
    build_meter_indexes(loaded_meters)
except FileNotFoundError as exc:
    raise RuntimeError(f"Required file not found: {METER_FILE}") from exc
except json.JSONDecodeError as exc:
    raise RuntimeError(f"Invalid JSON in meter.json: {exc}") from exc


def _cookie_from_client(client: httpx.AsyncClient) -> str | None:
    value = client.cookies.get(SESSION_COOKIE_NAME)
    return value.strip() if value and value.strip() else None


async def login_portal() -> str:
    """
    Log in to the portal, retain its session cookie, and verify the session.

    The portal's login response has been observed to redirect to /meters.
    This implementation validates the resulting session with an authenticated
    GET rather than trusting the redirect response alone.
    """
    global _session_cookie

    if not PORTAL_EMAIL or not PORTAL_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail=(
                "Portal credentials are not configured. Set PORTAL_EMAIL and "
                "PORTAL_PASSWORD in .env."
            ),
        )

    async with _auth_lock:
        # Another request may have completed login while this request waited.
        if _session_cookie:
            return _session_cookie

        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            ) as client:
                # The observed login form submits email and password.
                login_response = await client.post(
                    LOGIN_PATH,
                    data={"email": PORTAL_EMAIL, "password": PORTAL_PASSWORD},
                    headers={"Accept": "application/json"},
                )

                # Some portal/framework combinations return a JSON redirect with
                # HTTP 200; others return an HTTP 3xx. Both need session validation.
                if login_response.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Portal login failed with HTTP {login_response.status_code}.",
                    )

                cookie = _cookie_from_client(client)
                if not cookie:
                    # A cookie may be represented on the response even if the
                    # client jar did not retain it; never accept an empty value.
                    candidate = login_response.cookies.get(SESSION_COOKIE_NAME)
                    cookie = candidate.strip() if candidate and candidate.strip() else None

                if not cookie:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Portal login did not provide a non-empty session cookie. "
                            "Check the login request format and response headers."
                        ),
                    )

                # Verify that the newly obtained cookie really authenticates.
                client.cookies.set(SESSION_COOKIE_NAME, cookie)
                verification = await client.get(
                    "/meters",
                    headers={"Accept": "text/html,application/json"},
                )
                if verification.status_code in (401, 403) or verification.url.path == LOGIN_PATH:
                    raise HTTPException(
                        status_code=502,
                        detail="Portal login returned a cookie, but the session was not accepted.",
                    )
                if verification.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Portal session verification failed with HTTP "
                            f"{verification.status_code}."
                        ),
                    )

                _session_cookie = cookie
                return cookie

        except HTTPException:
            raise
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Portal login timed out.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Unable to log in to the portal.") from exc


async def _portal_get_once(path: str, cookie: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
        return await client.get(
            f"{BASE_URL}{path}",
            cookies={SESSION_COOKIE_NAME: cookie},
            headers={"Accept": "application/json"},
        )


async def portal_get(path: str) -> dict[str, Any]:
    """Make an authenticated portal GET; re-login and retry once on 401/403."""
    global _session_cookie

    cookie = _session_cookie
    if not cookie:
        cookie = await login_portal()

    try:
        response = await _portal_get_once(path, cookie)

        if response.status_code in (401, 403):
            # Clear only the session we just tried. If another request has
            # already replaced it, reuse the newer cookie instead.
            async with _auth_lock:
                if _session_cookie == cookie:
                    _session_cookie = None
                replacement = _session_cookie

            cookie = replacement or await login_portal()
            response = await _portal_get_once(path, cookie)

        if response.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="Portal session is invalid after one re-login attempt.",
            )

        if response.is_redirect:
            raise HTTPException(
                status_code=502,
                detail="Portal redirected an API request; the session may not be authenticated.",
            )

        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Portal returned invalid JSON.") from exc

        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail="Portal returned an unexpected response format.",
            )
        return result

    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Request to the portal timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Portal returned HTTP {exc.response.status_code}.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Unable to retrieve data from the portal.") from exc


async def fetch_all_transformers() -> list[dict[str, Any]]:
    """Fetch all transformer pages and reject incomplete or inconsistent results."""
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
            raise HTTPException(
                status_code=502,
                detail="Transformer pagination changed during retrieval.",
            )
        records.extend(page_data)

    if len(records) != total:
        raise HTTPException(
            status_code=502,
            detail=f"Incomplete transformer dataset: expected {total}, received {len(records)}.",
        )
    return records


def cache_is_fresh(cache: dict[str, Any]) -> bool:
    return time.monotonic() < cache["expires_at_monotonic"]


def cache_response(
    cache: dict[str, Any],
    stale: bool,
    refresh_error: str | None = None,
) -> dict[str, Any]:
    result = {
        "data": cache["data"],
        "total": len(cache["data"]),
        "cachedForSeconds": TRANSFORMER_CACHE_TTL,
        "fetchedAt": cache["fetched_at"],
        "stale": stale,
    }
    if stale:
        result["refreshError"] = refresh_error or "Fresh data could not be retrieved."
    return result


async def get_transformer_cache() -> dict[str, Any]:
    """Refresh expired transformer data, retaining the last good copy on failure."""
    global _transformer_cache

    if _transformer_cache is not None and cache_is_fresh(_transformer_cache):
        return cache_response(_transformer_cache, stale=False)

    async with _transformer_cache_lock:
        if _transformer_cache is not None and cache_is_fresh(_transformer_cache):
            return cache_response(_transformer_cache, stale=False)

        previous = _transformer_cache
        try:
            fresh_data = await fetch_all_transformers()
        except HTTPException as exc:
            if previous is None:
                raise
            return cache_response(previous, stale=True, refresh_error=str(exc.detail))
        except httpx.HTTPError as exc:
            if previous is None:
                raise HTTPException(status_code=502, detail="Portal request failed.") from exc
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
        allowed_ids = {id(meter) for meter in candidates[0]}
        for candidate_list in candidates[1:]:
            allowed_ids.intersection_update(id(meter) for meter in candidate_list)
        results = [meter for meter in candidates[0] if id(meter) in allowed_ids]
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
        if any(
            value is not None and norm(meter.get(field)) != norm(value)
            for value, field in exact_fields
        ):
            continue

        mismatch = False
        for value, level in hierarchy_fields:
            if value is not None:
                node = hierarchy_node(meter, level)
                if norm(node.get("code")) != norm(value) and norm(node.get("name")) != norm(value):
                    mismatch = True
                    break
        if not mismatch:
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
