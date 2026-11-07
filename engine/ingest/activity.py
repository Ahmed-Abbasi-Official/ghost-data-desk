"""Typed view of dataset/system_activity.csv.

Activity rows are claims from operational systems. They are used for linking (data catalog, deletion
batches), for recreation signals (connector/restore repopulation) and for data-flow / restore checks,
never as the only proof that a record is gone.
"""
import re

from common import RESOURCE_RE, prefix_info, read_csv

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
USER_RE = re.compile(r"\buser_\d+\b")


def _kind(row):
    t, d = row["event_type"], row["details"]
    if t == "LINK_RESOURCE_IDENTITY":
        return "link"
    if t == "CLASSIFY_REQUEST":
        return "classify"
    if "repopulated downstream resource" in d:
        return "repopulate"
    if t == "CHECK_DATA_FLOW":
        return "check_flow"
    if t == "CHECK_RESTORE_ACCESS":
        return "check_restore"
    if row["source_system"] == "deletion_worker" or t in (
            "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"):
        return "policy_action"
    return "routine"


def load():
    events = []
    for row in read_csv("system_activity.csv"):
        ev = {
            "activity_id": row["activity_id"],
            "time": row["event_time"],
            "event_type": row["event_type"],
            "source_system": row["source_system"],
            "target_system": row["target_system"],
            "resource_id": row["resource_id"],
            "result": row["reported_result"],
            "details": row["details"],
            "kind": _kind(row),
        }
        ev["system_id"] = prefix_info(row["resource_id"])[0] or row["target_system"]
        ev["mentioned"] = sorted(set(RESOURCE_RE.findall(row["details"])) - {row["resource_id"]})
        ev["emails"] = [e.lower() for e in EMAIL_RE.findall(row["details"])]
        ev["user_ids"] = USER_RE.findall(row["details"])
        if ev["kind"] == "check_flow":
            ev["flow_enabled"] = "remains enabled" in row["details"]
        if ev["kind"] == "check_restore":
            ev["restore_allowed"] = "can still be restored" in row["details"]
        if row["event_type"] == "DELETE_RECORD" and row["resource_id"].startswith("account_"):
            ev["account_delete"] = True
        if "belongs only to" in row["details"]:
            ev["exclusive_owner"] = True
        events.append(ev)
    return events
