"""Phases 6-7: turn per-resource decisions into the ordered steps_json and the case status."""
import json

from common import OP_INDEX, OP_PHASE, OP_STATE

RECORD_OPS = {  # required state -> (finding, remediation, verification)
    "ABSENT": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACTED": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZED": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "PRESENT": (None, None, "VERIFY_RECORD_PRESENT"),
}
FLOW_OPS = ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")
BACKUP_OPS = ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")


def _ops(d):
    if d["kind"] == "flow":
        return FLOW_OPS
    if d["kind"] == "backup":
        return BACKUP_OPS
    return RECORD_OPS[d["required"]]


def plan(decisions):
    """Return (status, steps) for a deletion-authorised case."""
    incomplete = any(d["violation"] for d in decisions)
    raw = []
    for d in decisions:
        finding, remediation, verification = _ops(d)
        if incomplete:
            if d["violation"]:
                raw.append((finding, d, d["evidence"]))
                raw.append((remediation, d, ""))
            raw.append((verification, d, ""))
        else:
            raw.append((verification, d, d["evidence"]))
    raw.sort(key=lambda r: (OP_INDEX[r[0]], r[1]["system_id"], r[1]["resource_id"]))
    steps = []
    for i, (op, d, evidence) in enumerate(raw, 1):
        steps.append({"step_no": i, "phase": OP_PHASE[op], "system_id": d["system_id"],
                      "resource_id": d["resource_id"], "operation": op, "state": OP_STATE[op],
                      "evidence_id": evidence or ""})
    return ("INCOMPLETE" if incomplete else "COMPLETE"), steps


def dumps(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
