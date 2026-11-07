"""Parser for miscellaneous JSON evidence files (see SCHEMA.md).

Handles two distinct shapes found in dataset/evidence/records/:

1. Lookup ledger (connector_configurations_*, customer_export_manifest_*,
   security_audit_ledger_*):
       {"exported_at", "origin", "record_count",
        "records": [{"recorded_at", "system", "resource_id", "record_type",
                     "result", "subject_reference"}, ...]}
   A ledger is a list of per-resource lookup results spanning many systems; it is
   NOT a full table snapshot, so no coverage dicts are emitted for it.

2. Collection snapshot (customer_export_jobs_*, customer_analytics_report_*):
       {"captured_at", "source", "collection", "records": [ {...}, ... ]}
   Each record is one stored row of that collection; one coverage dict is emitted
   per collection (even when empty).

Nothing is invented: identifier values are copied exactly as they appear;
null / empty strings become None.
"""

import json
import re

PREFIX_TABLE = [
    # longer prefixes first so "email_delivery_" wins over "email_" etc.
    ("email_delivery_", "email_delivery_service", "EMAIL_DELIVERY_LOG"),
    ("audit_record_", "security_service", "SECURITY_AUDIT_LOG"),
    ("legal_hold_", "legal_service", "LEGAL_HOLD_RECORD"),
    ("tombstone_", "identity_service", "ACCOUNT_TOMBSTONE"),
    ("data_flow_", "integration_service", "DATA_FLOW"),
    ("analytics_", "analytics_platform", "ANALYTICS_PROFILE"),
    ("marketing_", "marketing_platform", "MARKETING_PROFILE"),
    ("account_", "account_service", "USER_PROFILE"),
    ("search_", "search_service", "SEARCH_INDEX_ENTRY"),
    ("support_", "support_platform", None),  # decided from content
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
    ("object_", "object_storage", None),  # decided from content
]

KNOWN_CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE", "DATA_FLOW",
}

# Status words that mean the record does not exist at observed_at.
ABSENT_STATUSES = {"NOT_FOUND", "DELETED", "PURGED", "REMOVED", "MISSING", "ABSENT", "404"}

USER_ID_RE = re.compile(r"^user_\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")


def _prefix_info(resource_id):
    rid = (resource_id or "").lower()
    for prefix, system_id, category in PREFIX_TABLE:
        if rid.startswith(prefix):
            return system_id, category
    return None, None


