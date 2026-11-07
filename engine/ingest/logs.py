"""Parser for dataset/evidence/logs/*.log (see SCHEMA.md).

Line styles handled (every line starts with an ISO UTC timestamp):

1. key=value         ``TS level=INFO service=x trace_id=t event=e k=v k="v v"``
                     (resource_lookup_trace_*, service_events_*, support_replica)
2. bare level + kv   ``TS INFO service=x event=record_lookup system="..." result="..."``
                     (notification_delivery_*, search_index_diagnostic_*)
3. bracketed         ``TS [LEVEL] [service] [trace] event: k="v" ...``   (application_trace_*)
4. pipe separated    ``TS | LEVEL | service | trace | event | k="v" ...`` (operations_stream_*)
5. JSON line         ``TS {"severity": ..., "service": ..., ...}``     (structured_service_events_*)
6. RFC5424 syslog    ``TS <pri>1 host service trace - - level=X k="v"`` (syslog_archive_*)
7. cloud runtime     ``TS requestId=t level=X function=service message="k=\\"v\\" ..."``
                     (cloud_runtime_export_*)

Multi-line traces are grouped by trace id: ``*_started`` lines supply the query key
(``lookup_key``), connection / cache / retry lines supply read-path context (pool, route),
and every line that names a ``resource_id`` produces (or extends) one record per
(trace, resource). Lines with no resource and no trace context (log_router, dns_cache,
webhook_delivery, thread_pool, ...) are operational noise and are skipped.

Logs never list a whole table, so this parser emits no coverage dicts.
Nothing is invented: identifiers are copied verbatim; null / "" / "[REDACTED]" -> None.
"""

import json
import re

