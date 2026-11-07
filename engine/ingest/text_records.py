"""Parser for plain-text record handovers (see SCHEMA.md).

Handles dataset/evidence/records/*.txt files shaped like the customer-platform
shift handover:

    CUSTOMER PLATFORM - SHIFT HANDOVER
    Snapshot: 2026-10-01T12:00:00Z

    Account and downstream checks completed during the shift:

    - <ts> | <system> | <resource_id> | <CATEGORY> | <STATUS> | <reference>

`<reference>` is the subject value the check was tied to: an email, a stable
`user_NN` reference, or the literal text "no direct identifier".

Interpretation (nothing is invented; every value is copied verbatim):
* Present rows (FOUND / IDENTIFIABLE / ACTIVE / ...) put the reference in
  `identifiers` (the row says the stored record is tied to it).
* Absent rows (NOT_FOUND / DELETED / ...) describe a record that does not exist,
  so they expose nothing: identifiers stay None and the reference becomes the
  `lookup_key` of that check.
* "no direct identifier" -> identifiers None, attrs["identifier_note"].
* The handover is a list of selected checks, not a full table / store snapshot,
  so no coverage dicts are emitted (absence-by-omission would be wrong here).
"""

import re

PREFIX_TABLE = [
    ("email_delivery_", "email_delivery_service", "EMAIL_DELIVERY_LOG"),
    ("audit_record_", "security_service", "SECURITY_AUDIT_LOG"),
    ("legal_hold_", "legal_service", "LEGAL_HOLD_RECORD"),
    ("tombstone_", "identity_service", "ACCOUNT_TOMBSTONE"),
    ("data_flow_", "integration_service", "DATA_FLOW"),
    ("analytics_", "analytics_platform", "ANALYTICS_PROFILE"),
    ("marketing_", "marketing_platform", "MARKETING_PROFILE"),
    ("account_", "account_service", "USER_PROFILE"),
    ("search_", "search_service", "SEARCH_INDEX_ENTRY"),
    ("support_", "support_platform", None),  # decided from content / stated category
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
    ("object_", "object_storage", None),  # decided from content / stated category
]

KNOWN_CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE", "DATA_FLOW",
}

ABSENT_STATUSES = {"NOT_FOUND", "DELETED", "PURGED", "REMOVED", "MISSING", "ABSENT", "404"}

ROW_RE = re.compile(
    r"^\s*[-*]\s*(?P<ts>[^|]+?)\s*\|\s*(?P<system>[^|]*?)\s*\|\s*(?P<rid>[^|]+?)\s*\|"
    r"\s*(?P<category>[^|]*?)\s*\|\s*(?P<status>[^|]*?)\s*\|\s*(?P<ref>[^|]*?)\s*$"
)
SNAPSHOT_RE = re.compile(r"^\s*Snapshot\s*:\s*(?P<ts>\S+)\s*$", re.I)
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")
USER_ID_RE = re.compile(r"^user_\d+$", re.I)
EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")
PHONE_RE = re.compile(r"^\+?\d[\d\s().-]{6,}\d$")
EMAIL_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
NO_IDENTIFIER_RE = re.compile(r"^(no direct identifier|none|null|n/?a|-|redacted|\[redacted\])$", re.I)


def _prefix_info(resource_id):
    rid = (resource_id or "").lower()
    for prefix, system_id, category in PREFIX_TABLE:
        if rid.startswith(prefix):
            return system_id, category
    return None, None


def _norm_status(value):
    text = (value or "").strip()
    if not text:
        return None
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or None


def _empty_identifiers():
    return {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}


def _classify_reference(ref):
    """Return (identifier_field, value) for a reference cell, or (None, None)."""
    value = (ref or "").strip()
    if not value or NO_IDENTIFIER_RE.match(value):
        return None, None
    if EMAIL_RE.match(value):
        return "email", value
    if USER_ID_RE.match(value):
        return "user_id", value
    if EMAIL_HASH_RE.match(value):
        return "email_hash", value
    if PHONE_RE.match(value):
        return "phone", value
    return None, None


def _state_attrs(category, status):
    """State facts that the status word itself states for flows and backups."""
    attrs = {}
    if category == "DATA_FLOW":
        if status in ("ACTIVE", "ENABLED", "RUNNING"):
            attrs["enabled"] = True
        elif status in ("DISABLED", "PAUSED", "STOPPED", "INACTIVE"):
            attrs["enabled"] = False
    elif category == "BACKUP_ARCHIVE":
        # Problem vocabulary: a backup restore path is ACTIVE (risk) or BLOCKED.
        if status == "BLOCKED":
            attrs["restore_allowed"] = False
        elif status == "ACTIVE":
            attrs["restore_allowed"] = True
    return attrs


def parse(path, file_id, captured_at):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    snapshot_at = None
    title = None
    out = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if title is None:
            title = line.strip()
        m = SNAPSHOT_RE.match(line)
        if m:
            snapshot_at = m.group("ts")
            continue
        m = ROW_RE.match(line)
        if not m:
            continue

        rid = m.group("rid").strip()
        stated_system = m.group("system").strip() or None
        stated_category = m.group("category").strip().upper() or None
        status = _norm_status(m.group("status"))
        ref_raw = m.group("ref").strip()
        ts = m.group("ts").strip()
        observed_at = ts if TS_RE.match(ts) else (snapshot_at or captured_at)

        system_id, default_category = _prefix_info(rid)
        if system_id is None:
            system_id = stated_system
        if stated_category in KNOWN_CATEGORIES:
            category = stated_category
        else:
            category = default_category

        present = status not in ABSENT_STATUSES if status else True

        identifiers = _empty_identifiers()
        lookup_key = None
        field, value = _classify_reference(ref_raw)
        attrs = {
            "source_format": "pipe_delimited_handover",
            "document_title": title,
            "snapshot_at": snapshot_at,
            "listed_reference": ref_raw or None,
        }
        if field is None and ref_raw:
            attrs["identifier_note"] = ref_raw
        if present:
            if field is not None:
                identifiers[field] = value
        else:
            # A NOT_FOUND row exposes nothing; the reference is only the check key.
            lookup_key = value if field is not None else None
        if stated_system and stated_system != system_id:
            attrs["stated_system"] = stated_system
        if stated_category and stated_category != category:
            attrs["stated_category"] = stated_category
        if not TS_RE.match(ts):
            attrs["row_timestamp_raw"] = ts
        attrs.update(_state_attrs(category, status))

        out.append({
            "kind": "record",
            "file_id": file_id,
            "observed_at": observed_at,
            "system_id": system_id,
            "resource_id": rid,
            "category": category,
            "present": present,
            "status": status,
            "identifiers": identifiers,
            "lookup_key": lookup_key,
            "attrs": attrs,
            "location": "line %d" % lineno,
        })
    return out
