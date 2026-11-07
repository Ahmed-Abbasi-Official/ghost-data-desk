"""Parser for SQLite-compatible database snapshots (dataset/evidence/databases/*.sql).

Three shapes occur in the dataset:

* flat replica dumps (``customer_profiles_primary`` / ``customer_profiles_failover_replica``,
  ``search_documents_primary``) with a ``-- Captured at:`` / ``-- Database topology:`` header;
* mixed-collection replica dumps (``customer_data_primary`` plus ``customer_data_read_replica`` /
  ``_failover_replica`` / ``_reporting_replica`` / ``_replica_<region>``) whose rows carry
  ``origin_system``, ``collection_name`` and a ``record_json`` payload;
* per-system inventory snapshots (``customer_profiles``, ``support_records`` ...) with a
  ``snapshot_scope`` table that lists which origin systems the snapshot actually covers.

Every row becomes one ``record`` observation. Every resource table becomes one ``coverage`` dict
per system it holds (mixed tables: per system seen anywhere in the file, so a system that is missing
from one copy shows up as an empty coverage list for that copy). Inventory tables whose system is not
listed in ``snapshot_scope`` get no coverage, because the snapshot does not claim to cover them.

Each file is loaded into an in-memory SQLite database (nothing is written to disk). Values are
copied exactly as stored; null / empty / redacted identifiers become ``None``.
"""
import json
import re
import sqlite3

from common import prefix_info

_CAPTURED_RE = re.compile(r"^--\s*Captured at:\s*(\S+)", re.M)
_TOPOLOGY_RE = re.compile(r"^--\s*Database topology:\s*(\S+)", re.M)
_COMPLETED_RE = re.compile(r"^--\s*snapshot_completed_at\s*=\s*(\S+)", re.M)
_INSERT_RE = re.compile(
    r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"`\[]?([A-Za-z0-9_]+)[\"`\]]?\s+VALUES\s*\(\s*'((?:[^']|'')*)'",
    re.I,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_USER_RE = re.compile(r"^user_[A-Za-z0-9_]+$", re.I)
_REDACTED_RE = re.compile(r"redact|^\*+$|^\[?removed\]?$", re.I)

# Base table name (role suffix stripped) -> canonical system, for tables that may be empty.
_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_accounts": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}

# Source field -> identifier slot. Order inside each list is the precedence order.
_USER_FIELDS = ("user_id", "customer_reference", "subject_reference")
_EMAIL_FIELDS = ("email", "requester_email", "owner_email", "customer_email", "uploaded_by")
_PHONE_FIELDS = ("phone", "requester_phone", "owner_phone", "customer_phone")
_NAME_FIELDS = ("full_name", "display_name", "requester_name", "name", "customer_name")
_HASH_FIELDS = ("email_hash",)
_IDENTIFIER_FIELDS = set(_USER_FIELDS + _EMAIL_FIELDS + _PHONE_FIELDS + _NAME_FIELDS + _HASH_FIELDS)

_BOOL_FIELDS = ("enabled", "restore_allowed", "download_available", "archive_present")
_STATUS_FIELDS = ("account_status", "status", "last_delivery_status")
_TIME_FIELDS = ("updated_at", "indexed_at", "observed_at")
_SKIP_ATTR_FIELDS = {"resource_id", "record_json", "origin_system", "collection_name"}


def _upper_snake(value):
    if value is None:
        return None
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip()).strip("_")
    return text.upper() or None


def _ident(value):
    """Identifier exactly as stored, or None when null / empty / redacted."""
    if value is None:
        return None
    if isinstance(value, (dict, list, bool)):
        return None
    text = str(value)
    if not text.strip() or _REDACTED_RE.search(text):
        return None
    return text


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "y", "t"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "n", "f"):
        return False
    return value


def _replica_role(table):
    name = table.lower()
    m = re.search(r"_replica_([a-z0-9_]+)$", name)
    if m:
        return "regional:" + m.group(1)
    if name.endswith("_primary"):
        return "primary"
    if name.endswith("_failover_replica") or name.endswith("_failover"):
        return "failover"
    if name.endswith("_read_replica"):
        return "read_replica"
    if name.endswith("_reporting_replica") or name.endswith("_reporting"):
        return "reporting"
    if name.endswith("_replica"):
        return "replica"
    return "snapshot"


