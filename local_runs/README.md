# local_runs — issuance-prep (Sabre GDS local → staging)

Local integrator (`localhost:8857`) pointed at Sabre **staging** (`is_staging: true`,
credentials via `FindBySupplierAndIsStaging`; host is Sabre cert
`api.cert.platform.sabre.com`). Distribution: **sabre-gds** (direct integrator).

**Flow per case:** `search → revalidate → revalidate_itin_prp → booking` then STOP.
- **No cancel** call (`release_pnr=false`) — PNR left **LIVE** for issuance.
- **No issuance** call — the issuance curl is only *emitted* (`ISSUANCE_CURL.sh`), never POSTed.

Harness: `mcp/multicity/tools/issuance_run_local.py` (flow `search_to_issuance_curl`).
Pax: **1 adult**. Dates: ~180 days out (2027-03-03). Routes: CGK-SIN-BKK-KUL legs.

## Results (1 adult)

| Case | Segments | Booking code (live PNR) | Booking | Grade | Note |
|---|---|---|---|---|---|
| LOCAL-1A-2SEG | 2 | `PSBYQS` | SUCCESS | FAIL* | PRP repriced far above SRP (6.1M → 38.2M) |
| LOCAL-1A-3SEG | 3 | `PKUPAT` | SUCCESS | PASS | clean |
| LOCAL-1A-4SEG | 4 | `PYQWYB` | SUCCESS | FAIL* | PRP repriced +1.0M vs SRP (29.8M → 30.8M) |

\* `FAIL` = `prp_total_matches_srp` only: the revalidate-PRP quote differs from the
search (SRP) quote for the same combo. This is **supplier repricing** (thin staging
inventory reprices the cheap search fare upward), **not** a booking failure. The PNR
is created at the PRP/booked price — which is exactly what issuance would settle.

## Per-run contents
- `01_search.response.json.gz.b64` — search (gzip+base64; decode with `decompress_search.py`)
- `02_revalidate.response.json`, `03_revalidate_itin_prp.response.json`
- `04_booking.response.json` — booking (`code: SUCCESS`, `bookingCode`)
- `outbound/` — raw Sabre payloads from app.log (incl. `04_booking.supplier_Book_response.json` with the record locator)
- `ISSUANCE_CURL.sh` — the not-executed `/issued` POST (ready to fire by hand)
- `ISSUANCE_INFO.json` — booking_code, pnr_released=false, and the (also not-executed) cancel curl
- `<id>_run.txt` — full dump (all curls, requests, responses, validation)

## Issuance test — 4-month+ dates (2027-01-07, +125d)

Fresh bookings then a real `/issued` POST. Harness: `issuance_run_local.py` with
`ISSUE_FOR_REAL=1 ISSUANCE_START_DAYS=125`. Post-booking the harness reads the
PNR segment status (HK=confirmed / HX=holding-cancelled) from the `/issued`
flow's `GetReservationV2` in app.log.

| Case | Seg | PNR | `distributionType: sabre-gds` | `distributionType: sabre` |
|---|---|---|---|---|
| LOCAL-1A-2SEG | 2 | `PSHPQY` | CREDENTIALS_NOT_FOUND | **SUCCESS (issued)** |
| LOCAL-1A-3SEG | 3 | `PRAJTK` | CREDENTIALS_NOT_FOUND | **SUCCESS (issued)** |
| LOCAL-1A-4SEG | 4 | `PGYZSJ` | CREDENTIALS_NOT_FOUND | **SUCCESS (issued)** |

**Findings**
1. **Issuance works at ~4-month dates for 2/3/4-seg multi-city** — all three issued.
2. **`distributionType` must be `sabre`, not `sabre-gds`.** Staging seeds the
   issuance/ticketing credential under `sabre`; `sabre-gds` → `CREDENTIALS_NOT_FOUND`
   ("credential is empty"). Fixed in `_do_issue` (env `ISSUANCE_DIST`, default `sabre`).
3. The earlier **6-month** attempt (PYQWYB) failed with Sabre `HX SEG STATUS NOT
   ALLOWED-2122` — segments were `HX` (holding-cancelled), not ticketable. At
   4-month dates the segments confirm, so issuance succeeds.

Each run's `ISSUANCE_RESULT.json` records both the `sabre-gds` failure and the
final `sabre` success.

## To issue one by hand (side effect — creates a ticket)
`ISSUANCE_CURL.sh` is emitted with `distributionType: "sabre-gds"` — **change it to
`"sabre"`** or issuance returns `CREDENTIALS_NOT_FOUND`:
```bash
sed 's/"sabre-gds"/"sabre"/' local_runs/LOCAL-1A-3SEG/<stamp>/ISSUANCE_CURL.sh | bash
```
To release instead of issuing, use the `cancel_curl` in `ISSUANCE_INFO.json`.