def _norm_status(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or None


def _clean(value):
    """Copy a scalar identifier exactly; null / empty / whitespace -> None."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _empty_identifiers():
    return {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}


def _classify_reference(ref, identifiers):
    """Place a subject_reference / customer_reference into the right identifier slot.

    Returns True when the value was placed, False when it fits no known slot.
    """
    ref = _clean(ref)
    if ref is None:
        return True
    if not isinstance(ref, str):
        return False
    s = ref.strip()
    if USER_ID_RE.match(s):
        identifiers["user_id"] = ref
        return True
    if EMAIL_RE.match(s):
        identifiers["email"] = ref
        return True
    if PHONE_RE.match(s) and sum(c.isdigit() for c in s) >= 7:
        identifiers["phone"] = ref
        return True
    return False


def _category_for(resource_id, stated_type, content_text=""):
    _, prefix_cat = _prefix_info(resource_id)
    stated = _norm_status(stated_type)
    rid = (resource_id or "").lower()
    if rid.startswith("support_"):
        if stated in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"):
            return stated
        low = content_text.lower()
        if "attachment" in low or "file_name" in low:
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE"
    if rid.startswith("analytics_"):
        if stated == "PRODUCT_TELEMETRY" or "telemetry" in content_text.lower():
            return "PRODUCT_TELEMETRY"
        return "ANALYTICS_PROFILE"
    if prefix_cat is not None:
        return prefix_cat
    if stated in KNOWN_CATEGORIES:
        return stated
    return None


# --------------------------------------------------------------------------
# Shape 1: lookup ledger
# --------------------------------------------------------------------------

def _parse_ledger(data, file_id, captured_at):
    out = []
    exported_at = data.get("exported_at") or captured_at
    origin = data.get("origin")
    for i, rec in enumerate(data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        resource_id = rec.get("resource_id")
        system_id, _ = _prefix_info(resource_id)
        stated_system = rec.get("system")
        if system_id is None:
            system_id = stated_system
        status = _norm_status(rec.get("result"))
        present = status not in ABSENT_STATUSES if status is not None else None

        identifiers = _empty_identifiers()
        raw_ref = rec.get("subject_reference")
        attrs = {
            "source_shape": "lookup_ledger",
            "origin": origin,
            "file_exported_at": data.get("exported_at"),
            "record_type": rec.get("record_type"),
            "result": rec.get("result"),
            "subject_reference": _clean(raw_ref),
        }
        if not _classify_reference(raw_ref, identifiers):
            attrs["unclassified_subject_reference"] = raw_ref
        if stated_system is not None and stated_system != system_id:
            attrs["stated_system"] = stated_system
        recorded_at = rec.get("recorded_at")
        if recorded_at and data.get("exported_at") and str(recorded_at) > str(data.get("exported_at")):
            # The row's own timestamp is later than the ledger export time: a timing
            # fact that downstream conflict handling may need. Compared as ISO strings.
            attrs["recorded_after_file_export"] = True
        for key, value in rec.items():
            if key not in ("recorded_at", "system", "resource_id", "record_type",
                           "result", "subject_reference") and key not in attrs:
                attrs[key] = value

        out.append({
            "kind": "record",
            "file_id": file_id,
            "observed_at": recorded_at or exported_at,
            "system_id": system_id,
            "resource_id": resource_id,
            "category": _category_for(resource_id, rec.get("record_type")),
            "present": present,
            "status": status,
            "identifiers": identifiers,
            "lookup_key": None,
            "attrs": attrs,
            "location": "records[%d]" % i,
        })
    return out


# --------------------------------------------------------------------------
# Shape 2: collection snapshot
# --------------------------------------------------------------------------

_IDENTIFIER_FIELDS = {
    "user_id": "user_id",
    "customer_reference": "user_id",
    "subject_reference": "user_id",
    "subject_id": "user_id",
    "email": "email",
    "owner_email": "email",
    "customer_email": "email",
    "phone": "phone",
    "phone_number": "phone",
    "name": "name",
    "full_name": "name",
    "customer_name": "name",
    "email_hash": "email_hash",
}

_STATUS_FIELDS = ("status", "result", "state", "record_status")


def _parse_collection(data, file_id, captured_at):
    out = []
    snapshot_at = data.get("captured_at") or captured_at
    collection = data.get("collection")
    source = data.get("source")
    records = data.get("records") or []
    resource_ids = []
    coverage_system = None

    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        resource_id = rec.get("resource_id")
        system_id, _ = _prefix_info(resource_id)
        if coverage_system is None:
            coverage_system = system_id
        if resource_id is not None:
            resource_ids.append(resource_id)

        identifiers = _empty_identifiers()
        attrs = {
            "source_shape": "collection_snapshot",
            "source": source,
            "table": collection,
            "replica_role": "primary",
        }
        status_raw = None
        for key, value in rec.items():
            if key == "resource_id":
                continue
            slot = _IDENTIFIER_FIELDS.get(key)
            if slot is not None:
                if slot == "user_id" and key != "user_id":
                    # a subject/customer reference: route by its actual shape
                    if not _classify_reference(value, identifiers):
                        attrs[key] = value
                    elif _clean(value) is not None:
                        attrs[key] = value
                elif identifiers[slot] is None:
                    identifiers[slot] = _clean(value)
                    if key != slot:
                        attrs[key] = value
                else:
                    attrs[key] = value
                continue
            if key in _STATUS_FIELDS and status_raw is None:
                status_raw = value
            attrs[key] = value

        # Derived, file-internal facts (comparisons only, no new values).
        expires_at = rec.get("expires_at")
        if isinstance(expires_at, str) and isinstance(snapshot_at, str):
            attrs["expired_at_observed"] = expires_at <= snapshot_at
        if all(v is None for v in identifiers.values()):
            attrs["all_identifiers_null"] = True

        status = _norm_status(status_raw)
        present = True
        if status in ABSENT_STATUSES or rec.get("deleted") is True:
            present = False

        content_text = json.dumps(rec, sort_keys=True)
        out.append({
            "kind": "record",
            "file_id": file_id,
            "observed_at": snapshot_at,
            "system_id": system_id,
            "resource_id": resource_id,
            "category": _category_for(resource_id, rec.get("record_type"), content_text),
            "present": present,
            "status": status,
            "identifiers": identifiers,
            "lookup_key": None,
            "attrs": attrs,
            "location": "%s.records[%d]" % (collection or "records", i),
        })

    if coverage_system is None:
        coverage_system = _system_for_collection(collection, source)
    out.append({
        "kind": "coverage",
        "file_id": file_id,
        "observed_at": snapshot_at,
        "system_id": coverage_system,
        "table": collection,
        "replica_role": "primary",
        "resource_ids": resource_ids,
    })
    return out


def _system_for_collection(collection, source):
    """System for an empty collection, from its stated name only."""
    text = "%s %s" % (collection or "", source or "")
    if "export" in text:
        return "export_service"
    if "analytics" in text:
        return "analytics_platform"
    return None


# --------------------------------------------------------------------------

def parse(path, file_id, captured_at):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return []
    records = data.get("records")
    if not isinstance(records, list):
        return []
    if "collection" in data:
        return _parse_collection(data, file_id, captured_at)
    return _parse_ledger(data, file_id, captured_at)
