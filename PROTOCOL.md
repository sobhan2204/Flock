# PROTOCOL.md — Urja Meter Ops Portal Investigation

This document records the portal behavior observed while building the Flock Energy API adapter. It distinguishes **observed facts** from **implementation decisions** and **items not yet verified**. Portal responses can change; re-check them if the upstream application changes.

## 1. Scope and approach

The legacy portal is available at `https://urja-ops.flockenergy.tech`.

The goal is to provide a clean, documented API over meter metadata, transformer/network relationships, meter location, and recent energy readings. The investigation used the portal UI and browser Network tools, direct requests to portal endpoints while authenticated, and the exported meter JSON.

The current adapter combines a local meter metadata snapshot (`meter.json`) with live portal requests for transformer data, energy readings, and geographic coordinates.

## 2. Authentication and session behavior

### Observed

- Portal API requests require an authenticated session. Unauthenticated or expired-session requests can return a response similar to:

  ```json
  {
    "error": "unauthorized",
    "message": "A valid session is required."
  }
  ```

- The observed browser session cookie is named `__Secure-better-auth.session_token`.
- Observed cookie attributes included `Max-Age=3600`, `Path=/`, `HttpOnly`, `Secure`, and `SameSite=Lax`.
- During testing, the session expired after roughly 30–40 minutes and required logging in again. The exact lifetime may vary.
- The adapter reads the cookie value from `PORTAL_SESSION_COOKIE` in a local `.env` file and sends it using the cookie name above.
- The session token is secret. Do not commit `.env`, put the token in this document, or expose it in logs or a public repository.

### Adapter behavior

- Portal `401`/`403` responses are translated to local HTTP `401` with a message asking the user to log in again and update `PORTAL_SESSION_COOKIE`.
- A portal timeout is translated to local HTTP `504`.
- Other HTTP/network errors are generally translated to local HTTP `502`.
- If the cookie is not configured, the adapter returns local HTTP `500`.

### Not established

- No supported automated login or session-refresh API has been confirmed.
- No refresh-token flow has been confirmed, so the adapter does not attempt automatic session renewal.

## 3. Meter list, search, and bulk export

### Observed portal behavior

The meter search/list page was observed at:

`GET /portal/meters/search?q=&page=1`

The portal accepts a `q` search parameter and page number. UI search was observed to match meter IDs and serial numbers, including partial matches. For example, `SE4` returned multiple matches, while `SE33962` returned one matching result.

At the time of investigation, the meter listing showed:

- **403 total meter records**
- **20 records per page**
- **21 pages**

One observed attempt to click a meter row did not open the detail view; clicking the meter ID link did open the meter detail page. This is an observed UI behavior, not a guarantee for every browser state.

### Bulk export

The portal UI provided an **Export all meters** action. It produced a JSON array containing all 403 meter records. The local file used by the adapter is `meter.json`.

This is a confirmed bulk path for meter metadata. The exported JSON did not contain energy/consumption readings in the searches performed: searches for `consumption`, `kWh`, `voltage`, and `timestamp` did not find those fields.

### Observed meter JSON fields

A meter record contained fields including:

- `meterId`
- `serialNo`
- `make`
- `phaseType`
- `installStatus`
- `installType`
- `build`
- `dtCode`
- `hierarchy`
- `geo`

The `hierarchy` object contained levels such as `zone`, `circle`, `division`, `subdivision`, `substation`, `feeder`, and `dt`. Each hierarchy node can contain `name` and `code`; some values may be blank. The `geo` object contained latitude/longitude coordinates.

Observed examples included makes HPL, L&T, Genus, Secure, and Allied; phase types such as single and three; statuses such as Installed, Faulty, and Decommissioned; installation types such as Whole Current and CT Operated; and build values such as legacy and v2. These are examples, not an exhaustive list of valid values.

### Adapter behavior and limitation

The API loads `meter.json` at startup and builds local indexes for exact meter ID, serial number, transformer code, feeder code, make, and installation status. Exact lookups and common equality filters use these indexes. Partial text search remains a local search over candidate records, which is adequate for the observed dataset size.

`meter.json` is a local snapshot. Changes made to portal meter metadata after the snapshot was exported will not automatically appear in local meter endpoints. The current implementation does not automatically re-download the export.

## 4. Transformer (distribution transformer / DT) data

### Observed portal endpoint

The transformer tab's data request was observed at:

- `GET /portal/dts?page=1`
- `GET /portal/dts?page=2`

Page 1 returned a JSON object with `data`, `total`, `page`, and `pageSize`. An abbreviated example:

```json
{
  "data": [
    {
      "code": "DT-001",
      "name": "Malviya Nagar DT 1",
      "feederCode": "F-001",
      "capacityKva": 100
    }
  ],
  "total": 40,
  "page": 1,
  "pageSize": 20
}
```

