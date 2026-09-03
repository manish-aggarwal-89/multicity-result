> **CORRECTION (2026-09-03):** the "MH+infant multicity cannot book" conclusion below was
> **date/availability specific to 2027-02-05**, not a hard limit. The same route/pax
> (`SQ~SQ~MH~SQ`, 1A1C1I) books cleanly via VI on **2027-03-01** (PNR ISKAEC; and GBINOU under
> multiFare). It depends on whether the MH flight/date has an infant fare/RBD filed. See
> `runs/MULTIFARE-OWRT-VI-RESULTS.md`.

# VI integrator — Sabre multi-city booking (4 & 5 segments, future dates)

Flow tested: **VI swagger `POST /tix-flight-vi-integrator/booking`** (fan-out to Sabre GDS) →
**cancel on local Sabre `:8857` `POST /tix-flight-sabre-gds-integrator/cancel-booking`** →
verify GetReservation + cancel in `TIX-FLIGHT-SABRE-GDS-INTEGRATOR-BE/app.log`.

Supplier: **sabre** (`distributionType`). Dates: ~5 months out. Route family: `CGK-SIN-BKK-KUL-CGK`
(4 seg) and the 5-seg extension.

## Config fix that unblocked VI (root cause of the initial TIMEOUT)

VI's `integrator_config` (Mongo + Redis) pointed at a **stale Sabre booking endpoint**, so every
VI→Sabre book hung and VI returned `TIMEOUT` after 30s. Corrected endpoint:

```
http://krakend-gateway-flight.flight-ns.svc.tiket/krakend-gateway/tix-flight-sabre-gds-integrator/booking
```

Pushed via the `com.tiket.tix.flight.rule.integratorConfig` Kafka topic (`IntegratorConfigEvent`,
which upserts Mongo and refreshes the Redis cache). After that, non-infant VI bookings succeed.

## Results matrix

| Segments | Pax      | VI booking | Cancel (local Sabre) | Notes |
|----------|----------|------------|----------------------|-------|
| 4        | 1A       | SUCCESS    | SUCCESS              | PNR retrieved + cancelled in app.log |
| 4        | 1A1C     | SUCCESS    | SUCCESS              | child OK |
| 4        | 1A1C1I   | **FAILED** | n/a (no PNR)         | Sabre air-book `*NO FARES/RBD/CARRIER` on MH leg |
| 4        | 2A2C1I   | **FAILED** | n/a (no PNR)         | same |
| 5        | 1A       | SUCCESS    | SUCCESS              | |
| 5        | 1A1C1I   | search 0 combos | n/a             | thin availability for 5-seg + infant |
| 5        | 2A2C1I   | search 0 combos | n/a             | thin availability |

## Root cause of the infant failures — NOT a VI or integrator bug

The VI combo engine returns **SQ × MH (Malindo) interline** combos for these routes
(e.g. `SQ 951~SQ 720~MH 797~SQ 103`). Reproduced deterministically on **local Sabre `:8857`**,
reading the raw `CreatePassengerNameRecordRS`:

```
ApplicationResults.status = "Incomplete"
ERR.SP.PROVIDER_ERROR            : "Unable to perform air booking step"
WARN.SWS.HOST.ERROR_IN_RESPONSE  : "EnhancedAirBookRQ: *NO FARES/RBD/CARRIER"
```

The integrator payload is correct — the infant SSR is built and associated to the adult:
`SSR_Code INFT, PersonName 1.1, Text "Aggarwal/Krishna mstr/25Dec25"`. The host rejects the
**air-book** because there is no filed infant fare/RBD for the **MH interline** segment.

### A/B proof (same MH 797 leg, local Sabre)

| Pax     | flightSelect                         | Result |
|---------|--------------------------------------|--------|
| 1A      | `SQ 955~SQ 710~MH 797~SQ 103\|SQ 958`| **SUCCESS** — PNR `KIVAST` (cancelled) |
| 1A0C1I  | `SQ 957~SQ 710~MH 797~SQ 103\|SQ 958`| **FAIL** — `*NO FARES/RBD/CARRIER` |
| 1A1C1I  | `SQ 951~SQ 720~MH 797~SQ 103\|SQ 958`| **FAIL** — `*NO FARES/RBD/CARRIER` |

Adult (and adult+child) book fine on the identical MH leg; adding an **infant** flips it to
`*NO FARES/RBD/CARRIER`. An all-SQ multi-city (no MH) books infants fine (earlier local PNR
`EQSNAT`). So the failure is a **supplier/interline fare-filing limitation** (no MH infant fare on
this interline itinerary), surfaced because VI stitches SQ×MH combos — not a defect in VI or the
Sabre integrator.

## Evidence

`evidence/{A_1A1C1I_FAIL,B_1A_SUCCESS,C_1A0C1I_FAIL}.json` — flightSelect + the raw Sabre
`ERR/WARN` codes for each run. Full `book_applog.log` deltas live in the run dumps under
`~/Desktop/mcp-apis/sabre-gds-multicity/INFANT-MH-LOCAL/`.

## Recommendation

VI/funnel combo engine should **avoid or downgrade SQ×MH (interline) combos when an infant is in
the party** for these routes, or skip infant-bearing itineraries whose interline carrier has no
filed infant fare. No code change is warranted in the VI or Sabre integrator — both behave
correctly and the host error is authoritative.
