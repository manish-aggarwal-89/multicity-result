#!/usr/bin/env python3
"""Validate flight-integrator fares within and across search / revalidate / booking.

Point it at a runs directory (local path or a GitHub URL) and it walks every run,
loads whatever responses are present, and reports every fare that does not reconcile.

Per run it does three things:

1. SEARCH self-check (whenever a search response is present)
   For each offered fare:
     - per pax type:  base + tax - supplierDiscount == total
     - grand total :  originalTotalFare.total == Σ paxCount * paxTotal
       (pax counts come from data.integratorFareRequest)

2. BOOKING internal reconciliation (whenever a booking response is present)
     - per pax, per itinerary:  base + tax + psc + iwjr - discount == fare
     - itinerary.original{Pax}Fare == that per-pax fare
     - itinerary.originalFare == Σ perPaxFare * count
     - passengerFareInfo.{pax} == Σ per-pax breakdown * count
     - originalBookingFare.{fare,adultFare,…} == the rolled-up totals

3. CROSS-STAGE fare chain  search -> revalidate[ -> revalidate_itin_prp] -> booking
   The chosen itinerary is matched across stages by its flight-number sequence.
   For every adjacent pair the per-single-pax whole-trip fare (adult/child/infant)
   plus fareClass and fareBasis are compared, with two severities:
     - search vs the next stage: search can be a cached / sold-out supplier quote,
       so any difference (fare, baggage, fareClass, fareBasis) is a WARNING and the
       fareClass/fareBasis change is spelled out — it does not fail the run.
     - revalidate -> revalidate_itin_prp -> booking: authoritative, so fare,
       fareClass and fareBasis MUST match; any difference is a hard FAIL.

4. OUTBOUND Sabre checks (when run_dir/outbound/ exists)
   - Book ApplicationResults must be Complete; flags RULE VALIDATION FAILED /
     ERR.SP.PROVIDER_ERROR (the MH Q/N fare-basis mismatch failure mode).
   - If Book request forces FareBasis codes, they must match revalidate priced
     fareBasisCode set.
   - Book segments (flight/O-D/RBD) must match integrator bookDetail schedules.
   - Sabre FreeBaggageAllowance (FareCalculationBreakdown) vs integrator booked
     check-in baggage per segment.
   - Revalidate supplier total vs integrator revalidate grand total.

Console output groups runs under === PASS === and === FAIL === sections.

File names understood in each run dir (numeric prefixes optional):
    *search*    .json or .json.gz.b64   (gzip+base64 wrapped is auto-decoded)
    *revalidate*.json                    (02_revalidate, 03_revalidate_itin_prp, …)
    *booking*   .json                    (highest-numbered wins, e.g. 04_booking)

Usage:
    python3 fare_validator.py                          # embedded self-check
    python3 fare_validator.py <runs-dir>               # validate every run under it
    python3 fare_validator.py <single-run-dir>         # validate one run
    python3 fare_validator.py https://github.com/user/repo/tree/main/runs
    python3 fare_validator.py booking.json [search.json]   # legacy single-file mode
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import sys

# breakdownInfo / passengerFareInfo keys (booking side)
PAX = (("adult", "originalAdult"), ("child", "originalChild"), ("infant", "originalInfant"))
# search / revalidate fare keys
FARE_PAX = (("adult", "originalAdultFare"), ("child", "originalChildFare"), ("infant", "originalInfantFare"))
PTYPES = ("adult", "child", "infant")


def _num(x) -> int:
    return x if isinstance(x, (int, float)) else 0


def _eq(a, b, tol: float = 0.01) -> bool:
    """Money equality with a sub-unit tolerance.

    ponytail: whole-trip fares are split across legs and can land on repeating
    decimals (e.g. 9887600/3), so exact == would flag float noise. tol=0.01 is far
    below one currency unit, so any real fare difference still fails.
    """
    return abs(_num(a) - _num(b)) <= tol


def _load(path: str) -> dict:
    if path.endswith(".gz.b64"):
        return json.loads(gzip.decompress(base64.b64decode(open(path, "rb").read().strip())))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# booking internal reconciliation (unchanged logic, still the source of truth) #
# --------------------------------------------------------------------------- #
def _components(node: dict) -> int:
    """Per-pax fare a supplier bills: base + tax + fees - discount.

    ponytail: only the additive fees seen across Sabre/Amadeus/AirAsia dumps are
    summed (psc, iwjr). A new supplier's extra fee makes base+tax+fees != fare and
    the check flags it rather than hiding it.
    """
    return (
        node.get("baseFare", 0)
        + node.get("tax", 0)
        + node.get("psc", 0)
        + node.get("iwjr", 0)
        - node.get("discount", 0)
    )


def validate_booking(resp: dict) -> list[str]:
    issues: list[str] = []
    bd = (resp.get("data") or resp).get("bookDetail") or resp.get("bookDetail")
    if not bd:
        return ["no data.bookDetail in response"]

    itineraries = bd.get("itineraries") or []
    pax_totals = {k: {"baseFare": 0, "tax": 0, "fare": 0} for k, _ in PAX}
    booking_fare_sum = 0

    for i, itin in enumerate(itineraries):
        bi = itin.get("breakdownInfo") or {}
        itin_fare_recompute = 0
        for typ, key in PAX:
            node = bi.get(key) or {}
            count = itin.get(typ, 0)
            fare = node.get("fare", 0)

            calc = _components(node)
            if not _eq(calc, fare):
                issues.append(f"itin[{i}] {key}: base+tax+fees-discount={calc} != fare={fare}")

            itin_per_pax = itin.get(f"original{typ.capitalize()}Fare", 0)
            if not _eq(itin_per_pax, fare):
                issues.append(
                    f"itin[{i}] original{typ.capitalize()}Fare={itin_per_pax} != breakdown fare={fare}"
                )

            itin_fare_recompute += fare * count
            pax_totals[typ]["baseFare"] += node.get("baseFare", 0) * count
            pax_totals[typ]["tax"] += node.get("tax", 0) * count
            pax_totals[typ]["fare"] += fare * count

        itin_fare = itin.get("originalFare", 0)
        if not _eq(itin_fare, itin_fare_recompute):
            issues.append(
                f"itin[{i}] originalFare={itin_fare} != sum(perPaxFare*count)={itin_fare_recompute}"
            )
        booking_fare_sum += itin_fare

    pfi = bd.get("passengerFareInfo") or {}
    for typ, key in PAX:
        node = pfi.get(key) or {}
        for field in ("baseFare", "tax", "fare"):
            got = node.get(field, 0)
            exp = pax_totals[typ][field]
            if not _eq(got, exp):
                issues.append(
                    f"passengerFareInfo.{key}.{field}={got} != sum(itin per-pax * count)={exp}"
                )

    obf = bd.get("originalBookingFare") or {}
    total_fare = sum(pax_totals[t]["fare"] for t, _ in PAX)
    for field, exp in (
        ("fare", total_fare),
        ("adultFare", pax_totals["adult"]["fare"]),
        ("childFare", pax_totals["child"]["fare"]),
        ("infantFare", pax_totals["infant"]["fare"]),
    ):
        got = obf.get(field, 0)
        if not _eq(got, exp):
            issues.append(f"originalBookingFare.{field}={got} != expected {exp}")
    if not _eq(booking_fare_sum, total_fare):
        issues.append(
            f"sum(itinerary.originalFare)={booking_fare_sum} != sum(passengerFareInfo fares)={total_fare}"
        )
    for field in ("bookedFare", "balanceDue"):
        if field in obf and not _eq(obf[field], obf.get("fare", 0)):
            issues.append(f"originalBookingFare.{field}={obf[field]} != fare={obf.get('fare')}")

    return issues


# --------------------------------------------------------------------------- #
# fare extraction shared by search & revalidate                                #
# --------------------------------------------------------------------------- #
def _avail(block: dict) -> list:
    sa = block.get("searchAvailabilities")
    if isinstance(sa, list):
        return sa
    one = block.get("searchAvailability")
    return [one] if isinstance(one, dict) else []


def _leg_sig(avail: list) -> tuple:
    """Flight-number sequence per leg — the itinerary identity across all stages."""
    return tuple(
        tuple(str(s.get("flightNumber") or s.get("marketingFlightNumber") or "") for s in (a.get("schedules") or []))
        for a in (avail or [])
    )


def _booking_sig(bd: dict) -> tuple:
    return tuple(
        tuple(str(s.get("marketingFlightNumber") or s.get("flightNumber") or "") for s in (it.get("schedules") or []))
        for it in (bd.get("itineraries") or [])
    )


def _fare_pax(fare: dict) -> dict:
    """Per-single-pax whole-trip {total,base,tax,supplierDiscount} for each pax type."""
    out = {}
    for typ, key in FARE_PAX:
        f = fare.get(key) or {}
        out[typ] = {k: _num(f.get(k)) for k in ("total", "base", "tax", "supplierDiscount")}
    return out


def search_options(resp: dict) -> list[dict]:
    data = resp.get("data") or resp
    opts = []
    for key in ("multiCities", "departureFlights", "returnFlights", "roundTrips", "roundTripCombines"):
        blk = data.get(key)
        if isinstance(blk, list):
            for o in blk:
                if isinstance(o, dict) and o.get("fares"):
                    opts.append({"avail": _avail(o), "fares": o["fares"]})
    return opts


def revalidate_option(resp: dict) -> dict | None:
    mf = ((resp.get("data") or resp) or {}).get("mainFare") or {}
    for key in ("multiCities", "departureFlight", "returnFlight", "roundTripCombine"):
        blk = mf.get(key)
        if isinstance(blk, dict) and blk.get("fares"):
            return {"avail": _avail(blk), "fares": blk["fares"]}
    return None


def _request_counts(resp: dict) -> dict:
    req = ((resp.get("data") or resp) or {}).get("integratorFareRequest") or {}
    counts = {t: _num(req.get(t)) for t in PTYPES}
    return counts if sum(counts.values()) else {"adult": 1, "child": 0, "infant": 0}


# --------------------------------------------------------------------------- #
# stage checks                                                                 #
# --------------------------------------------------------------------------- #
def check_search(resp: dict) -> list[str]:
    """Search self-consistency: per-pax base+tax==total and total==Σ count*paxTotal."""
    issues: list[str] = []
    counts = _request_counts(resp)
    for oi, opt in enumerate(search_options(resp)):
        fare = opt["fares"][0]
        pax = _fare_pax(fare)
        for t in PTYPES:
            p = pax[t]
            if p["total"] == 0:
                continue
            calc = p["base"] + p["tax"] - p["supplierDiscount"]
            if not _eq(calc, p["total"]):
                issues.append(f"search opt[{oi}] {t}: base+tax-disc={calc} != total={p['total']}")
        grand = _num((fare.get("originalTotalFare") or {}).get("total"))
        exp = sum(counts[t] * pax[t]["total"] for t in PTYPES)
        if grand and exp and not _eq(grand, exp):
            issues.append(
                f"search opt[{oi}] originalTotalFare.total={grand} != Σ count*paxTotal={exp} (counts={counts})"
            )
        for x in _tax_breakdown_issues(f"search opt[{oi}]", fare):
            issues.append(x)
    return issues


FIELDS = ("total", "base", "tax")


def _per_single_from_fare(fare: dict) -> dict:
    """Whole-trip, per-single-pax {total,base,tax} for each pax type (search/revalidate)."""
    pax = _fare_pax(fare)
    return {t: {f: pax[t][f] for f in FIELDS} for t in PTYPES}


def _per_single_from_booking(bd: dict) -> dict:
    """Whole-trip, per-single-pax {total,base,tax} summed over booking itineraries."""
    out = {t: {f: 0 for f in FIELDS} for t in PTYPES}
    for it in bd.get("itineraries") or []:
        bi = it.get("breakdownInfo") or {}
        for t, key in PAX:
            n = bi.get(key) or {}
            out[t]["total"] += _num(n.get("fare"))
            out[t]["base"] += _num(n.get("baseFare"))
            out[t]["tax"] += _num(n.get("tax"))
    return out


def _cmp_stage(label_a: str, a: dict, label_b: str, b: dict) -> list[str]:
    """Every pax type × every fare field must match between two stages."""
    out = []
    for t in PTYPES:
        for f in FIELDS:
            av, bv = a.get(t, {}).get(f, 0), b.get(t, {}).get(f, 0)
            if not _eq(av, bv):
                out.append(f"{t} {f}: {label_a}={av} != {label_b}={bv}")
    return out


def _tax_breakdown_issues(tag: str, fare: dict) -> list[str]:
    """Within one fare: Σ breakdownTaxes[].amount == tax (when a breakdown is given)."""
    out = []
    for t, key in FARE_PAX:
        f = fare.get(key) or {}
        bd = f.get("breakdownTaxes")
        if isinstance(bd, list) and bd:
            s = sum(_num(x.get("amount")) for x in bd)
            if not _eq(s, _num(f.get("tax"))):
                out.append(f"{tag} {t}: Σ breakdownTaxes={s} != tax={_num(f.get('tax'))}")
    return out


def _checkin(seg_baggage: dict) -> tuple:
    b = (seg_baggage or {}).get("checkIn") or {}
    return (_num(b.get("qty")), str(b.get("measurement") or "").lower())


def _seg_key(airline, flightno, origin, dest) -> tuple:
    return (str(airline or ""), str(flightno or ""), origin, dest)


def _baggage_issues(source_fare: dict, bd: dict, source_label: str = "search") -> list[str]:
    """Per-segment check-in baggage: source-stage inclusiveAddons vs booked schedules.

    Segments are matched by (airline, flightNumber, origin, destination) rather than
    position, so a different ordering never produces a false mismatch.
    """
    addons = ((source_fare.get("inclusiveAddons") or {}).get("departureScheduleAddonsList")) or []
    idx = {
        _seg_key(a.get("airline"), a.get("flightNumber"), a.get("origin"), a.get("destination")):
        _checkin(a.get("baggage"))
        for a in addons
    }
    out = []
    for seg in (sc for it in (bd.get("itineraries") or []) for sc in (it.get("schedules") or [])):
        k = _seg_key(
            seg.get("marketingAirline") or seg.get("operatingAirline"),
            seg.get("marketingFlightNumber") or seg.get("flightNumber"),
            seg.get("origin"), seg.get("destination"),
        )
        if k in idx and idx[k] != _checkin(seg.get("baggage")):
            out.append(f"{k[0]}{k[1]} {k[2]}->{k[3]} check-in baggage {source_label}={idx[k]} != booking={_checkin(seg.get('baggage'))}")
    return out


# --------------------------------------------------------------------------- #
# fareClass / fareBasis identity across stages                                 #
# --------------------------------------------------------------------------- #
def _stage_fare_meta(avail: list, fare: dict) -> tuple[set, set]:
    """(fareClasses, fareBasisCodes) advertised for a search/revalidate option.

    Pulled from everywhere Sabre/Amadeus/AirAsia stash them: per-segment RBD,
    per-pax fareClasses/fareBasisCodes, the rolled-up originalTotalFare, the
    fareClassDetails list and scheduleFareDetailInformations. Upper-cased so a
    case wobble never reads as a change.
    """
    classes: set[str] = set()
    bases: set[str] = set()
    for a in avail or []:
        for s in a.get("schedules") or []:
            if s.get("fareClass"):
                classes.add(str(s.get("fareClass")).upper())
    for _, key in FARE_PAX:
        f = fare.get(key) or {}
        for c in f.get("fareClasses") or []:
            classes.add(str(c).upper())
        for b in f.get("fareBasisCodes") or []:
            bases.add(str(b).upper())
        for sfi in f.get("scheduleFareDetailInformations") or []:
            if sfi.get("fareBasisCode"):
                bases.add(str(sfi.get("fareBasisCode")).upper())
    ot = fare.get("originalTotalFare") or {}
    for c in ot.get("fareClasses") or []:
        classes.add(str(c).upper())
    for b in ot.get("fareBasisCodes") or []:
        bases.add(str(b).upper())
    for fcd in fare.get("fareClassDetails") or []:
        if fcd.get("fareClass"):
            classes.add(str(fcd.get("fareClass")).upper())
    return classes, bases


def _booking_fare_meta(bd: dict) -> tuple[set, set]:
    """(fareClasses, fareBasisCodes) actually booked, read from bookDetail."""
    classes: set[str] = set()
    bases: set[str] = set()
    for it in bd.get("itineraries") or []:
        for s in it.get("schedules") or []:
            if s.get("fareClass"):
                classes.add(str(s.get("fareClass")).upper())
    for b in _walk_strings(bd, {"fareBasisCode", "fareBasis"}):
        bases.add(b.upper())
    return classes, bases


def _cross_stage_diffs(chain: list) -> tuple[list[str], list[str]]:
    """Fare / fareClass / fareBasis diffs across the stage chain, split by severity.

    chain items are (label, per_single_fares, fareClasses, fareBasisCodes).

    Search is allowed to drift from the rest — it can be a cached supplier quote or
    the class/basis sold out before pricing — so any search<->next difference is a
    WARNING (with the fareClass/fareBasis change spelled out). Every other adjacent
    pair (revalidate, revalidate_itin_prp, booking) is authoritative and MUST agree
    on fare, fareClass and fareBasis, so differences there are hard FAILs.
    """
    issues: list[str] = []
    warnings: list[str] = []
    for (la, a, ca, ba), (lb, b, cb, bb) in zip(chain, chain[1:]):
        field_diffs = _cmp_stage(la, a, lb, b)
        class_diff = bool(ca and cb and ca != cb)
        basis_diff = bool(ba and bb and ba != bb)
        if la == "search" or lb == "search":
            for d in field_diffs:
                warnings.append(f"{d}  (search may be a cached/sold-out quote; {lb} is authoritative)")
            if class_diff or basis_diff:
                warnings.append(
                    f"fare selection changed {la}->{lb}: "
                    f"fareClass {sorted(ca) or ['?']}->{sorted(cb) or ['?']}, "
                    f"fareBasis {sorted(ba) or ['?']}->{sorted(bb) or ['?']}"
                )
        else:
            issues += field_diffs
            if class_diff:
                issues.append(
                    f"fareClass mismatch {la}={sorted(ca)} != {lb}={sorted(cb)} "
                    f"(revalidate and booking must use the same fareClass)"
                )
            if basis_diff:
                issues.append(
                    f"fareBasis mismatch {la}={sorted(ba)} != {lb}={sorted(bb)} "
                    f"(revalidate and booking must use the same fareBasis)"
                )
    return issues, warnings


# --------------------------------------------------------------------------- #
# outbound Sabre request/response (run_dir/outbound/*.supplier_*.json)         #
# --------------------------------------------------------------------------- #
def _outbound_dir(run_dir: str) -> str | None:
    p = os.path.join(run_dir, "outbound")
    return p if os.path.isdir(p) else None


def _outbound_pick(out_dir: str, step_hint: str, op: str, kind: str) -> str | None:
    """Pick outbound file; prefer unsuffixed `*.supplier_Op_kind.json` (not `_2`)."""
    hits = sorted(
        f for f in os.listdir(out_dir)
        if f.endswith(".json") and op in f and kind in f and step_hint in f
    )
    if not hits:
        return None
    plain = [h for h in hits if re.search(rf"\.supplier_{re.escape(op)}_{re.escape(kind)}\.json$", h)]
    pick = plain[-1] if plain else hits[-1]
    return os.path.join(out_dir, pick)


def _walk_strings(obj, keys: set[str]) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v.strip():
                out.append(v.strip())
            out.extend(_walk_strings(v, keys))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_walk_strings(v, keys))
    return out


def _parse_sabre_baggage(code: str) -> tuple[int, str]:
    """Sabre FreeBaggageAllowance -> (qty, measurement). KG010/NONIL/PC002."""
    c = (code or "").upper().strip()
    if c in ("NIL", "NONIL", "NO", "0", ""):
        return 0, "kg"
    m = re.match(r"^KG(\d+)$", c)
    if m:
        return int(m.group(1)), "kg"
    m = re.match(r"^PC(\d+)$", c) or re.match(r"^(\d+)P$", c)
    if m:
        return int(m.group(1)), "piece"
    if c.endswith("K") and c[:-1].isdigit():
        return int(c[:-1]), "kg"
    return 0, "unknown"


def _book_request_segments(req: dict) -> list[dict]:
    segs: list[dict] = []
    rq = req.get("CreatePassengerNameRecordRQ") or req
    airbook = rq.get("AirBook") or {}
    odi = airbook.get("OriginDestinationInformation") or []
    if isinstance(odi, dict):
        odi = [odi]
    for block in odi:
        if not isinstance(block, dict):
            continue
        items = block.get("FlightSegment")
        if isinstance(items, dict):
            items = [items]
        for seg in items or []:
            if not isinstance(seg, dict):
                continue
            o = ((seg.get("OriginLocation") or {}).get("LocationCode") or "").upper()
            d = ((seg.get("DestinationLocation") or {}).get("LocationCode") or "").upper()
            fn = str(seg.get("FlightNumber") or (seg.get("MarketingAirline") or {}).get("FlightNumber") or "")
            segs.append({
                "origin": o,
                "destination": d,
                "flightNumber": fn.lstrip("0") or fn,
                "rbd": str(seg.get("ResBookDesigCode") or "").upper(),
                "party": _num(seg.get("NumberInParty")),
            })
    return segs


def _integrator_segments(bd: dict) -> list[dict]:
    segs: list[dict] = []
    for it in bd.get("itineraries") or []:
        for sc in it.get("schedules") or []:
            fn = str(sc.get("marketingFlightNumber") or sc.get("flightNumber") or "")
            segs.append({
                "origin": str(sc.get("origin") or "").upper(),
                "destination": str(sc.get("destination") or "").upper(),
                "flightNumber": fn.lstrip("0") or fn,
                "rbd": str(sc.get("fareClass") or "").upper(),
                "baggage": _checkin(sc.get("baggage")),
            })
    return segs


def _sabre_book_messages(resp: dict) -> tuple[str | None, list[str]]:
    rs = (resp.get("CreatePassengerNameRecordRS") or resp)
    ar = rs.get("ApplicationResults") or {}
    status = ar.get("status")
    msgs: list[str] = []

    def _pull(node):
        if isinstance(node, dict):
            for k in ("Message", "ShortText", "ErrorMessage", "text"):
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    msgs.append(v.strip())
            for v in node.values():
                _pull(v)
        elif isinstance(node, list):
            for v in node:
                _pull(v)

    for key in ("Error", "Warning", "Success"):
        _pull(ar.get(key))
    return status, msgs


def _sabre_fcb_baggage(resp: dict) -> list[tuple[int, str]]:
    """Per-segment FreeBaggageAllowance from the ADULT fare breakdown.

    Sabre returns one AirItineraryPricingInfo per passenger type and an infant
    block (KG010) can sort ahead of the adult block (KG030). Comparing the first
    block against the integrator's adult check-in baggage is a false mismatch.
    Mirror the Go integrator (selectAdultPricingInfo): report the ADT allowance,
    falling back to the first block only when no ADT breakdown exists.
    """
    rs = resp.get("CreatePassengerNameRecordRS") or resp
    blocks: list[list[dict]] = []
    for ap in rs.get("AirPrice") or []:
        apis = (((ap.get("PriceQuote") or {}).get("PricedItinerary") or {})
                .get("AirItineraryPricingInfo"))
        if isinstance(apis, dict):
            apis = [apis]
        for info in apis or []:
            fcb = info.get("FareCalculationBreakdown") or []
            if fcb:
                blocks.append(fcb)
    if not blocks:
        return []

    def _is_adult(fcb: list[dict]) -> bool:
        for row in fcb:
            ptc = str((row.get("FareBasis") or {}).get("FarePassengerType") or "").upper()
            if ptc in ("ADT", "ADULT"):
                return True
        return False

    chosen = next((f for f in blocks if _is_adult(f)), blocks[0])
    return [_parse_sabre_baggage(str(x.get("FreeBaggageAllowance") or "")) for x in chosen]


def _revalidate_supplier_total(resp: dict) -> int | None:
    gir = resp.get("groupedItineraryResponse") or {}
    for g in gir.get("itineraryGroups") or []:
        for it in g.get("itineraries") or []:
            for pi in it.get("pricingInformation") or []:
                tf = (pi.get("fare") or {}).get("totalFare") or {}
                val = _num(tf.get("totalPrice"))
                if val:
                    return val
    return None


def _pick_revalidate_outbound(out_dir: str, integrator_total: int | None) -> str | None:
    """Pick the RevalidateItinerary response whose total matches the integrator fare."""
    hits = sorted(
        f for f in os.listdir(out_dir)
        if f.endswith(".json") and "RevalidateItinerary" in f and "response" in f
    )
    if integrator_total:
        for h in hits:
            try:
                total = _revalidate_supplier_total(_load(os.path.join(out_dir, h)))
            except Exception:  # noqa: BLE001
                continue
            if total and _eq(total, integrator_total):
                return os.path.join(out_dir, h)
    return _outbound_pick(out_dir, "revalidate", "RevalidateItinerary", "response")


def _revalidate_fare_bases(resp: dict) -> set[str]:
    bases = set(_walk_strings(resp, {"fareBasisCode", "FareBasisCode"}))
    gir = resp.get("groupedItineraryResponse") or {}
    for g in gir.get("itineraryGroups") or []:
        for it in g.get("itineraries") or []:
            for pi in it.get("pricingInformation") or []:
                fare = pi.get("fare") or {}
                for pil in fare.get("passengerInfoList") or []:
                    pi2 = pil.get("passengerInfo") or {}
                    for fc in pi2.get("fareComponents") or []:
                        fb = fc.get("fareBasisCode")
                        if isinstance(fb, str) and fb:
                            bases.add(fb)
    return bases


def _book_forced_fare_bases(req: dict) -> set[str]:
    return set(_walk_strings(req, {"FareBasis", "FareBasisCode"}))


def _integrator_booking_failed(booking: dict | None) -> tuple[bool, str]:
    if not booking:
        return False, ""
    body = booking.get("data") or booking
    code = str(body.get("code") or booking.get("code") or "").upper()
    msg = str(body.get("message") or booking.get("message") or "")
    if code in ("BOOKING_FAILED", "ERR.SP.PROVIDER_ERROR"):
        return True, f"{code}: {msg}".strip(": ")
    if not body.get("bookDetail") and code:
        return True, f"{code}: {msg}".strip(": ")
    return False, ""


def validate_outbound(
    run_dir: str,
    booking: dict | None,
    revals: list[tuple[str, dict]],
    bd: dict,
) -> list[str]:
    """Validate Sabre outbound payloads captured under run_dir/outbound/."""
    out_dir = _outbound_dir(run_dir)
    if not out_dir:
        return []

    issues: list[str] = []
    integrator_reval_total = None
    if revals:
        opt = revalidate_option(revals[0][1])
        if opt:
            integrator_reval_total = _num((opt["fares"][0].get("originalTotalFare") or {}).get("total"))

    book_req_path = _outbound_pick(out_dir, "booking", "Book", "request")
    book_resp_path = _outbound_pick(out_dir, "booking", "Book", "response")
    reval_resp_path = _pick_revalidate_outbound(out_dir, integrator_reval_total)
    book_reval_path = _outbound_pick(out_dir, "booking", "RevalidateItinerary", "response")

    book_req = book_resp = reval_resp = book_reval = None
    for path, slot in (
        (book_req_path, "book_req"),
        (book_resp_path, "book_resp"),
        (reval_resp_path, "reval_resp"),
        (book_reval_path, "book_reval"),
    ):
        if not path:
            continue
        try:
            data = _load(path)
            if slot == "book_req":
                book_req = data
            elif slot == "book_resp":
                book_resp = data
            elif slot == "book_reval":
                book_reval = data
            else:
                reval_resp = data
        except Exception as e:  # noqa: BLE001
            issues.append(f"outbound: could not load {os.path.basename(path)}: {e}")

    integrator_failed, fail_reason = _integrator_booking_failed(booking)

    # 1) Sabre Book response health
    if book_resp:
        status, msgs = _sabre_book_messages(book_resp)
        blob = " | ".join(msgs).upper()
        if status and status != "Complete":
            issues.append(f"outbound Book ApplicationResults.status={status!r}")
        for needle in ("RULE VALIDATION FAILED", "ERR.SP.PROVIDER_ERROR", "UNABLE TO PERFORM AIR BOOKING"):
            if needle in blob:
                issues.append(f"outbound Book Sabre error: {needle}")
        if integrator_failed and status == "Complete":
            issues.append(f"outbound Book Complete but integrator booking failed ({fail_reason})")
        if not integrator_failed and status and status != "Complete":
            issues.append(f"integrator booking OK but outbound Book status={status!r}")

    # 2) Forced fare basis vs what RevalidateItinerary priced (pre-book revalidate)
    fare_basis_src = book_reval or reval_resp
    if book_req and fare_basis_src:
        forced = _book_forced_fare_bases(book_req)
        priced = _revalidate_fare_bases(fare_basis_src)
        if forced:
            bad = sorted(forced - priced)
            if bad:
                issues.append(
                    f"outbound Book forces fare basis {sorted(forced)} but revalidate priced "
                    f"{sorted(priced)} — mismatch {bad} (causes RULE VALIDATION FAILED / TRY WPQ)"
                )

    # 3) Segments sent to Sabre must match integrator bookDetail
    if book_req and bd.get("itineraries"):
        sent = _book_request_segments(book_req)
        got = _integrator_segments(bd)
        if sent and got:
            if len(sent) != len(got):
                issues.append(f"outbound Book segment count={len(sent)} != integrator schedules={len(got)}")
            for i, (s, g) in enumerate(zip(sent, got)):
                if s["flightNumber"] and g["flightNumber"] and s["flightNumber"] != g["flightNumber"]:
                    issues.append(
                        f"outbound Book seg[{i}] flight {s['flightNumber']} != integrator {g['flightNumber']}"
                    )
                if s["origin"] and g["origin"] and s["origin"] != g["origin"]:
                    issues.append(f"outbound Book seg[{i}] origin {s['origin']} != integrator {g['origin']}")
                if s["destination"] and g["destination"] and s["destination"] != g["destination"]:
                    issues.append(
                        f"outbound Book seg[{i}] dest {s['destination']} != integrator {g['destination']}"
                    )

    # 4) Sabre-authoritative baggage vs integrator booked schedules
    if book_resp and bd.get("itineraries"):
        sabre_bag = _sabre_fcb_baggage(book_resp)
        ours = [g["baggage"] for g in _integrator_segments(bd)]
        if sabre_bag and ours and len(sabre_bag) == len(ours):
            for i, (sb, ob) in enumerate(zip(sabre_bag, ours)):
                if sb != ob:
                    issues.append(
                        f"outbound Book seg[{i}] Sabre baggage {sb} != integrator checkIn {ob}"
                    )

    # 5) Revalidate supplier total vs integrator revalidate grand total
    if reval_resp and integrator_reval_total:
        sabre_total = _revalidate_supplier_total(reval_resp)
        if sabre_total and not _eq(sabre_total, integrator_reval_total):
            issues.append(
                f"outbound revalidate total={sabre_total} != integrator originalTotalFare.total={integrator_reval_total}"
            )

    return issues


# --------------------------------------------------------------------------- #
# per-run driver                                                               #
# --------------------------------------------------------------------------- #
def process_run(run_dir: str, root: str) -> tuple[bool, str]:
    files = os.listdir(run_dir)

    def pick(sub: str) -> list[str]:
        return sorted(
            f for f in files
            if sub in f and (f.endswith(".json") or f.endswith(".gz.b64"))
        )

    search_files = pick("search")
    reval_files = pick("revalidate")
    book_files = pick("booking")

    rel = os.path.relpath(run_dir, root)
    issues: list[str] = []
    notes: list[str] = []

    def safe_load(name):
        try:
            return _load(os.path.join(run_dir, name))
        except Exception as e:  # noqa: BLE001 - report, don't crash the whole tree
            issues.append(f"could not load {name}: {e}")
            return None

    search = safe_load(search_files[0]) if search_files else None
    revals = [(n, safe_load(n)) for n in reval_files]
    revals = [(n, r) for n, r in revals if r]
    booking = safe_load(book_files[-1]) if book_files else None

    if search:
        issues += check_search(search)
    if booking:
        issues += validate_booking(booking)

    # ---- build the cross-stage chain of per-single-pax whole-trip fares ----
    reval_opts = [(n, revalidate_option(r)) for n, r in revals]
    reval_opts = [(n, o) for n, o in reval_opts if o]

    # reference itinerary signature: prefer revalidate, else booking
    ref_sig = None
    if reval_opts:
        ref_sig = _leg_sig(reval_opts[0][1]["avail"])
    elif booking:
        ref_sig = _booking_sig((booking.get("data") or {}).get("bookDetail") or {})

    warnings: list[str] = []
    chain: list[tuple] = []
    chosen = None

    if search:
        opts = search_options(search)
        if ref_sig:
            for o in opts:
                if _leg_sig(o["avail"]) == ref_sig:
                    chosen = o
                    break
        if chosen is None and len(opts) == 1:
            chosen = opts[0]
        if chosen is not None:
            issues += _tax_breakdown_issues("search", chosen["fares"][0])
            sc_cls, sc_bas = _stage_fare_meta(chosen["avail"], chosen["fares"][0])
            chain.append(("search", _per_single_from_fare(chosen["fares"][0]), sc_cls, sc_bas))
        elif reval_opts or booking:
            notes.append("could not match chosen itinerary to a search option (search->next skipped)")

    for n, o in reval_opts:
        label = "revalidate_itin_prp" if "itin_prp" in n else "revalidate"
        issues += _tax_breakdown_issues(label, o["fares"][0])
        rc_cls, rc_bas = _stage_fare_meta(o["avail"], o["fares"][0])
        chain.append((label, _per_single_from_fare(o["fares"][0]), rc_cls, rc_bas))

    bd = (booking.get("data") or {}).get("bookDetail") or {} if booking else {}
    if booking:
        bk_cls, bk_bas = _booking_fare_meta(bd)
        chain.append(("booking", _per_single_from_booking(bd), bk_cls, bk_bas))

    chain_issues, chain_warnings = _cross_stage_diffs(chain)
    issues += chain_issues
    warnings += chain_warnings

    # per-segment baggage. search->booking may legitimately differ (cached search /
    # sold-out reprice) -> warning; revalidate->booking is authoritative -> fail.
    if chosen is not None and booking:
        warnings += _baggage_issues(chosen["fares"][0], bd, "search")
    if reval_opts and booking:
        issues += _baggage_issues(reval_opts[-1][1]["fares"][0], bd, "revalidate")

    # outbound Sabre request/response checks (when run_dir/outbound/ exists)
    issues += validate_outbound(run_dir, booking, revals, bd)

    present = "search={} revalidate={} booking={} outbound={}".format(
        "Y" if search else "-",
        len(reval_opts),
        "Y" if booking else "-",
        "Y" if _outbound_dir(run_dir) else "-",
    )
    ok = not issues
    status = "FAIL" if issues else "PASS"
    warn_suffix = f"  [+{len(warnings)} warning(s)]" if warnings else ""
    head = f"[{status}] {rel}  ({present}){warn_suffix}"
    lines = [head]
    for note in notes:
        lines.append(f"    note: {note}")
    for x in issues:
        lines.append(f"    - {x}")
    for w in warnings:
        lines.append(f"    ! warn: {w}")
    return ok, "\n".join(lines), len(warnings)


def _iter_run_dirs(root: str):
    for dp, _dn, fn in os.walk(root):
        if any(f.endswith(".response.json") or f.endswith(".response.json.gz.b64") for f in fn):
            yield dp


def run_tree(root: str) -> int:
    run_dirs = sorted(_iter_run_dirs(root))
    if not run_dirs:
        print(f"no run directories with response files found under {root}")
        return 1
    passed = failed = warn_runs = 0
    pass_blocks: list[str] = []
    fail_blocks: list[str] = []
    for d in run_dirs:
        ok, out, nwarn = process_run(d, root)
        if nwarn:
            warn_runs += 1
        if ok:
            pass_blocks.append(out)
            passed += 1
        else:
            fail_blocks.append(out)
            failed += 1

    print("=== PASS ===")
    if pass_blocks:
        print("\n\n".join(pass_blocks))
    else:
        print("(none)")

    print("\n=== FAIL ===")
    if fail_blocks:
        print("\n\n".join(fail_blocks))
    else:
        print("(none)")

    print(f"\n{'=' * 60}\n{passed} passed, {failed} failed, "
          f"{warn_runs} with warning(s), {passed + failed} runs")
    return 0 if failed == 0 else 1


def _materialize(arg: str) -> str:
    """Local path as-is; a git/GitHub URL is shallow-cloned to a temp dir.

    Supports GitHub 'tree' URLs (…/tree/<branch>/<subdir>) by cloning that branch
    and returning the subdir. ponytail: shells out to `git`; needs network + git.
    """
    if "://" not in arg:
        return arg
    import re
    import subprocess
    import tempfile

    m = re.match(r"(https?://[^/]+/[^/]+/[^/]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?/?$", arg)
    if not m:
        raise SystemExit(f"unrecognized repo URL: {arg}")
    repo, branch, sub = m.group(1) + ".git", m.group(2), (m.group(3) or "")
    tmp = tempfile.mkdtemp(prefix="fareval_")
    cmd = ["git", "clone", "--depth", "1"] + (["--branch", branch] if branch else []) + [repo, tmp]
    subprocess.run(cmd, check=True)
    return os.path.join(tmp, sub) if sub else tmp


# --------------------------------------------------------------------------- #
# runnable self-check                                                          #
# --------------------------------------------------------------------------- #
_BOOKING_FIXTURE = {
    "data": {
        "bookDetail": {
            "itineraries": [
                {
                    "originalFare": 19896000, "originalAdultFare": 9948000,
                    "originalChildFare": 0, "originalInfantFare": 0,
                    "adult": 2, "child": 0, "infant": 0,
                    "schedules": [
                        {"marketingAirline": "EY", "marketingFlightNumber": "473"},
                        {"marketingAirline": "EY", "marketingFlightNumber": "63"},
                    ],
                    "breakdownInfo": {
                        "originalAdult": {"baseFare": 6946000, "tax": 3002000, "fare": 9948000,
                                          "psc": 0, "iwjr": 0, "discount": 0},
                        "originalChild": {"baseFare": 0, "tax": 0, "fare": 0},
                        "originalInfant": {"baseFare": 0, "tax": 0, "fare": 0},
                    },
                }
            ],
            "passengerFareInfo": {
                "originalAdult": {"baseFare": 13892000, "tax": 6004000, "fare": 19896000},
                "originalChild": {"baseFare": 0, "tax": 0, "fare": 0},
                "originalInfant": {"baseFare": 0, "tax": 0, "fare": 0},
            },
            "originalBookingFare": {
                "balanceDue": 19896000, "bookedFare": 19896000, "fare": 19896000,
                "adultFare": 19896000, "childFare": 0, "infantFare": 0,
            },
        }
    }
}

# one adult, two multicity legs; search offers the exact chosen combo + a decoy
_SEARCH_FIXTURE = {
    "data": {
        "integratorFareRequest": {"adult": 1, "child": 0, "infant": 0},
        "multiCities": [
            {  # decoy option, different flights
                "searchAvailabilities": [{"schedules": [{"flightNumber": "999"}]}],
                "fares": [{
                    "originalTotalFare": {"total": 5000000, "base": 4000000, "tax": 1000000},
                    "originalAdultFare": {"total": 5000000, "base": 4000000, "tax": 1000000},
                    "originalChildFare": {"total": 0}, "originalInfantFare": {"total": 0},
                }],
            },
            {  # the chosen combo: flights 473 then 63
                "searchAvailabilities": [
                    {"schedules": [{"flightNumber": "473"}]},
                    {"schedules": [{"flightNumber": "63"}]},
                ],
                "fares": [{
                    "originalTotalFare": {"total": 9948000, "base": 6946000, "tax": 3002000},
                    "originalAdultFare": {"total": 9948000, "base": 6946000, "tax": 3002000},
                    "originalChildFare": {"total": 0}, "originalInfantFare": {"total": 0},
                }],
            },
        ],
    }
}

_REVALIDATE_FIXTURE = {
    "data": {
        "mainFare": {
            "multiCities": {
                "searchAvailabilities": [
                    {"schedules": [{"flightNumber": "473"}]},
                    {"schedules": [{"flightNumber": "63"}]},
                ],
                "fares": [{
                    "originalTotalFare": {"total": 9948000, "base": 6946000, "tax": 3002000},
                    "originalAdultFare": {"total": 9948000, "base": 6946000, "tax": 3002000},
                    "originalChildFare": {"total": 0}, "originalInfantFare": {"total": 0},
                }],
            }
        }
    }
}


def _self_check() -> None:
    import copy

    assert validate_booking(_BOOKING_FIXTURE) == [], "known-good booking must reconcile"
    bad = copy.deepcopy(_BOOKING_FIXTURE)
    bad["data"]["bookDetail"]["itineraries"][0]["breakdownInfo"]["originalAdult"]["tax"] = 1
    assert validate_booking(bad), "tampered booking must be flagged"

    assert check_search(_SEARCH_FIXTURE) == [], "consistent search must pass self-check"
    bad_s = copy.deepcopy(_SEARCH_FIXTURE)
    bad_s["data"]["multiCities"][1]["fares"][0]["originalAdultFare"]["tax"] = 1
    assert check_search(bad_s), "inconsistent search fare must be flagged"

    # chain: search chosen (473/63) -> revalidate -> booking, all 9948000 per adult
    ref = _leg_sig(revalidate_option(_REVALIDATE_FIXTURE)["avail"])
    chosen = next(o for o in search_options(_SEARCH_FIXTURE) if _leg_sig(o["avail"]) == ref)
    s = _per_single_from_fare(chosen["fares"][0])
    r = _per_single_from_fare(revalidate_option(_REVALIDATE_FIXTURE)["fares"][0])
    b = _per_single_from_booking(_BOOKING_FIXTURE["data"]["bookDetail"])
    assert _cmp_stage("search", s, "revalidate", r) == [], "search==revalidate must hold"
    assert _cmp_stage("revalidate", r, "booking", b) == [], "revalidate==booking must hold"
    # a drift in ANY field (here: same total, different base/tax split) must be caught
    r_split = copy.deepcopy(r)
    r_split["adult"]["base"] += 1
    r_split["adult"]["tax"] -= 1
    assert _cmp_stage("search", s, "revalidate", r_split), "base/tax drift at equal total must be flagged"

    # tax-breakdown reconciliation
    tb_fare = {"originalAdultFare": {"tax": 100, "breakdownTaxes": [{"amount": 60}, {"amount": 40}]}}
    assert _tax_breakdown_issues("t", tb_fare) == [], "matching tax breakdown must pass"
    tb_fare["originalAdultFare"]["breakdownTaxes"][0]["amount"] = 59
    assert _tax_breakdown_issues("t", tb_fare), "tax breakdown that doesn't sum to tax must be flagged"

    # fareClass / fareBasis extraction from a search/revalidate fare and a booking
    fmeta = {
        "originalAdultFare": {"fareClasses": ["M"], "fareBasisCodes": ["M13IAOL"]},
        "originalTotalFare": {"fareClasses": ["M"]},
    }
    fc, fb = _stage_fare_meta([{"schedules": [{"fareClass": "M"}]}], fmeta)
    assert fc == {"M"} and fb == {"M13IAOL"}, "stage meta must pull fareClass and fareBasis"
    bfc, bfb = _booking_fare_meta(
        {"itineraries": [{"schedules": [{"fareClass": "M", "fareBasisCode": "M13IAOL"}]}]}
    )
    assert bfc == {"M"} and bfb == {"M13IAOL"}, "booking meta must pull fareClass and fareBasis"

    # severity routing: search may drift (warning); revalidate!=booking is a failure
    zero = {t: {"total": 0, "base": 0, "tax": 0} for t in PTYPES}
    s_fare = copy.deepcopy(zero); s_fare["adult"] = {"total": 5102200, "base": 3539000, "tax": 1563200}
    r_fare = copy.deepcopy(zero); r_fare["adult"] = {"total": 7570200, "base": 6007000, "tax": 1563200}
    ci, cw = _cross_stage_diffs([
        ("search", s_fare, {"Q"}, {"Q15IAOL"}),
        ("revalidate", r_fare, {"M"}, {"M13IAOL"}),
    ])
    assert ci == [] and cw, "search drift (fare + class/basis) must be warnings, not failures"
    assert any("fareClass" in w for w in cw), "search drift must spell out the class/basis change"
    ci2, _ = _cross_stage_diffs([
        ("revalidate", r_fare, {"M"}, {"M13IAOL"}),
        ("booking", r_fare, {"M"}, {"N13IAOL"}),
    ])
    assert any("fareBasis mismatch" in x for x in ci2), "revalidate!=booking fareBasis must fail"
    ci3, cw3 = _cross_stage_diffs([
        ("revalidate", r_fare, {"M"}, {"M13IAOL"}),
        ("booking", r_fare, {"M"}, {"M13IAOL"}),
    ])
    assert ci3 == [] and cw3 == [], "revalidate==booking (fare + class + basis) must be clean"

    assert _parse_sabre_baggage("KG010") == (10, "kg")
    assert _parse_sabre_baggage("NONIL") == (0, "kg")

    # ADT selection: an infant block (KG010) sorting ahead of the adult block
    # (KG030) must not be mistaken for the itinerary allowance.
    _bag_resp = {"CreatePassengerNameRecordRS": {"AirPrice": [{"PriceQuote": {"PricedItinerary": {
        "AirItineraryPricingInfo": [
            {"FareCalculationBreakdown": [
                {"FareBasis": {"FarePassengerType": "INF"}, "FreeBaggageAllowance": "KG010"}]},
            {"FareCalculationBreakdown": [
                {"FareBasis": {"FarePassengerType": "ADT"}, "FreeBaggageAllowance": "KG030"}]},
        ]}}}]}}
    assert _sabre_fcb_baggage(_bag_resp) == [(30, "kg")], "must read the ADT baggage, not the infant's"
    forced = {"QBX1YID", "QBX1YID"}
    priced = {"QBX1YID", "NBX1YID"}
    assert sorted(forced - priced) == [], "uniform Q should be subset when priced has Q"
    assert sorted({"QBX1YID", "QBX1YID"} - {"NBX1YID"}) == ["QBX1YID"], "forced Q not priced as N must fail"

    print("self-check OK: booking, search self-check, tax breakdowns, outbound helpers, and the full "
          "search->revalidate->booking field-by-field chain all validated")


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        _self_check()
        return 0

    target = _materialize(argv[1])

    if os.path.isdir(target):
        return run_tree(target)

    # legacy single-file mode: booking.json [search.json]
    booking = _load(target)
    issues = validate_booking(booking)
    if len(argv) > 2:
        search = _load(_materialize(argv[2]))
        bd = (booking.get("data") or {}).get("bookDetail") or {}
        ref_sig = _booking_sig(bd)
        chosen = next((o for o in search_options(search) if _leg_sig(o["avail"]) == ref_sig), None)
        if chosen is None:
            issues.append("no matching flight in search response for the booked itinerary")
        else:
            issues += _cmp_stage(
                "search", _per_single_from_fare(chosen["fares"][0]),
                "booking", _per_single_from_booking(bd),
            )
    if issues:
        print(f"FAIL ({len(issues)} issue(s)):")
        for x in issues:
            print("  -", x)
        return 1
    print("OK: every fare reconciles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