The actual observed page contained 20 records. The reported total was **40 transformers**, with **20 per page**, meaning two pages at the time of observation. Both page 1 and page 2 were tested.

Transformer fields:

- `code` — identifier such as `DT-001`
- `name` — transformer name
- `feederCode` — associated feeder code
- `capacityKva` — capacity in kVA

Examples included DT-001 (100 kVA), DT-002 (63 kVA), DT-003 (250 kVA), and capacities such as 160 and 400 kVA. These examples do not define all allowed capacities.

### Bulk access finding

Pagination is a confirmed way to retrieve the complete transformer dataset: read `total` and `pageSize` from the first response, then request every page. The adapter calculates the number of pages rather than hardcoding two.

A separate one-request transformer export/download route has **not** been confirmed. `/portal/dts?page=1` is one page, not a single-call full export.

### Relationship to meters

The meter export contains `dtCode` and/or a DT code under `hierarchy.dt.code`. Transformer records use `code`. Values such as `DT-001` are the shared join key for associating meters with transformers. The observed relationship is one-to-many: multiple meter records can reference one transformer. Transformer `feederCode` can be compared with a meter's hierarchy feeder code.

The meter export did not include transformer `capacityKva`; the dedicated transformer endpoint is the source for that field.

## 5. Meter detail, energy readings, and geographic coordinates

### Meter detail and SvelteKit data

A detail page was observed at a path shaped like `/meters/{meter_id}`, including `/meters/J100000`. A SvelteKit data request was also observed in this form:

`/meters/J100000/__data.json?x-sveltekit-invalidated=001`

This SvelteKit route is an implementation detail of the portal UI and may change. The current adapter does not rely on it for the energy/geo proxy endpoints.

The detail view showed meter metadata, hierarchy breadcrumbs, geographic information, and a consumption table.

### Energy endpoint

An energy API request was observed in this form:

`GET /portal/meters/{meter_id}/energy`

An observed example used `J100001`: `/portal/meters/J100001/energy`.

The response contained a `data` array of readings with fields including:

- `timestamp`
- `kwh`
- `kvah`
- `voltR`

Sample readings covered late June 2026 and appeared at half-hour intervals. A detail-page screenshot also showed half-hourly timestamps and columns labelled TIMESTAMP, KWH, KVAH, and V(R). This sample does not prove all meters always update at that cadence.

No confirmed all-meters bulk energy endpoint has been established. The meter export did not contain the energy fields searched for.

### Geographic endpoint

A geo request was observed in this form:

`GET /portal/meters/{meter_id}/geo`

An observed example used `J100000`: `/portal/meters/J100000/geo`.

The response shape was:

```json
{
  "data": {
    "latitude": "26.822136543835608",
    "longitude": "75.90718190602279"
  }
}
```

Coordinates were strings in that response. The meter export also contained a `geo` object, so location data can be present in the local snapshot as well as through the live endpoint.

### Meter ID caveat

The energy observation used `J100001`, while the geo/detail observations included `J100000`. These were different examples during investigation. Do not assume the sample energy and geo responses describe the same meter or the same reading values.

### Adapter behavior

- `GET /meters/{meter_id}/energy` validates the meter ID against the local snapshot, then proxies to `/portal/meters/{meter_id}/energy`.
- `GET /meters/{meter_id}/geo` validates the meter ID against the local snapshot, then proxies to `/portal/meters/{meter_id}/geo`.
- Energy and geo currently use live portal requests and do not use the transformer cache.

## 6. Observed UI and data quirks

- Clicking an arbitrary meter row did not trigger visible navigation/request in one test; clicking the meter ID link opened the detail page.
- Meter search supports partial text matches across meter ID and serial number.
- Some hierarchy fields in the meter export can be blank.
- Meter metadata is available through a full JSON export; transformer metadata is confirmed through paginated JSON responses.
- Energy readings are available through a per-meter endpoint; the meter export did not contain the energy fields searched for.
- The geo endpoint returned latitude and longitude as strings in the observed response, while the exported meter JSON also had a `geo` object.
- A portal session can expire while the local API process is still running.
- The browser's Network tab shows browser-to-local-API requests when testing `127.0.0.1`; upstream portal requests are made by the FastAPI backend and ordinarily will not appear as browser requests in that tab.
- The observed SvelteKit `__data.json` request is a UI data route and is not used by the current adapter.
- The observed energy sample appeared to use half-hour intervals, but that cadence has not been verified for every meter or time period.

## 7. Local API implemented over the portal

These routes belong to **this adapter API**, not necessarily to the legacy portal.

