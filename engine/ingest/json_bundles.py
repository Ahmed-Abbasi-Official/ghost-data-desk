"""Parser for JSON snapshot bundles and service resource exports (see SCHEMA.md).

Scope (dataset/evidence/records/):

1. customer_data_snapshot_bundle_*.json
       {"exported_at", "source",
        "snapshots": [{"captured_at", "data_store", "collection", "records": [...]}]}
   Two kinds of snapshot:
   a. Store collections (customer_profiles, customer_search_documents,
      customer_analytics_reports, backup_catalog, customer_export_jobs,
      connector_configurations, security_audit_events, support_conversations,
      ticket_attachments, retained_order_records, deleted_customer_references).
      Each row is one stored record; one coverage dict is emitted per snapshot
      (even when the collection is empty), replica_role "snapshot".
   b. operational_record_history (held by backup_restore_service,
      support_storage_service, privacy_treatment_service): per-resource lookup
      results {"recorded_at", "system", "resource_id", "record_type", "result",
      "lookup_value"} spanning many systems. It is a lookup trace, not a table of
      the resources it mentions, so NO coverage dict is emitted for it.

2. service_resource_export_*.json
       {"exported_at", "region", "source",
        "service_exports": {<system>: {<list_name>: [entries...]}}}
   Lists: account_service.profile_lookups (status_code 200/404, profile or null),
   analytics_platform.profiles, backup_service.restore_entries,
   export_service.exports, integration_service.connectors,
   search_service.index_queries (query + hits.documents[]),
   security_service.audit_records, support_platform.tickets.
   Every entry carries its own observed_at. These lists are selective per-subject
   exports, not whole tables (e.g. audit_records lists 3 of the 10 audit records
   the same-day bundle snapshot still holds), so NO coverage dicts are emitted.

Nothing is invented: identifier values are copied exactly as they appear;
null / empty / "[REDACTED]" become None.
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

CANONICAL_SYSTEMS = {s for _, s, _ in PREFIX_TABLE}

KNOWN_CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE", "DATA_FLOW",
}

# Status words meaning the record does not exist at observed_at.
ABSENT_STATUSES = {"NOT_FOUND", "PURGED", "REMOVED", "MISSING", "ABSENT", "404"}

HISTORY_COLLECTIONS = {"operational_record_history"}

USER_ID_RE = re.compile(r"^user_\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ORDER_RE = re.compile(r"\border(s)?\b", re.IGNORECASE)
REDACTED_MARKERS = {"[REDACTED]", "REDACTED", "[redacted]", "***", "<redacted>"}

# Field name -> identifier slot (the stored record still exposes these).
IDENT_FIELDS = {
    "user_id": "user_id",
    "customer_reference": "user_id",
    "email": "email",
    "owner_email": "email",
    "customer_email": "email",
    "requester_email": "email",
    "uploaded_by": "email",
    "phone": "phone",
    "owner_phone": "phone",
    "requester_phone": "phone",
    "full_name": "name",
    "name": "name",
    "display_name": "name",
    "requester_name": "name",
    "email_hash": "email_hash",
}
# Reference fields whose value may be a user_NN or an email: classified by shape,
# and the raw value is also kept in attrs as a linkage fact.
REFERENCE_FIELDS = ("subject_reference", "subject_selector")
# Message-like text fields: kept in attrs as message_excerpt; emails inside are exposed.
MESSAGE_FIELDS = ("message_text", "message_excerpt")


# ---------------------------------------------------------------- helpers

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
    """Copy a scalar identifier exactly; null / empty / redacted -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip() or value.strip() in REDACTED_MARKERS:
            return None
    return value


def _empty_identifiers():
    return {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}


def _classify_reference(value):
    """Return identifier slot for a free reference value (user_NN or email)."""
    v = _clean(value)
    if not isinstance(v, str):
        return None, None
    if USER_ID_RE.match(v):
        return "user_id", v
    if EMAIL_RE.match(v):
        return "email", v
    return None, None


def _is_redacted(value):
    return isinstance(value, str) and value.strip() in REDACTED_MARKERS


def _support_category(collection, row):
    """SUPPORT_MESSAGE vs SUPPORT_ATTACHMENT, decided from the row content."""
    coll = (collection or "").lower()
    storage = str(row.get("storage_class") or "").lower()
    if "attachment" in coll or "attachment" in storage:
        return "SUPPORT_ATTACHMENT", "attachment collection/storage_class"
    if any(k in row for k in MESSAGE_FIELDS):
        return "SUPPORT_MESSAGE", "message content"
    fname = str(row.get("file_name") or "")
    if fname:
        return "SUPPORT_ATTACHMENT", "file_name"
    return None, None


