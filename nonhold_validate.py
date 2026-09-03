import json, glob, os

RUNS = "/Users/manishaggarwal/PycharmProjects/multicity-result/runs"
DUMMY_POOL = set("EBPZS95LRQ4MJNKADOCTYVXG2I18HU37")


def load(p):
    with open(p) as f:
        return json.load(f)


def seg_chain(itineraries):
    """(origin, destination) per itinerary from its schedules[]."""
    chain = []
    for it in itineraries:
        sch = it.get("schedules") or []
        if not sch:
            chain.append((None, None))
            continue
        chain.append((sch[0].get("origin"), sch[-1].get("destination")))
    return chain


def approx(a, b, tol=1):
    return abs((a or 0) - (b or 0)) <= tol


def validate(run_dir):
    issues, notes = [], []
    case = os.path.basename(os.path.dirname(run_dir))
    sreq = load(os.path.join(run_dir, "01_search.json")).get("request_body") or {}
    routes = sreq.get("routes") or []
    exp_chain = [(r.get("origin"), r.get("destination")) for r in routes]
    exp_a, exp_c, exp_i = sreq.get("adult"), sreq.get("child"), sreq.get("infant")

    resp = load(os.path.join(run_dir, "03_nonhold_booking.response.json"))
    chosen = resp.get("chosen") or {}
    status = chosen.get("status")
    b = chosen.get("response_body") or {}

    if status != 200:
        issues.append(f"HTTP {status}")
    if b.get("code") != "SUCCESS":
        issues.append(f"code={b.get('code')}")
    if b.get("message") != "SUCCESS":
        notes.append(f"message={b.get('message')}")

    data = b.get("data") or {}
    bd = data.get("bookDetail") or {}
    code = bd.get("bookingCode")

    # 1) dummy booking code shape: 6-char uppercase alphanumeric (deployed build
    #    uses a broader pool than the local source snippet; "dummy" is proven
    #    separately by integratorBookingResponse/issuedResponse being null in Mongo).
    if not code:
        issues.append("no bookingCode")
    else:
        if len(code) != 6:
            issues.append(f"bookingCode len={len(code)} (expected 6)")
        if not code.isalnum() or code.upper() != code:
            issues.append(f"bookingCode '{code}' not 6-char uppercase alphanumeric")

    # 2) distribution + currency
    if bd.get("distributionType") != "sabre":
        issues.append(f"distributionType={bd.get('distributionType')}")
    if not (bd.get("account") or {}).get("code"):
        issues.append("no account.code")
    if bd.get("originalCurrency") != "IDR":
        issues.append(f"currency={bd.get('originalCurrency')}")

    # 3) fare arithmetic
    fare = bd.get("originalBookingFare") or {}
    total = fare.get("fare")
    af, cf, inf = fare.get("adultFare"), fare.get("childFare"), fare.get("infantFare")
    if not total or total <= 0:
        issues.append(f"total fare={total}")
    if not approx(fare.get("bookedFare"), total):
        issues.append(f"bookedFare {fare.get('bookedFare')} != fare {total}")
    if not approx(fare.get("balanceDue"), total):
        issues.append(f"balanceDue {fare.get('balanceDue')} != fare {total}")
    if not approx((af or 0) + (cf or 0) + (inf or 0), total):
        issues.append(f"adult+child+infant {(af or 0)+(cf or 0)+(inf or 0)} != total {total}")
    if not af or af <= 0:
        issues.append(f"adultFare={af}")
    if exp_c and not (cf and cf > 0):
        issues.append(f"child requested but childFare={cf}")
    if not exp_c and cf:
        notes.append(f"childFare={cf} but no child requested")
    if exp_i and not (inf and inf > 0):
        issues.append(f"infant requested but infantFare={inf}")

    # 4) per-pax fare consistency (base + tax == fare)
    pfi = bd.get("passengerFareInfo") or {}
    for lbl, key, want in (("adult", "originalAdult", True),
                            ("child", "originalChild", bool(exp_c)),
                            ("infant", "originalInfant", bool(exp_i))):
        pax = pfi.get(key) or {}
        pf = pax.get("fare")
        if want:
            if not pf or pf <= 0:
                issues.append(f"{lbl} per-pax fare={pf}")
            elif not approx((pax.get("baseFare") or 0) + (pax.get("tax") or 0), pf, tol=5):
                issues.append(f"{lbl} base+tax {(pax.get('baseFare') or 0)+(pax.get('tax') or 0)} != fare {pf}")

    # per-type fare in originalBookingFare must equal passengerFareInfo per-type total
    if not approx(af, (pfi.get('originalAdult') or {}).get('fare', 0), tol=5):
        issues.append(f"adultFare {af} != passengerFareInfo.adult {(pfi.get('originalAdult') or {}).get('fare')}")

    # 5) itineraries: count + per-itin pax + cabin + chain
    itins = bd.get("itineraries") or []
    if len(itins) != len(exp_chain):
        issues.append(f"itineraries={len(itins)} != segments={len(exp_chain)}")
    for idx, it in enumerate(itins):
        if (it.get("adult"), it.get("child"), it.get("infant")) != (exp_a, exp_c, exp_i):
            issues.append(f"itin[{idx}] pax {(it.get('adult'),it.get('child'),it.get('infant'))} != {(exp_a,exp_c,exp_i)}")
        if it.get("cabinClass") != "ECONOMY":
            issues.append(f"itin[{idx}] cabin={it.get('cabinClass')}")
    chain = seg_chain(itins)
    if chain != exp_chain:
        issues.append(f"segment chain {chain} != requested {exp_chain}")

    # 6) integratorBookRequest echo
    ibr = data.get("integratorBookRequest") or {}
    if (ibr.get("adult"), ibr.get("child"), ibr.get("infant")) != (exp_a, exp_c, exp_i):
        issues.append(f"IBR pax {(ibr.get('adult'),ibr.get('child'),ibr.get('infant'))} != {(exp_a,exp_c,exp_i)}")

    return {
        "case": case, "bookingCode": code, "status": status,
        "segments": len(exp_chain), "pax": f"{exp_a}A{exp_c}C{exp_i}I",
        "total_fare": total, "chain": "->".join([exp_chain[0][0]] + [d for _, d in exp_chain]) if exp_chain else "",
        "verdict": "VALID" if not issues else "INVALID",
        "issues": issues, "notes": notes,
    }


results = []
for run_dir in sorted(glob.glob(os.path.join(RUNS, "NONHOLD-*", "*"))):
    if os.path.isdir(run_dir) and os.path.exists(os.path.join(run_dir, "03_nonhold_booking.response.json")):
        results.append(validate(run_dir))

print(f"{'CASE':<22}{'CODE':<8}{'PAX':<9}{'SEG':<4}{'CHAIN':<26}{'TOTAL(IDR)':<14}VERDICT")
for r in results:
    print(f"{r['case']:<22}{r['bookingCode'] or '-':<8}{r['pax']:<9}{r['segments']:<4}{r['chain']:<26}{str(r['total_fare']):<14}{r['verdict']}")
    for i in r["issues"]:
        print(f"    ! {i}")
    for n in r["notes"]:
        print(f"    ~ {n}")

bad = [r for r in results if r["verdict"] != "VALID"]
print(f"\n{len(results)} cases, {len(results)-len(bad)} VALID, {len(bad)} INVALID")
with open("/tmp/nh_validation_report.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