def _base_table(table):
    name = table.lower()
    name = re.sub(r"_replica_[a-z0-9_]+$", "", name)
    for suffix in ("_failover_replica", "_read_replica", "_reporting_replica", "_replica",
                   "_primary", "_failover", "_reporting"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _category(system_id, default_category, row, collection, table):
    if system_id == "support_platform" or system_id == "object_storage":
        blob = " ".join(str(x) for x in (collection, table, row.get("storage_class"), row.get("file_name")) if x)
        if "attachment" in blob.lower() or row.get("file_name"):
            return "SUPPORT_ATTACHMENT"
        if system_id == "support_platform":
            return "SUPPORT_MESSAGE"
        if "export" in blob.lower():
            return "USER_EXPORT"
        return None
    if system_id == "analytics_platform":
        blob = " ".join([str(collection or ""), str(table or "")] + [str(k) for k in row.keys()]
                        + [str(v) for v in row.values() if isinstance(v, str)])
        if "telemetry" in blob.lower():
            return "PRODUCT_TELEMETRY"
        return "ANALYTICS_PROFILE"
    return default_category


def _insert_lines(text):
    """(table, resource_id) -> 1-based line number of its INSERT statement."""
    lines = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _INSERT_RE.match(line)
        if m:
            key = (m.group(1), m.group(2).replace("''", "'"))
            lines.setdefault(key, lineno)
    return lines


def _build_record(file_id, table, rid, row, payload, snap_time, topology, scope, lineno):
    """row: SQL columns; payload: decoded record_json (or None)."""
    system_id, default_category = prefix_info(rid)
    origin_system = row.get("origin_system")
    collection = row.get("collection_name")

    fields = {}
    if payload is not None:
        for key, value in payload.items():
            if key == "document" and isinstance(value, dict):
                for dkey, dvalue in value.items():
                    fields.setdefault(dkey, dvalue)
            else:
                fields[key] = value
    else:
        fields = {k: v for k, v in row.items()}

    attrs = {"table": table, "replica_role": _replica_role(table), "snapshot_at": snap_time}
    if topology:
        attrs["topology"] = topology
    if origin_system is not None:
        attrs["origin_system"] = origin_system
        if system_id and origin_system != system_id:
            attrs["stated_system"] = origin_system
    if collection is not None:
        attrs["collection_name"] = collection
    if payload is not None and payload.get("resource_id") not in (None, rid):
        attrs["json_resource_id"] = payload.get("resource_id")
    if scope is not None:
        attrs["in_snapshot_scope"] = bool(system_id and system_id in scope)
        if system_id in scope:
            attrs["scope_captured_at"] = scope[system_id]
    for tkey in _TIME_FIELDS:
        if row.get(tkey) is not None:
            attrs[tkey] = row[tkey]

    # Identifiers.
    ids = {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}
    emails = []

    def add_email(value):
        value = _ident(value)
        if value and value.lower() not in [e.lower() for e in emails]:
            emails.append(value)

    for key in _USER_FIELDS:
        value = _ident(fields.get(key))
        if value is None:
            continue
        if _USER_RE.match(value):
            if ids["user_id"] is None:
                ids["user_id"] = value
        elif "@" in value:
            add_email(value)
        else:
            attrs.setdefault(key, value)
    for key in _EMAIL_FIELDS:
        add_email(fields.get(key))
    selector = fields.get("subject_selector")
    if selector is not None:
        sel = _ident(selector)
        if sel and _USER_RE.match(sel) and ids["user_id"] is None:
            ids["user_id"] = sel
        elif sel and "@" in sel:
            add_email(sel)
    message = fields.get("message_text", fields.get("message_excerpt"))
    if isinstance(message, str):
        found = _EMAIL_RE.findall(message)
        if found:
            attrs["message_emails"] = found
        for value in found:
            add_email(value)
    for key in _PHONE_FIELDS:
        value = _ident(fields.get(key))
        if value and ids["phone"] is None:
            ids["phone"] = value
    for key in _NAME_FIELDS:
        value = _ident(fields.get(key))
        if value and ids["name"] is None:
            ids["name"] = value
    for key in _HASH_FIELDS:
        value = _ident(fields.get(key))
        if value and ids["email_hash"] is None:
            ids["email_hash"] = value
    if emails:
        ids["email"] = emails[0]
        if len(emails) > 1:
            attrs["additional_emails"] = emails[1:]

    # Remaining facts -> attrs.
    status = None
    for key, value in fields.items():
        if key in _SKIP_ATTR_FIELDS or key in _IDENTIFIER_FIELDS:
            continue
        if key in _TIME_FIELDS:
            if payload is not None:
                attrs["record_" + key] = value
            continue
        if key in ("message_text", "message_excerpt"):
            attrs["message_excerpt"] = value
            if isinstance(value, str) and _REDACTED_RE.search(value):
                attrs["message_redacted"] = True
            continue
        if key in _BOOL_FIELDS:
            attrs[key] = _as_bool(value)
            continue
        attrs[key] = value
    if "retained_until" in fields:
        attrs.setdefault("legal_hold_until", fields["retained_until"])
        attrs.setdefault("retention_until", fields["retained_until"])
    for key in _STATUS_FIELDS:
        if fields.get(key) is not None:
            status = _upper_snake(fields[key])
            break
    if status is None and attrs.get("message_redacted"):
        status = "REDACTED"

    observed_at = None
    for tkey in _TIME_FIELDS:
        if row.get(tkey):
            observed_at = row[tkey]
            break
    if observed_at is None:
        observed_at = snap_time

    record = {
        "kind": "record",
        "file_id": file_id,
        "observed_at": observed_at,
        "system_id": system_id,
        "resource_id": rid,
        "category": _category(system_id, default_category, fields, collection, table) if system_id else None,
        "present": True,
        "status": status,
        "identifiers": ids,
        "lookup_key": None,
        "attrs": attrs,
        "location": ("line %d (%s)" % (lineno, table)) if lineno else "table %s" % table,
    }
    return record


def parse(path, file_id, captured_at):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    m = _CAPTURED_RE.search(text)
    header_time = m.group(1) if m else None
    m = _COMPLETED_RE.search(text)
    completed_time = m.group(1) if m else None
    snap_time = header_time or completed_time or captured_at
    m = _TOPOLOGY_RE.search(text)
    topology = m.group(1) if m else None
    line_of = _insert_lines(text)

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]

        scope = None
        if "snapshot_scope" in tables:
            scope = {}
            for origin, when in con.execute("SELECT origin_system, captured_at FROM snapshot_scope ORDER BY rowid"):
                scope[origin] = when

        rows_by_table = []  # (table, is_mixed, [(lineno, idx, rid, row_dict, payload)])
        for table in tables:
            if table == "snapshot_scope":
                continue
            cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))]
            if "resource_id" not in cols:
                continue
            cur = con.execute('SELECT * FROM "%s" ORDER BY rowid' % table.replace('"', '""'))
            names = [d[0] for d in cur.description]
            rows = []
            for idx, values in enumerate(cur.fetchall()):
                row = dict(zip(names, values))
                rid = row["resource_id"]
                payload = None
                if "record_json" in row and row["record_json"] is not None:
                    payload = json.loads(row["record_json"])
                lineno = line_of.get((table, rid))
                rows.append((lineno, idx, rid, row, payload))
            rows_by_table.append((table, "record_json" in cols, rows))
    finally:
        con.close()

    # Records, in file (INSERT line) order.
    flat = []
    for t_index, (table, _, rows) in enumerate(rows_by_table):
        for lineno, idx, rid, row, payload in rows:
            sort_key = (lineno if lineno is not None else 10 ** 9, t_index, idx)
            flat.append((sort_key, table, rid, row, payload, lineno))
    flat.sort(key=lambda item: item[0])

    out = []
    system_order = []
    for sort_key, table, rid, row, payload, lineno in flat:
        rec = _build_record(file_id, table, rid, row, payload, snap_time, topology, scope, lineno)
        out.append(rec)
        if rec["system_id"] and rec["system_id"] not in system_order:
            system_order.append(rec["system_id"])

    # Coverage, one dict per (table, system), tables in CREATE order.
    # A mixed-collection table (record_json) is a full dump of one database copy, so it gets a coverage
    # entry for every system seen anywhere in the file; a per-system table covers its own system.
    for table, mixed, rows in rows_by_table:
        ordered = sorted(rows, key=lambda r: (r[0] if r[0] is not None else 10 ** 9, r[1]))
        table_systems = []
        for _, _, rid, _, _ in ordered:
            sid = prefix_info(rid)[0]
            if sid and sid not in table_systems:
                table_systems.append(sid)
        if mixed:
            systems = list(system_order)
        else:
            mapped = _TABLE_SYSTEM.get(_base_table(table))
            systems = [mapped] if mapped else []
        systems += [s for s in table_systems if s not in systems]
        for sid in systems:
            if scope is not None and sid not in scope:
                continue  # snapshot does not claim to cover this system
            observed = scope.get(sid, snap_time) if scope is not None else snap_time
            out.append({
                "kind": "coverage",
                "file_id": file_id,
                "observed_at": observed,
                "system_id": sid,
                "table": table,
                "replica_role": _replica_role(table),
                "resource_ids": [r[2] for r in ordered if prefix_info(r[2])[0] == sid],
            })
    return out