def _category(resource_id, collection, row, record_type=None):
    if record_type:
        rt = _norm_status(record_type)
        if rt in KNOWN_CATEGORIES:
            return rt, "record_type"
    _, cat = _prefix_info(resource_id)
    rid = (resource_id or "").lower()
    if rid.startswith("support_"):
        return _support_category(collection, row)
    if rid.startswith("analytics_"):
        blob = json.dumps(row, sort_keys=True).lower() + " " + (collection or "").lower()
        if "telemetry" in blob:
            return "PRODUCT_TELEMETRY", "record says telemetry"
        return "ANALYTICS_PROFILE", "prefix"
    if cat:
        return cat, "prefix"
    return None, None


def _extract_row(row, nested_keys=("document", "profile")):
    """Split a stored row into (identifiers, attrs, extra_emails).

    identifiers: what the stored row still exposes, copied verbatim.
    attrs: every other field (scalars, lists, small dicts) plus linkage facts.
    """
    ident = _empty_identifiers()
    attrs = {}
    extra_emails = []

    def put(slot, value):
        value = _clean(value)
        if value is None:
            return
        if ident[slot] is None:
            ident[slot] = value
        elif ident[slot] != value:
            attrs.setdefault("other_" + slot + "s", [])
            if value not in attrs["other_" + slot + "s"]:
                attrs["other_" + slot + "s"].append(value)

    def walk(obj, prefix):
        for key, value in obj.items():
            if key == "resource_id" and not prefix:
                continue
            if key in nested_keys and isinstance(value, dict):
                walk(value, prefix + key + ".")
                continue
            if key in nested_keys and value is None:
                attrs[prefix + key] = None
                continue
            if key in IDENT_FIELDS:
                put(IDENT_FIELDS[key], value)
                if key in ("uploaded_by",):
                    attrs[prefix + key] = value
                continue
            if key in REFERENCE_FIELDS:
                attrs[prefix + key] = value
                slot, v = _classify_reference(value)
                if slot:
                    put(slot, v)
                continue
            if key in MESSAGE_FIELDS:
                attrs["message_excerpt"] = value
                if _is_redacted(value):
                    attrs["message_redacted"] = True
                elif isinstance(value, str):
                    for em in EMAIL_IN_TEXT_RE.findall(value):
                        extra_emails.append(em)
                    if ORDER_RE.search(value):
                        attrs["mentions_order"] = True
                continue
            attrs[prefix + key] = value

    walk(row, "")
    for em in extra_emails:
        put("email", em)
    return ident, attrs


def _record(file_id, observed_at, resource_id, category, present, status,
            identifiers, lookup_key, attrs, location):
    system_id, _ = _prefix_info(resource_id)
    return {
        "kind": "record",
        "file_id": file_id,
        "observed_at": observed_at,
        "system_id": system_id,
        "resource_id": resource_id,
        "category": category,
        "present": present,
        "status": status,
        "identifiers": identifiers,
        "lookup_key": lookup_key,
        "attrs": attrs,
        "location": location,
    }


def _stated(attrs, resource_id, stated):
    canon, _ = _prefix_info(resource_id)
    if stated and canon and stated != canon:
        attrs["stated_system"] = stated


# ---------------------------------------------------------------- bundles

def _parse_bundle(doc, file_id, captured_at):
    out = []
    exported_at = doc.get("exported_at") or captured_at
    source = doc.get("source")
    for si, snap in enumerate(doc.get("snapshots") or []):
        snap_at = snap.get("captured_at") or exported_at
        store = snap.get("data_store")
        collection = snap.get("collection")
        rows = snap.get("records") or []
        base = "snapshots[%d]" % si

        if collection in HISTORY_COLLECTIONS:
            for ri, row in enumerate(rows):
                out.append(_history_record(row, file_id, snap_at, store, collection,
                                           source, "%s.records[%d]" % (base, ri)))
            continue

        rids = []
        for ri, row in enumerate(rows):
            rid = row.get("resource_id")
            if not rid:
                continue
            rids.append(rid)
            ident, attrs = _extract_row(row)
            cat, basis = _category(rid, collection, row)
            attrs["table"] = collection
            attrs["data_store"] = store
            attrs["replica_role"] = "snapshot"
            attrs["snapshot_captured_at"] = snap_at
            if source:
                attrs["source"] = source
            if basis and rid.lower().startswith(("support_", "analytics_")):
                attrs["category_basis"] = basis
            _stated(attrs, rid, store if store in CANONICAL_SYSTEMS else None)

            status = None
            if "account_status" in row:
                status = _norm_status(row.get("account_status"))
            elif attrs.get("message_redacted"):
                status = "REDACTED"
            if "retained_until" in row:
                attrs["legal_hold_until"] = row.get("retained_until")
            # A stored row is listed in the snapshot -> present. A tombstone's
            # account_status DELETED describes the account, not the tombstone row.
            present = not (status in ABSENT_STATUSES)
            out.append(_record(file_id, snap_at, rid, cat, present, status, ident,
                               None, attrs, "%s.records[%d]" % (base, ri)))

        sys_id = store if store in CANONICAL_SYSTEMS else None
        if sys_id is None and rids:
            sys_id, _ = _prefix_info(rids[0])
        out.append({
            "kind": "coverage",
            "file_id": file_id,
            "observed_at": snap_at,
            "system_id": sys_id or store,
            "table": collection,
            "replica_role": "snapshot",
            "resource_ids": rids,
        })
    return out