PREFIX_TABLE = [
    # longer prefixes first
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

# result / status words meaning "the record does not exist at observed_at"
ABSENT_WORDS = {"NOT_FOUND", "MISS", "DELETED", "PURGED", "REMOVED", "MISSING", "ABSENT", "404"}
# lookup-style result words normalized to FOUND / NOT_FOUND
RESULT_WORD_MAP = {"HIT": "FOUND", "MISS": "NOT_FOUND"}

# fields that carry the query key of a trace (never copied into identifiers)
LOOKUP_KEY_FIELDS = ("lookup_key", "subject_key", "subject_ref", "match_key", "owner_key", "scan_key")
# per-line structural fields (not copied into attrs)
STRUCTURAL_FIELDS = {"resource_id", "event", "level", "severity", "logger", "service", "trace_id",
                     "syslog_pri", "syslog_host", "attempt"}
# fields that are stored identifiers of the record -> identifiers slot
IDENTIFIER_FIELDS = {
    "customer_reference": "user_id",
    "email": "email",
    "customer_email": "email",
    "owner_email": "email",
    "requester_email": "email",
    "phone": "phone",
    "requester_phone": "phone",
    "requester_name": "name",
    "name": "name",
    "email_hash": "email_hash",
}
# context events that describe the read path of a trace, not a resource state
CONTEXT_EVENTS = {"connection_acquired", "cache_lookup", "dependency_retry", "dependency_recovered"}

BOOL_FIELDS = {"enabled", "restore_allowed", "archive_present", "available", "eligible",
               "deletion_block", "scheduler_registered", "marker_found", "email_opt_in", "retry"}
INT_FIELDS = {"rows", "http_status", "total_hits", "direct_identifiers", "duration_ms", "took_ms",
              "latency_ms", "shard", "seq", "rows_read"}
RENAMED_ATTRS = {"index": "index_name", "file": "file_name", "available": "download_available"}

USER_ID_RE = re.compile(r"^user_\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S*)')
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.\-]+):\s*(.*)$")
SYSLOG_RE = re.compile(r"^<(\d+)>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s*(.*)$")
LEVEL_RE = re.compile(r"^(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL)\s+(.*)$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _prefix_info(resource_id):
    rid = (resource_id or "").lower()
    for prefix, system_id, category in PREFIX_TABLE:
        if rid.startswith(prefix):
            return system_id, category
    return None, None


def _norm_word(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or None


def _clean(value):
    """Copy a scalar verbatim; null / empty / redacted -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("null", "none") or s.upper() in ("[REDACTED]", "REDACTED"):
            return None
    return value


def _to_bool(value):
    v = _clean(value)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return value


def _to_int(value):
    v = _clean(value)
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return value


def _convert(key, value):
    if key in BOOL_FIELDS:
        return _to_bool(value)
    if key in INT_FIELDS:
        return _to_int(value)
    return _clean(value)


def _unquote(v):
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return re.sub(r'\\(.)', r'\1', v[1:-1])
    return v


def _kvs(text):
    out = {}
    for k, v in KV_RE.findall(text):
        out[k] = _unquote(v)
    return out


def _tokenize(line):
    """Return (style, timestamp, fields) or None when the line has no structure."""
    m = TS_RE.match(line)
    if not m:
        return None
    ts, rest = m.group(1), m.group(2).strip()
    if rest.startswith("{"):
        try:
            data = json.loads(rest)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        fields = {}
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, sort_keys=True)
            fields[str(k)] = v
        if "severity" in fields and "level" not in fields:
            fields["level"] = fields["severity"]
        return "json", ts, fields
    m = BRACKET_RE.match(rest)
    if m:
        level, service, trace, event, body = m.groups()
        fields = {"level": level, "service": service, "trace_id": trace, "event": event}
        fields.update(_kvs(body))
        return "bracket", ts, fields
    if rest.startswith("|"):
        parts = [p.strip() for p in rest.lstrip("|").split(" | ", 4)]
        if len(parts) == 5:
            level, service, trace, event, body = parts
            fields = {"level": level, "service": service, "trace_id": trace, "event": event}
            fields.update(_kvs(body))
            return "pipe", ts, fields
        return None
    m = SYSLOG_RE.match(rest)
    if m:
        pri, host, service, trace, body = m.groups()
        fields = {"syslog_pri": pri, "syslog_host": host, "service": service}
        if trace != "-":
            fields["trace_id"] = trace
        fields.update(_kvs(body))
        return "syslog", ts, fields
    if rest.startswith("requestId="):
        outer = _kvs(rest)
        message = outer.pop("message", "")
        fields = {}
        if "requestId" in outer:
            fields["trace_id"] = outer.pop("requestId")
        if "function" in outer:
            fields["service"] = outer.pop("function")
        fields.update(outer)
        fields.update(_kvs(message))
        return "cloud_runtime", ts, fields
    m = LEVEL_RE.match(rest)
    if m:
        fields = {"level": m.group(1)}
        fields.update(_kvs(m.group(2)))
        return "level_kv", ts, fields
    fields = _kvs(rest)
    if not fields:
        return None
    return "kv", ts, fields


def _classify_value(value):
    """Return the identifier slot for a free value (matched_value / subject_reference)."""
    v = _clean(value)
    if v is None or not isinstance(v, str):
        return None
    s = v.strip()
    if USER_ID_RE.match(s):
        return "user_id"
    if EMAIL_RE.match(s):
        return "email"
    if HEX64_RE.match(s):
        return "email_hash"
    if PHONE_RE.match(s) and sum(c.isdigit() for c in s) >= 7:
        return "phone"
    return None


def _empty_identifiers():
    return {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}


def _category_for(resource_id, record_type, content_text):
    _, prefix_cat = _prefix_info(resource_id)
    stated = _norm_word(record_type)
    rid = (resource_id or "").lower()
    low = content_text.lower()
    if rid.startswith("support_"):
        if stated in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"):
            return stated
        if "attachment" in low or "file_name" in low or "thumbnail" in low:
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE"
    if rid.startswith("analytics_"):
        if stated == "PRODUCT_TELEMETRY" or "telemetry" in low:
            return "PRODUCT_TELEMETRY"
        return "ANALYTICS_PROFILE"
    if prefix_cat is not None:
        return prefix_cat
    if stated in KNOWN_CATEGORIES:
        return stated
    return None


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

def _scan(path):
    """Return (entries, unparsed_line_numbers); entries = [(lineno, style, ts, fields)]."""
    entries, unparsed = [], []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            tok = _tokenize(line)
            if tok is None:
                unparsed.append(lineno)
                continue
            style, ts, fields = tok
            entries.append((lineno, style, ts, fields))
    return entries, unparsed


def _is_started(fields):
    return str(fields.get("event") or "").endswith("_started")


def _trace_context(entries):
    """Per trace id: query key (from *_started lines) and read-path context."""
    ctx = {}
    for lineno, _style, _ts, f in entries:
        trace = f.get("trace_id")
        if not trace:
            continue
        c = ctx.setdefault(trace, {"lookup": None, "start_extra": {}, "pool": None, "route": None,
                                   "cache": None, "cache_result": None, "retries": 0,
                                   "retry_reasons": [], "context_lines": []})
        event = f.get("event")
        if _is_started(f):
            for key in LOOKUP_KEY_FIELDS:
                if key in f and c["lookup"] is None:
                    c["lookup"] = (key, _clean(f[key]), lineno)
            for key, value in f.items():
                if key in STRUCTURAL_FIELDS or key in LOOKUP_KEY_FIELDS or key in ("instance", "pod", "region"):
                    continue
                c["start_extra"].setdefault(key, _convert(key, value))
            c["context_lines"].append(lineno)
        elif event in CONTEXT_EVENTS and "resource_id" not in f:
            if event == "connection_acquired":
                c["pool"] = _clean(f.get("pool"))
            elif event == "cache_lookup":
                c["cache"] = _clean(f.get("cache"))
                c["cache_result"] = _clean(f.get("result"))
            elif event == "dependency_retry":
                c["retries"] += 1
                if _clean(f.get("reason")) is not None:
                    c["retry_reasons"].append(f.get("reason"))
            elif event == "dependency_recovered":
                c["route"] = _clean(f.get("route"))
            c["context_lines"].append(lineno)
    return ctx


def _role_from_pool(pool):
    p = (pool or "").lower()
    for role in ("failover", "read_replica", "replica", "reporting", "primary"):
        if p.endswith("_" + role) or p == role:
            return role
    return None


def _presence(fields_list):
    """Derive (present, status) from the resource lines of one (trace, resource) group."""
    present, status = None, None
    explicit = None   # an explicit result/status word from the file
    exists = None     # rows / http / result=hit evidence
    for f in fields_list:
        event = str(f.get("event") or "")
        result = _norm_word(f.get("result"))
        if result is not None and event != "cache_lookup":
            word = RESULT_WORD_MAP.get(result, result)
            explicit = word
            exists = word not in ABSENT_WORDS
        rows = _to_int(f.get("rows")) if "rows" in f else None
        http = _to_int(f.get("http_status")) if "http_status" in f else None
        if isinstance(rows, int):
            exists = rows > 0
        elif isinstance(http, int) and http in (200, 404, 410):
            exists = http == 200
        if "archive_present" in f and _to_bool(f.get("archive_present")) is False:
            exists = False
        if _to_bool(f.get("marker_found")) is True:
            exists = False
            explicit = explicit or "DELETED"
        if event == "thumbnail_failed":
            status = status or "THUMBNAIL_FAILED"
    if explicit is not None:
        return exists, explicit
    if exists is not None:
        return exists, "FOUND" if exists else "NOT_FOUND"
    for f in fields_list:
        decision = _norm_word(f.get("decision"))
        if decision:
            return True, decision
    for f in fields_list:
        if "enabled" in f and isinstance(_to_bool(f.get("enabled")), bool):
            return True, "ENABLED" if _to_bool(f.get("enabled")) else "DISABLED"
    if status == "THUMBNAIL_FAILED":
        # a failed thumbnail render does not state whether the attachment exists
        return None, status
    return True, "FOUND"


# --------------------------------------------------------------------------
# record building
# --------------------------------------------------------------------------

def _build_record(file_id, group, ctx):
    lines = group["lines"]          # [(lineno, style, ts, fields)]
    trace = group["trace"]
    resource_id = group["resource_id"]
    fields_list = [f for _, _, _, f in lines]
    last_lineno, last_style, last_ts, _ = lines[-1]

    system_id, _ = _prefix_info(resource_id)
    identifiers = _empty_identifiers()
    attrs = {"log_style": last_style}
    if trace:
        attrs["trace_id"] = trace
    events = [f.get("event") for f in fields_list if f.get("event")]
    if events:
        attrs["event"] = events[-1]
        if len(events) > 1:
            attrs["events"] = events

    lookup_key, lookup_field, lookup_line = None, None, None
    tc = ctx.get(trace) if trace else None
    # inline query key on the resource line wins, then the trace's *_started line
    for f in fields_list:
        for key in LOOKUP_KEY_FIELDS:
            if key in f and lookup_field is None:
                if key == "subject_ref" and str(f.get("event") or "") == "message_read":
                    continue  # stored subject of the message, handled as identifier
                lookup_key, lookup_field = _clean(f[key]), key
    if lookup_field is None and tc and tc["lookup"] is not None:
        lookup_field, lookup_key, lookup_line = tc["lookup"]
    correlation = None
    for f in fields_list:
        if "correlation_key" in f:
            correlation = _clean(f["correlation_key"])
    if lookup_field is None and correlation is not None:
        lookup_key, lookup_field = correlation, "correlation_key"
    elif correlation is not None and correlation != lookup_key:
        attrs["correlation_key"] = correlation
    if lookup_field is not None:
        attrs["lookup_key_field"] = lookup_field
    if lookup_line is not None:
        attrs["lookup_line"] = lookup_line

    record_type = None
    for f in fields_list:
        service = _clean(f.get("service"))
        event = str(f.get("event") or "")
        for key, value in f.items():
            if key in STRUCTURAL_FIELDS or (key in LOOKUP_KEY_FIELDS and not (
                    key == "subject_ref" and event == "message_read")):
                continue
            if key == "correlation_key":
                continue
            if key in IDENTIFIER_FIELDS:
                slot = IDENTIFIER_FIELDS[key]
                v = _clean(value)
                if slot == "user_id" and v is not None and _classify_value(v) not in (None, "user_id"):
                    slot = _classify_value(v)
                if v is not None and identifiers[slot] is None:
                    identifiers[slot] = v
                continue
            if key in ("matched_value", "subject_ref"):
                v = _clean(value)
                slot = _classify_value(v)
                if v is not None and slot is not None and identifiers[slot] is None:
                    identifiers[slot] = v
                elif v is not None and slot is None:
                    attrs["unclassified_" + key] = v
                if key == "subject_ref":
                    attrs["subject_reference"] = v
                continue
            if key == "subject_reference":
                v = _clean(value)
                attrs["subject_reference"] = v
                slot = _classify_value(v)
                if v is not None and slot is not None and identifiers[slot] is None:
                    identifiers[slot] = v
                elif v is not None:
                    attrs["unclassified_subject_reference"] = v
                continue
            if key == "record_type":
                record_type = value
                attrs["record_type"] = _clean(value)
                continue
            if key == "system":
                stated = _clean(value)
                if stated is not None and stated != system_id:
                    attrs["stated_system"] = stated
                continue
            if key == "message_text":
                v = _clean(value)
                attrs["message_excerpt"] = v
                if v is None and isinstance(value, str) and "REDACTED" in value.upper():
                    attrs["message_redacted"] = True
                continue
            if key == "marker_found":
                attrs["deletion_marker_found"] = _to_bool(value)
                continue
            if key == "level" or key == "severity":
                continue
            name = RENAMED_ATTRS.get(key, key)
            attrs[name] = _convert(key, value)
        if service is not None:
            if "record_type" in f or "system" in f:
                attrs["reporting_service"] = service
            elif system_id is not None and service != system_id:
                attrs["stated_system"] = service
            if service.endswith("_replica"):
                attrs["replica_role"] = "replica"
        level = _clean(f.get("level") or f.get("severity"))
        if level is not None:
            attrs["level"] = str(level).upper()

    # restore decision facts (copied, plus the boolean they state)
    if "restore_allowed" not in attrs:
        decision = _norm_word(attrs.get("decision"))
        if decision in ("DENY", "DENIED", "REJECT", "REJECTED"):
            attrs["restore_allowed"] = False
            attrs["restore_allowed_basis"] = "decision"
        elif decision in ("ALLOW", "ALLOWED", "APPROVE", "APPROVED", "PERMIT"):
            attrs["restore_allowed"] = True
            attrs["restore_allowed_basis"] = "decision"
        elif isinstance(attrs.get("eligible"), bool) and str(attrs.get("event", "")).startswith("restore_"):
            attrs["restore_allowed"] = attrs["eligible"]
            attrs["restore_allowed_basis"] = "eligible"
    if "archive_key" in attrs and "object_key" not in attrs:
        attrs["object_key"] = attrs["archive_key"]

    # read-path context of the trace
    if tc:
        for key, value in tc["start_extra"].items():
            attrs.setdefault(key, value)  # e.g. source= on the *_started line
        if tc["pool"] is not None:
            attrs["pool"] = tc["pool"]
        if tc["route"] is not None:
            attrs["route"] = tc["route"]
        if tc["cache"] is not None:
            attrs["cache"] = tc["cache"]
            attrs["cache_result"] = tc["cache_result"]
        if tc["retries"]:
            attrs["retries"] = tc["retries"]
            attrs["retry_reasons"] = list(tc["retry_reasons"])
        if "replica_role" not in attrs:
            role = tc["route"] or _role_from_pool(tc["pool"])
            if role is not None:
                attrs["replica_role"] = role
        if tc["context_lines"]:
            attrs["trace_lines"] = sorted(set(tc["context_lines"] + [ln for ln, _, _, _ in lines]))

    present, status = _presence(fields_list)
    if present is False:
        # an absent record exposes nothing; keep any reference only as linkage in attrs
        for slot, value in identifiers.items():
            if value is not None and not any(
                    _clean(f.get("subject_reference")) == value for f in fields_list):
                attrs.setdefault("reported_" + slot, value)
                identifiers[slot] = None

    content_text = " ".join(str(e) for e in events) + " " + " ".join(sorted(attrs))
    category = _category_for(resource_id, record_type, content_text)

    if len(lines) == 1:
        location = "line %d" % last_lineno
    else:
        location = "lines %s" % ",".join(str(ln) for ln, _, _, _ in lines)

    return {
        "kind": "record",
        "file_id": file_id,
        "observed_at": last_ts,
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


def parse(path, file_id, captured_at):
    """Return a list of observation dicts for one .log evidence file."""
    entries, _unparsed = _scan(path)
    ctx = _trace_context(entries)

    groups, order = {}, []
    for entry in entries:
        lineno, _style, _ts, f = entry
        resource_id = _clean(f.get("resource_id"))
        if resource_id is None or _is_started(f):
            continue  # started lines only provide the query key; noise lines carry no resource
        if f.get("event") in CONTEXT_EVENTS:
            continue
        trace = _clean(f.get("trace_id"))
        key = (trace, resource_id) if trace else ("#line", lineno)
        if key not in groups:
            groups[key] = {"trace": trace, "resource_id": resource_id, "lines": []}
            order.append(key)
        groups[key]["lines"].append(entry)

    out = []
    for key in order:
        rec = _build_record(file_id, groups[key], ctx)
        if not rec.get("observed_at"):
            rec["observed_at"] = captured_at
        out.append(rec)
    return out
