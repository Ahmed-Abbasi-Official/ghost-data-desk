"""Phase 7 checks: output schema, ordering, allowed operations and evidence rules."""
import json

from common import OP_INDEX, OP_PHASE, OP_STATE

STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def check_row(case, status, steps, manifest, activity_times):
    """Return a list of problems for one output row (empty when valid)."""
    problems = []
    if status not in STATUSES:
        problems.append("bad status %s" % status)
    if status == "NOT_APPLICABLE" and steps:
        problems.append("NOT_APPLICABLE must have []")
    if status == "COMPLETE" and any(s["phase"] != "VERIFICATION" for s in steps):
        problems.append("COMPLETE must only verify")
    if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
        problems.append("INCOMPLETE without findings")
    last = None
    for i, s in enumerate(steps, 1):
        if set(s) != FIELDS:
            problems.append("step %d fields %s" % (i, sorted(s)))
            continue
        if s["step_no"] != i:
            problems.append("step_no %s at position %d" % (s["step_no"], i))
        op = s["operation"]
        if op not in OP_INDEX or OP_PHASE[op] != s["phase"] or OP_STATE[op] != s["state"]:
            problems.append("bad operation/phase/state %s/%s/%s" % (s["phase"], op, s["state"]))
            continue
        key = (OP_INDEX[op], s["system_id"], s["resource_id"])
        if last is not None and key <= last:
            problems.append("order at step %d" % i)
        last = key
        ev = s["evidence_id"]
        needs = s["phase"] == "FINDING" or status == "COMPLETE"
        if needs and not ev:
            problems.append("step %d missing evidence" % i)
        if not needs and ev:
            problems.append("step %d future step must not cite evidence" % i)
        if ev:
            when = manifest[ev]["captured_at"] if ev in manifest else activity_times.get(ev)
            if when is None:
                problems.append("unknown evidence %s" % ev)
            elif when > case["audit_at"]:
                problems.append("evidence %s after audit_at" % ev)
    return problems


def check_file(rows, request_ids):
    problems = []
    ids = [r["case_id"] for r in rows]
    if ids != request_ids:
        problems.append("case ids/order differ from requests")
    for r in rows:
        try:
            json.loads(r["steps_json"])
        except ValueError:
            problems.append("invalid JSON for %s" % r["case_id"])
    return problems
