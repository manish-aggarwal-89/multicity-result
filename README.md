# Multi-city integrator run dumps

Overflow store for the Sabre-GDS Multi-City integrator MCP. The **primary dump is the Confluence
child page** under [Sabre-GDS Multi-City run dumps](https://borobudur.atlassian.net/wiki/spaces/~71202004c18360a29f474988bb0ada2ca9eae4/pages/4924145709):
verdict, fare summary, every curl, every request body, and the small response bodies.

Response bodies too big to sit on a wiki page land here, byte-identical to the run:

```
runs/<TC-id>/<timestamp>/<NN>_<step>.response.json
runs/<TC-id>/<timestamp>/01_search.response.json.gz.b64
```

`.response.json` is indented JSON, so GitHub renders and diffs it. The search body is ~1 MB, which
the contents API will not take in one write, so it is compact JSON, gzipped, then base64:

```bash
curl -sL https://raw.githubusercontent.com/manish-aggarwal-89/multicity-result/main/runs/TC-011/2026-08-31_18-38-52_b211e5/01_search.response.json.gz.b64 \
  | python3 -c "import base64,gzip,sys; sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))" > 01_search.response.json
```

The results Google Sheet **Dump** column links the Confluence page, never a file in here.

## Validating fares (`fare_validator.py`)

`fare_validator.py` walks every run under a directory and checks that fares reconcile **within**
each response and **across** the search → revalidate → booking flow. Point it at the `runs/` tree
(local path or this GitHub URL) and it reports every fare that does not match, in one shot.

```bash
python3 fare_validator.py                       # embedded self-check (no data needed)
python3 fare_validator.py runs                  # validate every run under ./runs
python3 fare_validator.py runs/TC-292/2026-09-01_15-26-35_7c0a6d   # one run
python3 fare_validator.py https://github.com/manish-aggarwal-89/multicity-result/tree/main/runs
```

The `.gz.b64` search bodies are decoded automatically; no dependencies beyond the Python 3 stdlib.

### What it checks per run

1. **Search self-check** (whenever a search response exists) — for every offered fare:
   - per pax type: `base + tax − supplierDiscount == total`
   - grand total: `originalTotalFare.total == Σ paxCount × paxTotal`
     (counts from `data.integratorFareRequest`)
2. **Booking internal reconciliation** (whenever a booking response exists):
   - per pax, per itinerary: `base + tax + psc + iwjr − discount == fare`
   - `itinerary.original{Pax}Fare` equals that per-pax fare
   - `itinerary.originalFare == Σ perPaxFare × count`
   - `passengerFareInfo.{pax}` equals the per-pax breakdown × count
   - `originalBookingFare.{fare,adultFare,…}` equals the rolled-up totals
   - tax breakdown: `Σ breakdownTaxes[].amount == tax` (per pax type, when a breakdown is present)
3. **Cross-stage fare chain** `search → revalidate → revalidate_itin_prp → booking` — the chosen
   itinerary is matched across stages by its flight-number sequence, and **every field**
   (`total`, `base`, `tax`) of the per-single-pax whole-trip fare (adult / child / infant) must be
   identical between each adjacent stage — a matching `total` with a different base/tax split fails.
   Per-segment check-in **baggage** offered in search is also cross-checked against the booking
   (matched by airline + flight number + route, not position).

Runs that contain only a search response get just the self-check; partial chains
(e.g. search + revalidate, no booking) are handled automatically. Exit code is non-zero if any run
fails. Money comparisons use a sub-unit tolerance so fares split across legs into repeating decimals
(e.g. `9887600 / 3`) don't produce false positives.

### File names understood in each run dir

| pattern | role | notes |
| --- | --- | --- |
| `*search*.json` / `*search*.json.gz.b64` | search | gz.b64 auto-decoded |
| `*revalidate*.json` | revalidate | e.g. `02_revalidate`, `03_revalidate_itin_prp` |
| `*booking*.json` | booking | highest-numbered wins (`04_booking` over `03_booking`) |
