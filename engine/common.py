"""Shared paths, constants and small helpers for the deletion-audit agent."""
import csv
import os
import re

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CODE_DIR)
DATASET = os.environ.get("GHOST_DATASET_DIR", os.path.join(REPO_ROOT, "dataset"))

# Operation order from the specification table; also the sort order inside each phase.
OPERATIONS = [
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
]
OP_INDEX = {op: i for i, (_, op, _) in enumerate(OPERATIONS)}
OP_PHASE = {op: phase for phase, op, _ in OPERATIONS}
OP_STATE = {op: state for _, op, state in OPERATIONS}

# Canonical system and category by resource prefix (longest prefix first).
PREFIXES = [
    ("email_delivery_", "email_delivery_service", "EMAIL_DELIVERY_LOG"),
    ("audit_record_", "security_service", "SECURITY_AUDIT_LOG"),
    ("legal_hold_", "legal_service", "LEGAL_HOLD_RECORD"),
    ("data_flow_", "integration_service", "DATA_FLOW"),
    ("tombstone_", "identity_service", "ACCOUNT_TOMBSTONE"),
    ("analytics_", "analytics_platform", "ANALYTICS_PROFILE"),
    ("marketing_", "marketing_platform", "MARKETING_PROFILE"),
    ("account_", "account_service", "USER_PROFILE"),
    ("search_", "search_service", "SEARCH_INDEX_ENTRY"),
    ("support_", "support_platform", "SUPPORT_MESSAGE"),
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
    ("object_", "object_storage", "SUPPORT_ATTACHMENT"),
]
RESOURCE_RE = re.compile(r"\b(?:%s)\d+\b" % "|".join(p for p, _, _ in PREFIXES))


def prefix_info(resource_id):
    for prefix, system_id, category in PREFIXES:
        if resource_id.startswith(prefix):
            return system_id, category
    return None, None


def read_csv(name):
    with open(os.path.join(DATASET, name), encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def case_sort_key(case_id):
    m = re.search(r"(\d+)$", case_id)
    return (case_id.rsplit("_", 1)[0], int(m.group(1)) if m else 0, case_id)


def norm(value):
    """Lowercased, trimmed identifier string, or None."""
    if value is None:
        return None
    value = str(value).strip()
    return value.lower() if value else None