def _history_record(row, file_id, snap_at, store, collection, source, location):
    rid = row.get("resource_id")
    observed = row.get("recorded_at") or snap_at
    result = _norm_status(row.get("result"))
    cat, basis = _category(rid, None, row, row.get("record_type"))
    attrs = {
        "result": row.get("result"),
        "record_type": row.get("record_type"),
        "history_store": store,
        "table": collection,
        "snapshot_captured_at": snap_at,
        "category_basis": basis,
    }
    if source:
        attrs["source"] = source
    for key, value in row.items():
        if key not in ("recorded_at", "system", "resource_id", "record_type",
                       "result", "lookup_value"):
            attrs[key] = value
    _stated(attrs, rid, row.get("system"))
    lookup = _clean(row.get("lookup_value"))
    present = result not in ABSENT_STATUSES if result else None
    return _record(file_id, observed, rid, cat, present, result,
                   _empty_identifiers(), lookup, attrs, location)


# ---------------------------------------------------------------- service exports

def _status_from_code(code):
    if code is None:
        return None, None
    try:
        c = int(code)
    except (TypeError, ValueError):
        return _norm_status(code), None
    if c == 404 or c == 410:
        return "NOT_FOUND", False
    if 200 <= c < 300:
        return "FOUND", True
    return "HTTP_%d" % c, None


def _parse_export(doc, file_id, captured_at):
    out = []
    exported_at = doc.get("exported_at") or captured_at
    region = doc.get("region")
    source = doc.get("source")
    exports = doc.get("service_exports") or {}
    for system in exports:
        lists = exports[system] or {}
        for list_name in lists:
            entries = lists[list_name] or []
            base = "service_exports.%s.%s" % (system, list_name)
            for ei, entry in enumerate(entries):
                loc = "%s[%d]" % (base, ei)
                common = {"export_list": list_name, "export_system": system}
                if region:
                    common["region"] = region
                if source:
                    common["source"] = source
                if isinstance(entry.get("hits"), dict):
                    out.extend(_query_records(entry, file_id, exported_at, common, loc))
                    continue
                out.append(_export_record(entry, file_id, exported_at, common, loc))
    return out


def _export_record(entry, file_id, exported_at, common, loc):
    rid = entry.get("resource_id")
    observed = entry.get("observed_at") or exported_at
    row = {k: v for k, v in entry.items() if k != "observed_at"}
    ident, attrs = _extract_row(row)
    attrs.update(common)
    cat, basis = _category(rid, common["export_list"], row)
    if basis and (rid or "").lower().startswith(("support_", "analytics_")):
        attrs["category_basis"] = basis
    _stated(attrs, rid, common["export_system"] if common["export_system"] in CANONICAL_SYSTEMS else None)

    status, present = None, True
    if "status_code" in entry:
        attrs["http_status"] = entry.get("status_code")
        attrs.pop("status_code", None)
        status, p = _status_from_code(entry.get("status_code"))
        if p is not None:
            present = p
    elif "status" in entry:
        status = _norm_status(entry.get("status"))
        present = status not in ABSENT_STATUSES
    if "profile" in entry and entry.get("profile") is None and present:
        present = False
    return _record(file_id, observed, rid, cat, present, status, ident, None, attrs, loc)


def _query_records(entry, file_id, exported_at, common, loc):
    out = []
    observed = entry.get("observed_at") or exported_at
    query = _clean(entry.get("query"))
    hits = entry.get("hits") or {}
    docs = hits.get("documents") or []
    total = hits.get("total")
    for di, d in enumerate(docs):
        rid = d.get("resource_id") or entry.get("resource_id")
        ident, attrs = _extract_row({k: v for k, v in d.items()})
        attrs.update(common)
        attrs["hits_total"] = total
        attrs["query_resource_id"] = entry.get("resource_id")
        cat, _ = _category(rid, None, d)
        _stated(attrs, rid, common["export_system"] if common["export_system"] in CANONICAL_SYSTEMS else None)
        out.append(_record(file_id, observed, rid, cat, True, "FOUND", ident, query,
                           attrs, "%s.hits.documents[%d]" % (loc, di)))
    if not docs and entry.get("resource_id"):
        rid = entry.get("resource_id")
        attrs = dict(common)
        attrs["hits_total"] = total
        cat, _ = _category(rid, None, {})
        out.append(_record(file_id, observed, rid, cat, False, "NOT_FOUND",
                           _empty_identifiers(), query, attrs, loc))
    return out


# ---------------------------------------------------------------- entry point

def parse(path, file_id, captured_at):
    """Return a list of observation dicts for one evidence file."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        return []
    if "snapshots" in doc:
        return _parse_bundle(doc, file_id, captured_at)
    if "service_exports" in doc:
        return _parse_export(doc, file_id, captured_at)
    return []
