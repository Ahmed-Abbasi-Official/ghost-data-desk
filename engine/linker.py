"""Global account linker: which account_* resource is the primary profile of each deletion case.

Many deleted profiles keep no identifier, so the profile is often only visible as a deletion-worker
`DELETE_RECORD account_*` event. Signals, strongest first:
  DIRECT       evidence (<= audit_at) or a data-catalog link ties the account to the user's identifiers
  BATCH        delete event inside the case's reporting window and its result matches reported_status
               (SUCCESS->COMPLETE, PARTIAL->PARTIALLY_COMPLETE, FAILED->NOT_STARTED)
  ELIMINATION  candidates already claimed by other cases are removed until a fixpoint
Nothing is guessed. Unresolved cases get a review reason instead of an account:
  ACCOUNT_NOT_FOUND       no supporting account evidence
  AMBIGUOUS_ACCOUNT_LINK  several accounts remain equally plausible
  CONFLICTING_EVIDENCE    only candidates whose result, status or timing disagree with the case
"""
from datetime import datetime, timedelta

RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
WINDOW_BEFORE = timedelta(hours=1, minutes=30)
WINDOW_AFTER = timedelta(minutes=30)
CLEARLY_CLOSEST = 15 * 60  # seconds


def _t(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def account_owners(observations, events, identities, cutoff=None):
    """account_id -> {user_id: [evidence ids]} from evidence records and catalog links (optionally <= cutoff)."""
    owners = {}
    for ob in observations:
        rid = ob.get("resource_id") or ""
        if ob.get("kind") != "record" or not rid.startswith("account_"):
            continue
        if cutoff and ob["observed_at"] > cutoff:
            continue
        values = list((ob.get("identifiers") or {}).values()) + [ob.get("lookup_key")]
        for v in values:
            uid = identities.owner_of(v)
            if uid:
                owners.setdefault(rid, {}).setdefault(uid, []).append(ob["file_id"])
    for ev in events:
        if ev["kind"] != "link" or not ev["resource_id"].startswith("account_"):
            continue
        if cutoff and ev["time"] > cutoff:
            continue
        for v in ev["emails"] + ev["user_ids"]:
            uid = identities.owner_of(v)
            if uid:
                owners.setdefault(ev["resource_id"], {}).setdefault(uid, []).append(ev["activity_id"])
    return owners


def link_accounts(cases, observations, events, identities):
    """cases: list of dicts with case_id, uid, requested_at, reported_at, reported_status, audit_at.

    Returns {case_id: {"accounts": [...], "method", "reason", "candidates", "evidence_ids"}}.
    """
    deletes = [e for e in events if e.get("account_delete")]
    global_owners = account_owners(observations, events, identities)
    out, candidates = {}, {}

    for c in cases:
        own = account_owners(observations, events, identities, cutoff=c["audit_at"])
        direct = sorted(a for a, users in own.items() if c["uid"] in users)
        lo = max(_t(c["requested_at"]), _t(c["reported_at"]) - WINDOW_BEFORE)
        hi = _t(c["reported_at"]) + WINDOW_AFTER
        window = [d for d in deletes if lo <= _t(d["time"]) <= hi and d["time"] <= c["audit_at"]]
        if direct:
            in_window = [a for a in direct if any(d["resource_id"] == a for d in window)]
            chosen = in_window or direct
            ev_ids = sorted({i for a in chosen for i in own[a][c["uid"]]})
            out[c["case_id"]] = {"accounts": chosen, "method": "DIRECT", "reason": None,
                                 "candidates": direct, "evidence_ids": ev_ids}
            continue
        free = [d for d in window if not (set(global_owners.get(d["resource_id"], {})) - {c["uid"]})]
        want = RESULT_FOR_STATUS.get(c["reported_status"])
        matching = [d for d in free if d["result"] == want]
        candidates[c["case_id"]] = {
            "matching": matching, "conflicting": [d for d in free if d["result"] != want],
            "gap": {d["activity_id"]: abs((_t(c["reported_at"]) - _t(d["time"])).total_seconds()) for d in free},
        }

    changed = True
    while changed:
        changed = False
        taken = {a for r in out.values() for a in r["accounts"]}
        for cid in sorted(candidates):
            if cid in out:
                continue
            info = candidates[cid]
            left = sorted({d["resource_id"]: d for d in info["matching"] if d["resource_id"] not in taken}.values(),
                          key=lambda d: (info["gap"][d["activity_id"]], d["resource_id"]))
            pick = None
            if len(left) == 1:
                pick = left[0]
            elif len(left) > 1 and info["gap"][left[0]["activity_id"]] < CLEARLY_CLOSEST <= info["gap"][left[1]["activity_id"]]:
                pick = left[0]
            if pick:
                out[cid] = {"accounts": [pick["resource_id"]], "method": "BATCH", "reason": None,
                            "candidates": [d["resource_id"] for d in left], "evidence_ids": [pick["activity_id"]]}
                taken.add(pick["resource_id"])
                changed = True

    # A single unclaimed delete event within minutes of the report is a reliable link even when the summary status
    # differs (the status also reflects the case's other records).
    taken = {a for r in out.values() for a in r["accounts"]}
    for cid in sorted(candidates):
        if cid in out:
            continue
        info = candidates[cid]
        near = sorted({d["resource_id"]: d for d in info["matching"] + info["conflicting"]
                       if d["resource_id"] not in taken and info["gap"][d["activity_id"]] < CLEARLY_CLOSEST}.values(),
                      key=lambda d: d["resource_id"])
        if len(near) == 1:
            out[cid] = {"accounts": [near[0]["resource_id"]], "method": "TIME", "reason": None,
                        "candidates": [near[0]["resource_id"]], "evidence_ids": [near[0]["activity_id"]]}
            taken.add(near[0]["resource_id"])

    for cid, info in candidates.items():
        if cid in out:
            continue
        left = sorted({d["resource_id"] for d in info["matching"] if d["resource_id"] not in taken})
        conflicting = sorted({d["resource_id"] for d in info["conflicting"] if d["resource_id"] not in taken})
        if left:
            reason, cands = "AMBIGUOUS_ACCOUNT_LINK", left
        elif conflicting:
            reason, cands = "CONFLICTING_EVIDENCE", conflicting
        else:
            reason, cands = "ACCOUNT_NOT_FOUND", []
        ev_ids = sorted({d["activity_id"] for d in info["matching"] + info["conflicting"] if d["resource_id"] in cands})
        out[cid] = {"accounts": [], "method": None, "reason": reason, "candidates": cands, "evidence_ids": ev_ids}
    return out


REVIEW_HINT = {
    "ACCOUNT_NOT_FOUND": "Confirm which account_* profile belonged to the requester; no participant-visible record links one.",
    "AMBIGUOUS_ACCOUNT_LINK": "Confirm which candidate profile belonged to the requester; the deletion batch holds several equally plausible ones.",
    "CONFLICTING_EVIDENCE": "Check the candidate profile: its deletion result or timing disagrees with the reported workflow status.",
}