| Local route | Purpose | Data source |
|---|---|---|
| `GET /health` | Report API status and number of meter records loaded | Local process |
| `GET /meters` | List meter records with filters and pagination | Local `meter.json` snapshot and indexes |
| `GET /meters/search?q=...` | Partial, case-insensitive search by meter ID or serial number | Local snapshot |
| `GET /meters/search-by-serial/{serial_no}` | Exact serial-number lookup | Local index |
| `GET /meters/{meter_id}` | Exact meter lookup | Local index |
| `GET /meters/{meter_id}/energy` | Get readings for a known meter | Live portal proxy |
| `GET /meters/{meter_id}/geo` | Get coordinates for a known meter | Live portal proxy |
| `GET /transformers` | Return the complete transformer dataset | All portal pages, cached locally |
| `GET /transformers/{code}/meters` | Return a transformer and associated local meter records | Transformer cache + local meter index |
| `GET /network/hierarchy` | Return distinct hierarchy nodes in the meter snapshot | Local `meter.json` snapshot |

### Meter filters currently supported

`GET /meters` accepts optional query parameters:

- `q` — partial, case-insensitive search across `meterId` and `serialNo`
- `make`
- `install_status`
- `transformer_code`
- `feeder_code`
- `phase_type`
- `install_type`
- `zone`
- `circle`
- `division`
- `subdivision`
- `substation`
- `page` (default 1)
- `page_size` (default 20, maximum 100)

Equality filters are case-insensitive. Hierarchy filters match either the hierarchy node's `code` or `name`. `q` is partial matching; other filters are exact-value matching. Pagination is applied after filtering.

### Transformer pagination and cache

- On a cache miss or expiry, the API fetches `/portal/dts?page=1`, reads `total` and `pageSize`, and requests the remaining pages.
- The complete dataset is cached in process memory for 30 minutes.
- Successful cache responses include `fetchedAt` and `stale: false`.
- If refresh fails after a prior successful fetch, the API can return the previous dataset with `stale: true` and a `refreshError` field.
- If no cached dataset exists and fetching fails, the error is returned instead of fabricating data.
- The cache is in-memory: a server restart clears it, and multiple worker processes have separate caches.
- Stale fallback currently applies to transformers only, not meter metadata, energy readings, or geo requests.

## 8. Local API error behavior

Implemented/observed behavior:

- `200 OK` — successful endpoint response.
- `404 Not Found` — unknown meter, transformer, or route.
- `401 Unauthorized` — portal authentication failure when no prior transformer cache is available; if an expired transformer cache exists, a stale response may instead be returned.
- `500 Internal Server Error` — portal session cookie is not configured.
- `502 Bad Gateway` — upstream HTTP failure, invalid JSON/response shape, or incomplete/inconsistent transformer pagination.
- `504 Gateway Timeout` — upstream request timeout.

When an expired transformer cache exists and refresh fails with an expected `HTTPException`, the API returns the previous dataset with `stale: true` and the refresh error. Consumers must inspect `stale` instead of assuming every `200` response contains fresh data.

## 9. Cache freshness semantics

The transformer cache records:

- `fetchedAt` — when the API successfully fetched the complete dataset
- Internal monotonic expiry — used to determine whether the TTL elapsed
- `stale` — whether the returned dataset is being served after a failed refresh

The 30-minute TTL is an implementation policy, not an upstream freshness guarantee. It means the adapter tries to refresh after that duration; it does not mean the portal updates transformer data every 30 minutes.

For energy readings, the reading-level `timestamp` is the source's reported measurement time. If energy caching is added later, cache retrieval time and the latest reading timestamp should remain separate concepts.

## 10. Test coverage and limitations

The current `test.py` suite mocks portal responses for upstream-dependent behavior. It tests:

- Health endpoint
- Meter listing and pagination
- Partial meter search
- Make, installation-status, combined, transformer, and hierarchy filters
- Existing and missing meter lookup
- Energy and geo proxy behavior using mocks
- Network hierarchy
- Multi-page transformer retrieval
- Transformer cache hit
- Expired-cache refresh
- Stale fallback after refresh failure
- Failure when no cache exists
- Transformer-to-meter relationship
- Invalid transformer pagination
- Unknown route behavior

The recorded local test run completed with **18 tests passed**. This confirms tested code paths against the test fixtures/mocks; it does not replace a live integration check against the portal.

## 11. Unverified items and future investigation

Keep these explicitly unconfirmed until tested:

1. Whether the portal exposes a single-request transformer export endpoint beyond paginated `/portal/dts?page=N`.
2. Whether a bulk energy endpoint, multi-meter energy endpoint, or supported date-range parameter exists.
3. Whether a dedicated bulk geo export exists; the meter snapshot already contains geo fields.
4. Whether the portal provides ETag/Last-Modified headers or another conditional-refresh mechanism.
5. Whether session renewal can be automated through a supported API.
6. Whether reading cadence is always 30 minutes for every meter and period.
7. Whether the meter export can be refreshed programmatically rather than through the UI.
8. Whether endpoint schemas and pagination behavior remain stable over time.

Do not claim these capabilities until an actual request or portal behavior confirms them.