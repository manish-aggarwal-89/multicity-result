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
   For every adjacent pair present, the per-single-pax whole-trip fare
   (adult / child / infant) must be identical. This is the "the fare I saw in
   search is the fare I get in revalidate and in booking" guarantee.

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
    return issues


def _per_single_from_fare(fare: dict) -> dict:
    pax = _fare_pax(fare)
    return {t: pax[t]["total"] for t in PTYPES}


def _per_single_from_booking(bd: dict) -> dict:
    out = {t: 0 for t in PTYPES}
    for it in bd.get("itineraries") or []:
        for t in PTYPES:
            out[t] += _num(it.get(f"original{t.capitalize()}Fare"))
    return out


def _cmp_stage(label_a: str, a: dict, label_b: str, b: dict) -> list[str]:
    out = []
    for t in PTYPES:
        if not _eq(a.get(t, 0), b.get(t, 0)):
            out.append(f"{t} per-pax fare {label_a}={a.get(t, 0)} != {label_b}={b.get(t, 0)}")
    return out


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

    chain: list[tuple[str, dict]] = []

    if search:
        opts = search_options(search)
        chosen = None
        if ref_sig:
            for o in opts:
                if _leg_sig(o["avail"]) == ref_sig:
                    chosen = o
                    break
        if chosen is None and len(opts) == 1:
            chosen = opts[0]
        if chosen is not None:
            chain.append(("search", _per_single_from_fare(chosen["fares"][0])))
        elif reval_opts or booking:
            notes.append("could not match chosen itinerary to a search option (search->next skipped)")

    for n, o in reval_opts:
        label = "revalidate_itin_prp" if "itin_prp" in n else "revalidate"
        chain.append((label, _per_single_from_fare(o["fares"][0])))

    if booking:
        chain.append(("booking", _per_single_from_booking((booking.get("data") or {}).get("bookDetail") or {})))

    for (la, a), (lb, b) in zip(chain, chain[1:]):
        issues += _cmp_stage(la, a, lb, b)

    present = "search={} revalidate={} booking={}".format(
        "Y" if search else "-", len(reval_opts), "Y" if booking else "-"
    )
    ok = not issues
    head = f"[{'PASS' if ok else 'FAIL'}] {rel}  ({present})"
    lines = [head]
    for note in notes:
        lines.append(f"    note: {note}")
    for x in issues:
        lines.append(f"    - {x}")
    return ok, "\n".join(lines)


def _iter_run_dirs(root: str):
    for dp, _dn, fn in os.walk(root):
        if any(
            (("search" in f) or ("revalidate" in f) or ("booking" in f))
            and (f.endswith(".json") or f.endswith(".gz.b64"))
            for f in fn
        ):
            yield dp


def run_tree(root: str) -> int:
    run_dirs = sorted(_iter_run_dirs(root))
    if not run_dirs:
        print(f"no run directories with response files found under {root}")
        return 1
    passed = failed = 0
    for d in run_dirs:
        ok, out = process_run(d, root)
        print(out)
        passed += ok
        failed += not ok
    print(f"\n{'=' * 60}\n{passed} passed, {failed} failed, {passed + failed} runs")
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
    # a drifted revalidate fare must be caught
    assert _cmp_stage("search", {"adult": 9948000}, "revalidate", {"adult": 9000000}), "fare drift must be flagged"

    print("self-check OK: booking, search self-check, and search->revalidate->booking chain all validated")


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
