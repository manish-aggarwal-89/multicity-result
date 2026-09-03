# Multifare + One-way/Round-trip backward-compat + VI — results

Date: 2026-09-03. Supplier: Sabre GDS (`distribution=sabre`). Dates ~5–6 months out.
Direct-integrator cases run on **local** Sabre (`:8857`) — real PNR booked then cancelled.
VI cases **book on the cluster** VI integrator, then **cancel on local Sabre** (`:8857`);
the local `app.log` is parsed to prove the PNR was retrieved + cancelled (booking valid).

## 1. What "multicity multifare" is (revalidate-itinerary-prp)

- **PRP = Product Review Page.** `/revalidate-itinerary-prp` = the revalidate mode that returns
  **all branded fare options** for the chosen flights (`forceIncludeOtherFares=true`), regardless of
  the `multiFare` flag. Alternatives land in `data.mainFare.<tripBucket>.otherFares` (nested), not the
  empty top-level `data.otherFares`.
- **`multiFare` (bool) on the booking/revalidate request** switches the fare-selection behaviour:
  verification logic `M` (vs `B`), keeps the preferred fare basis, adds Sabre AirPrice
  `PricingQualifiers.Brand` (`SpecificBrand` per segment from `itineraries[].brandedFare.code`), and
  enables the **M→B retry** that surfaces as `FARE_ALREADY_SOLD_OUT` when the branded fare is gone.
- **Multicity multifare is itinerary-level** (one combined price across all legs), response bucket
  `mainFare.multiCities`. For these SQ/MH interline itineraries Sabre files **no branded fare
  families**, so `otherFares` is usually empty — the `multiFare=true` path is still exercised
  (verification M + brand qualifiers), there is simply one fare to sell.
- Booking never calls `/revalidate-itinerary-prp` internally; it always uses the standard revalidate
  context built from the book request. `fareSellKey`/`journeySellKey` are unused by Sabre GDS.

Refs: `revalidate_context.go`, `booking_service.go` (M→B retry), `book_request_constructor_helper.go`
(`buildBrandsForPricingQualifiers`), `select_journey_helper.go`, `integrator_book_request_helper.go`
(`DefineTripType`), `trip_type.go` (enum has no `ONE_WAY` — only DEPARTURE/ROUND_TRIP/MULTI_CITY).

## 2. Multicity with multiFare=true (all of yesterday's cases) — local

Each case: search → revalidate → **revalidate-itinerary-prp** → **booking (multiFare=true)** → cancel.
Booking + PRP requests verified to carry `multiFare:true`; fares reconciled by `fare_validator.py`.

| Case | seg | pax | Result | PNR |
|------|-----|-----|--------|-----|
| FINAL-1A1C1I-2SEG-MF | 2 | 1A1C1I | PASS | IRRRFD |
| FINAL-1A1C1I-4SEG-MF | 4 | 1A1C1I | PASS | GIPLUD |
| FINAL-1A1C1I-5SEG-MF | 5 | 1A1C1I | NO_DATA (supplier: no inventory) | — |
| FINAL-2A-2SEG-MF | 2 | 2A | PASS | GXVXYG |
| FINAL-2A-4SEG-MF | 4 | 2A | PASS | GIHEHF |
| FINAL-2A-5SEG-MF | 5 | 2A | PASS | GGHPKC |
| FINAL-2A2C2I-2SEG-MF | 2 | 2A2C2I | PASS | GDSOGD |
| FINAL-2A2C2I-4SEG-MF | 4 | 2A2C2I | PASS | GEXYGO |
| FINAL-2A2C2I-5SEG-MF | 5 | 2A2C2I | NO_DATA (supplier: no inventory) | — |

7/9 booked + validated. The two 5-seg infant/big-pax cases returned NO_DATA (thin availability on the
5-leg route), not a defect.

## 3. One-way / round-trip backward compatibility — local

The sign-off harness previously only extracted `multiCities`, so OW/RT never selected a combo and were
never actually booked (their old dumps have search only, no booking). Fixed the combo extraction to
normalise `departureFlights` (unwrapping `searchAvailability`) and `roundTripCombines`
(`departureFlights`+`returnFlights`) into the multi-city shape, normalised `ONE_WAY→DEPARTURE`, and let
the booking request omit `tripType` for OW/RT so the integrator's `DefineTripType` infers DEPARTURE (1
itinerary) / ROUND_TRIP (2). Round-trip needs a **distinct return date** — a same-day RT 500s on the
cluster search.

| Case | trip | pax | baseline | multiFare=true |
|------|------|-----|----------|----------------|
| FINAL-ONEWAY-1A1C1I | one-way | 1A1C1I | PASS (ITUYIY) | PASS (UIGSWM) |
| FINAL-ROUNDTRIP-2A | round-trip | 2A | PASS (ENLDFM) | PASS (UEMKNX) |

## 4. VI integrator — book on cluster, cancel on local Sabre

VI booking succeeds and the resulting PNR is retrieved + cancelled on local Sabre (app.log verified).

| Case | trip / mode | pax | Result | PNR |
|------|-------------|-----|--------|-----|
| VI-OW-1A | one-way | 1A | PASS | UEUEHX |
| VI-OW-1A1C1I | one-way | 1A1C1I | PASS | KKCNLD |
| VI-RT-2A | round-trip | 2A | PASS | EPOBND |
| VI-RT-2A-MF | round-trip + multiFare | 2A | PASS | ISIAVT (after FARE_ALREADY_SOLD_OUT retries) |
| VI-MC4-1A-MF | multi-city + multiFare | 1A | PASS | GWDOBG |
| VI-MC4-1A1C1I-MF | multi-city + multiFare + infant | 1A1C1I | PASS | GBINOU |
| VI-MC4-1A1C1I | multi-city (repro of the success case) | 1A1C1I | PASS | ISKAEC |

The RT-MF `FARE_ALREADY_SOLD_OUT` on the first combos is the **expected multiFare M→B retry** signal,
not a bug; the harness advanced to a bookable combo.

## 5. Correction to the earlier VI infant finding

Yesterday's VI run (`VI-SABRE-MULTICITY/FINDINGS.md`) concluded MH+infant multicity could not book
(`*NO FARES/RBD/CARRIER`). This was **date/fare-availability specific to 2027-02-05**, not a hard
limit: the same route/pax (`SQ~SQ~MH~SQ`, 1A1C1I) books cleanly on **2027-03-01** via VI (PNR ISKAEC,
and again GBINOU under multiFare). Whether an MH interline leg is bookable with an infant depends on
whether that flight/date has an infant fare/RBD filed — an availability property, not a VI/integrator
defect.

## Harness changes backing these results

- `engine/orchestrator.py::_combos` — normalise OW/RT search responses into the multi-city combo shape.
- `engine/requests.py` — `_norm_trip` (ONE_WAY→DEPARTURE), `multi_fare` + `trip_type` params on the
  revalidate/booking builders, `brandedFare` overlay, `tripType` inferred for OW/RT.
- `engine/business.py` — the three multi-city-only fare rules SKIP for OW/RT (they compare
  `mainFare.multiCities`, which OW/RT don't have).
- `tools/multifare_run.py` — runs the yesterday matrix with `multiFare=true`.
- `tools/vi_run.py` — adds OW/RT/multifare VI shapes.
- `tests/test_trip_multifare.py` — self-check for the normalisation + multifare wiring (5 checks).
