# Diagnosis: TC-147 / TC-194 / TC-292 (local rerun + supplier verification)

Reran all three locally against the local Sabre integrator (`localhost:8857`,
`SABRE_GDS_MCP_ENV=local`) through **search → revalidate → booking → cancel**, and
compared our API responses field-by-field against the raw Sabre payloads pulled
from the integrator's own `app.log` (saved under each run's `*_diag/outbound/`).

Diag run dirs:
- `TC-147/2026-09-01_19-03-42_f12737_diag`
- `TC-194/2026-09-01_19-04-55_d04157_diag`
- `TC-292/2026-09-01_19-05-15_73ce0d_diag`

## TC-194 — GENUINE INTEGRATOR BUG (reproduced) ✗

Route CGK→DPS→KNO, carriers JT (Lion) + OD (Batik Air Malaysia).

| Segment | Sabre `Book` (authoritative) | our SEARCH | our REVALIDATE | our BOOKING |
|---|---|---|---|---|
| JT12 CGK→DPS | `10K` / `FreeBaggageAllowance KG010` | 10kg ✓ | 10kg ✓ | 10kg ✓ |
| OD178 DPS→KUL | **`NIL`** / `NONIL` / "UNKNOWN - CONTACT OD" | 0kg ✓ | 0kg ✓ | **10kg ✗** |
| OD326 KUL→KNO | **`NIL`** / `NONIL` | 0kg ✓ | 0kg ✓ | **10kg ✗** |

Sabre explicitly returns **NIL / unknown** free checked baggage for the two OD
segments (TIR `FlightSegment.BaggageAllowance.Number = "NIL"`,
`FareCalculationBreakdown.FreeBaggageAllowance = "NONIL"`,
NonUS_DOT: "BAG ALLOWANCE -DPSKNO-NIL/OD", "BAGGAGE ALLOWANCES/FEES UNKNOWN - CONTACT OD").

**Search and revalidate map this correctly to 0. Only the BOOKING response is wrong:**
it stamps every segment with **10kg** — i.e. the *first* leg's allowance (JT's 10kg)
gets applied itinerary-wide instead of per-segment. So the booking response
**overstates free checked baggage** on carriers/segments that Sabre reports as
NIL/unknown (OD / Batik Air Malaysia here).

Where to look (booking-response baggage construction):
`internal/shared/helper/book_response_constructor_helper.go`
+ `internal/shared/helper/book_response_itinerary_builder.go`. Baggage must be
resolved **per segment** (honoring NIL → 0). The itinerary-wide check-in fallback
(`getCheckInBaggageKgFromCPNR`, applied at ~L784 when a segment resolves to 0) is
not segment-aware and back-fills NIL segments with another leg's weight.
> Note: the deployed local binary may predate the current source of that file, so
> please confirm against the exact build running on `localhost:8857`. The observed
> runtime behavior (all segments = first leg's 10kg) is the thing to fix.

## TC-147 — NOT reproduced locally (booking correct) ✓

Route CGK→SIN→BKK→KUL→CGK, all MH (Malaysia Airlines). Sabre `Book` returned
`FreeBaggageAllowance KG035` and "BAG ALLOWANCE -CGKSIN-35KG/MH" for every leg;
our search **and** booking both showed **35kg** on all six segments — PASS.
The earlier 35→10 failure (staging run `12-42-20`) did not reproduce on local.
Most likely the same per-segment vs itinerary-wide baggage weakness as TC-194
surfacing on a mixed-allowance fare, or a stale/staging build. No local evidence
of a fare or booking arithmetic problem for this itinerary.

## TC-292 — SUPPLIER-DRIVEN, not an integrator bug ✓

Route CGK→SIN→BKK, SQ (Singapore Airlines). On local, search == revalidate ==
booking (adult total 4,719,600; base 3,161,000; tax 1,558,600) and baggage 25kg
consistent across all stages — PASS.

The earlier failure (staging `15-26-35`: adult total search 5,097,600 →
revalidate 7,565,600, base 3,539,000 → 6,007,000, baggage 25→30) is a
**shop-vs-reprice fare change**: the fare BFM/search offered was no longer
available at `RevalidateItinerary` (live pricing), so Sabre repriced to a higher
fare family — which also carries different baggage (25 → 30kg). This is Sabre
inventory/pricing behavior, not an integrator miscalculation: at each stage our
fares match Sabre's own response for that stage (supplier fare-match rules pass).
Product concern worth noting (search price/baggage not guaranteed until reprice),
but nothing to fix in the integrator's math.

## Summary

| Case | Verdict | Fix owner |
|---|---|---|
| TC-194 | Booking overstates checked baggage on NIL/unknown segments (per-segment mapping bug) | **Integrator** |
| TC-147 | Correct on local; old failure not reproduced (likely same baggage weakness / stale build) | watch |
| TC-292 | Supplier reprice shop→revalidate (fare + baggage change) | not a bug |
