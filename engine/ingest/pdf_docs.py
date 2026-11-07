"""Parser for PDF business documents (see SCHEMA.md).

Text is extracted with pypdf (default extraction mode) and parsed with
deterministic regular expressions; no LLM is needed for these documents.

Layout produced by the document generator (every page):
    line 1  document title          e.g. "COMPLIANCE JOURNAL - SIGNED RECORD EXTRACT"
    line 2  subtitle / reference    e.g. "Reference CJX-20260905-0630"
    line 3  section heading         e.g. "Journal entry"
    body    a two-column table: a left-column label ("Record", "Detail", "Item",
            "Customer request", "Product", "Delivery address", "Timeline") followed
            by its value line(s) (wrapped values continue on following lines).
            Timestamps were rendered as "label: value" split at the colon, so they
            come out as two lines: "submitted 18 Jun 2026 10" + "24 UTC"
            -> "submitted" = 2026-06-18T10:24:00Z.
            Register tables come out as a row number line followed by a
            pipe-delimited row line.

Document shapes handled (detected from content, not file name):
* support document (a support_NNN reference): one record, category
  SUPPORT_ATTACHMENT when the document describes attachment/file properties,
  else SUPPORT_MESSAGE.
* signed journal extract (an audit_record_NNN reference): one SECURITY_AUDIT_LOG
  record with retention_until.
* register (pipe-delimited rows "ORD-... | user_NN | reason | date | amount |
  Retain until ... | owner"): one LEGAL_HOLD_RECORD-style record per row. The
  register carries no legal_hold_NNN id, so resource_id is the order reference
  copied verbatim and attrs["resource_reference_kind"] = "order_reference".
* customer statement (no canonical resource id): one record whose resource_id
  is the statement reference copied verbatim; system_id / category are None
  because the document does not state them (linking the statement to a
  canonical resource is left to the resolver, never guessed here).

No coverage dicts are emitted: these documents are business artifacts, not full
table / store snapshots, so absence-by-omission must not be inferred from them.
Personal data beyond identifiers is minimised: a delivery address or payment
line is recorded only as a boolean flag.
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
    ("support_", "support_platform", None),  # decided from content
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
    ("object_", "object_storage", None),  # decided from content
]
RESOURCE_RE = re.compile(r"\b(?:%s)\d+\b" % "|".join(p for p, _, _ in PREFIX_TABLE))

LABELS = {"Record", "Detail", "Item", "Customer request", "Product", "Delivery address", "Timeline"}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

DATE_RE = r"(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<y>\d{4})"
TS_SPLIT_HEAD_RE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z \-]*?)\s+" + DATE_RE + r"\s+(?P<hh>\d{1,2})$")
TS_SPLIT_TAIL_RE = re.compile(r"^(?P<mm>\d{2})\s+UTC$")
TS_ONE_LINE_RE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z \-]*?)\s+" + DATE_RE +
                            r"\s+(?P<hh>\d{1,2}):(?P<mm>\d{2})\s+UTC$")
DATE_ONLY_RE = re.compile(r"^" + DATE_RE + r"$")
ROW_NO_RE = re.compile(r"^\d+$")

USER_ID_RE = re.compile(r"^user_\d+$")
EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")
PHONE_RE = re.compile(r"^\+\d[\d\s().-]{6,}\d$")
EMAIL_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Z][a-z'’-]+(?: [A-Z][a-z'’-]+){1,3}$")
SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s?(?:B|KB|MB|GB)$", re.I)
ORDER_RE = re.compile(r"^ORD-\d+$")

# value-prefix keys -> attrs name (longest phrases first so they win)
VALUE_KEYS = [
    ("subject reference", "subject_reference"),
    ("event reference", "event_reference"),
    ("journal partition", "journal_partition"),
    ("record version", "record_version"),
    ("storage class", "storage_class"),
    ("integrity seal", "integrity_seal"),
    ("short fingerprint", "short_fingerprint"),
    ("checksum prefix", "checksum_prefix"),
    ("preferred contact", "preferred_contact"),
    ("billing status", "billing_status"),
    ("order date", "order_date"),
    ("register month", "register_month"),
    ("classification", "classification"),
    ("dispatched", "dispatched"),
    ("delivered", "delivered"),
    ("reference", "reference"),
    ("statement", "statement_reference"),
    ("received", "received_on"),
    ("customer", "customer_name"),
    ("payment", "payment"),
    ("product", "source_product"),
    ("uploader", "uploader"),
    ("carrier", "carrier"),
    ("ticket", "ticket_reference"),
    ("region", "region"),
    ("actor", "actor"),
    ("event", "event"),
    ("order", "order_reference"),
    ("owner", "owner"),
    ("file", "file_name"),
]
TIMESTAMP_KEYS = {
    "submitted": "submitted_at",
    "captured": "captured_at",
    "issued": "issued_at",
    "recorded": "recorded_at",
    "retain until": "retention_until",
    "generated": "generated_at",
    "received": "received_at",
}
LABEL_KEYS = {
    "Customer request": "message_excerpt",
    "Item": "item",
    "Product": "product",
    "Timeline": "timeline",
}


def _prefix_info(resource_id):
    rid = (resource_id or "").lower()
    for prefix, system_id, category in PREFIX_TABLE:
        if rid.startswith(prefix):
            return system_id, category
    return None, None


def _iso_date(d, mon, y):
    month = MONTHS.get(mon[:3].lower())
    if month is None:
        return None
    return "%04d-%02d-%02d" % (int(y), month, int(d))


def _iso_ts(d, mon, y, hh, mm):
    day = _iso_date(d, mon, y)
    if day is None:
        return None
    return "%sT%02d:%02d:00Z" % (day, int(hh), int(mm))


def _empty_identifiers():
    return {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}


def _extract_pages(path):
    from pypdf import PdfReader  # imported lazily so other parsers work without pypdf

    reader = PdfReader(path)
    return [(page.extract_text() or "") for page in reader.pages]


def _tokenize(pages):
    """Split page text into header lines and ordered body entries.

    entry kinds: "pair" (label, value), "timestamp" (key, iso, raw), "row" (row_no, cells),
    "text" (orphan line).
    """
    headers = []
    entries = []
    for page_no, text in enumerate(pages, start=1):
        lines = [ln.strip() for ln in text.split("\n")]
        lines = [ln for ln in lines if ln]
        headers.append(lines[:3])
        body = lines[3:]
        current = None
        i = 0
        while i < len(body):
            line = body[i]
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if line in LABELS:
                current = {"kind": "pair", "page": page_no, "label": line, "parts": []}
                entries.append(current)
                i += 1
                continue
            m_head = TS_SPLIT_HEAD_RE.match(line)
            m_tail = TS_SPLIT_TAIL_RE.match(nxt) if m_head else None
            m_one = TS_ONE_LINE_RE.match(line)
            if m_head and m_tail:
                g = m_head.groupdict()
                entries.append({
                    "kind": "timestamp", "page": page_no, "key": g["label"].strip().lower(),
                    "iso": _iso_ts(g["d"], g["mon"], g["y"], g["hh"], m_tail.group("mm")),
                    "raw": "%s %s" % (line, nxt),
                })
                current = None
                i += 2
                continue
            if m_one:
                g = m_one.groupdict()
                entries.append({
                    "kind": "timestamp", "page": page_no, "key": g["label"].strip().lower(),
                    "iso": _iso_ts(g["d"], g["mon"], g["y"], g["hh"], g["mm"]), "raw": line,
                })
                current = None
                i += 1
                continue
            if ROW_NO_RE.match(line) and "|" in nxt:
                entries.append({"kind": "row", "page": page_no, "row_no": int(line),
                                "cells": [c.strip() for c in nxt.split("|")]})
                current = None
                i += 2
                continue
            if "|" in line and current is None:
                entries.append({"kind": "row", "page": page_no, "row_no": None,
                                "cells": [c.strip() for c in line.split("|")]})
                i += 1
                continue
            if current is not None:
                current["parts"].append(line)
            else:
                entries.append({"kind": "text", "page": page_no, "value": line})
            i += 1
    for e in entries:
        if e["kind"] == "pair":
            e["value"] = " ".join(e.pop("parts")).strip()
    return headers, entries


def _split_key(value):
    low = value.lower()
    for phrase, key in VALUE_KEYS:
        if low.startswith(phrase + " ") and len(value) > len(phrase) + 1:
            return key, value[len(phrase) + 1:].strip()
    return None, value


def _collect_facts(headers, entries):
    """Turn entries into (identifiers, attrs, resource ids, register rows)."""
    identifiers = _empty_identifiers()
    attrs = {}
    rows = []
    unkeyed = []
    for e in entries:
        kind = e["kind"]
        if kind == "timestamp":
            key = TIMESTAMP_KEYS.get(e["key"], re.sub(r"\W+", "_", e["key"]).strip("_") + "_at")
            attrs.setdefault(key, e["iso"])
            continue
        if kind == "row":
            rows.append(e)
            continue
        value = e["value"]
        if not value:
            continue
        label = e.get("label")
        if label in LABEL_KEYS:
            attrs.setdefault(LABEL_KEYS[label], value)
            continue
        if label == "Delivery address":
            attrs["contains_delivery_address"] = True
            continue
        key, rest = _split_key(value)
        if key == "subject_reference":
            attrs.setdefault("subject_reference", rest)
            if USER_ID_RE.match(rest) and identifiers["user_id"] is None:
                identifiers["user_id"] = rest
        elif key == "customer_name":
            if NAME_RE.match(rest) and identifiers["name"] is None:
                identifiers["name"] = rest
            else:
                attrs.setdefault("customer", rest)
        elif key == "payment":
            attrs["contains_payment_details"] = True
        elif key == "received_on":
            m = DATE_ONLY_RE.match(rest)
            attrs.setdefault("received_on", _iso_date(m.group("d"), m.group("mon"), m.group("y")) if m else rest)
        elif key == "order_date":
            m = DATE_ONLY_RE.match(rest)
            attrs.setdefault("order_date", _iso_date(m.group("d"), m.group("mon"), m.group("y")) if m else rest)
        elif key is not None:
            attrs.setdefault(key, rest)
        elif USER_ID_RE.match(value):
            if identifiers["user_id"] is None:
                identifiers["user_id"] = value
        elif EMAIL_RE.match(value):
            if identifiers["email"] is None:
                identifiers["email"] = value
        elif PHONE_RE.match(value):
            if identifiers["phone"] is None:
                identifiers["phone"] = value
        elif EMAIL_HASH_RE.match(value):
            if identifiers["email_hash"] is None:
                identifiers["email_hash"] = value
        elif SIZE_RE.match(value):
            attrs.setdefault("file_size", value)
        elif label == "Detail" and NAME_RE.match(value) and identifiers["name"] is None:
            identifiers["name"] = value
        else:
            unkeyed.append((label, value))
    for label, value in unkeyed:
        if label == "Record" and "record_title" not in attrs:
            attrs["record_title"] = value
        else:
            attrs.setdefault("notes", []).append(value)

    # Reference "CS-4827 / support_001": keep the non-canonical part separately.
    ref = attrs.get("reference")
    if ref:
        others = [p.strip() for p in ref.split("/") if p.strip() and not RESOURCE_RE.fullmatch(p.strip())]
        if others:
            attrs["external_reference"] = others[0]

    text_pool = [h for page in headers for h in page]
    text_pool += [e.get("value", "") for e in entries if e["kind"] in ("pair", "text")]
    resource_ids = []
    for chunk in text_pool:
        for rid in RESOURCE_RE.findall(chunk):
            if rid not in resource_ids:
                resource_ids.append(rid)
    return identifiers, attrs, resource_ids, rows


def _support_category(attrs, full_text):
    low = full_text.lower()
    if attrs.get("file_name") or "attachment" in low:
        return "SUPPORT_ATTACHMENT"
    return "SUPPORT_MESSAGE"


def _record(file_id, observed_at, system_id, rid, category, identifiers, attrs, location,
            present=True, status=None):
    return {
        "kind": "record",
        "file_id": file_id,
        "observed_at": observed_at,
        "system_id": system_id,
        "resource_id": rid,
        "category": category,
        "present": present,
        "status": status,
        "identifiers": identifiers,
        "lookup_key": None,
        "attrs": attrs,
        "location": location,
    }


def _register_records(file_id, observed_at, rows, base_attrs):
    out = []
    for e in rows:
        cells = e["cells"]
        identifiers = _empty_identifiers()
        attrs = dict(base_attrs)
        attrs["register_row"] = e["row_no"]
        rid = None
        if len(cells) == 7 and ORDER_RE.match(cells[0]):
            order, subject, reason, date, amount, retain, owner = cells
            rid = order
            attrs["order_reference"] = order
            attrs["resource_reference_kind"] = "order_reference"
            attrs["has_canonical_resource_id"] = False
            if USER_ID_RE.match(subject):
                identifiers["user_id"] = subject
            attrs["subject_reference"] = subject
            attrs["retention_reason"] = reason
            m = DATE_ONLY_RE.match(date)
            attrs["invoice_date"] = _iso_date(m.group("d"), m.group("mon"), m.group("y")) if m else date
            attrs["amount"] = amount
            m = re.match(r"^retain until\s+(.*)$", retain, re.I)
            retain_val = m.group(1).strip() if m else retain
            m = DATE_ONLY_RE.match(retain_val)
            attrs["retention_until"] = _iso_date(m.group("d"), m.group("mon"), m.group("y")) if m else retain_val
            attrs["finance_owner"] = owner
        else:
            attrs["row_cells"] = cells
            for c in cells:
                if rid is None and (RESOURCE_RE.fullmatch(c) or ORDER_RE.match(c)):
                    rid = c
                if identifiers["user_id"] is None and USER_ID_RE.match(c):
                    identifiers["user_id"] = c
            if rid is None:
                continue
        canonical_sys, canonical_cat = _prefix_info(rid)
        out.append(_record(
            file_id, observed_at, canonical_sys or "legal_service", rid,
            canonical_cat or "LEGAL_HOLD_RECORD", identifiers, attrs,
            "page %d row %s" % (e["page"], e["row_no"] if e["row_no"] is not None else "?"),
        ))
    return out


def parse(path, file_id, captured_at):
    pages = _extract_pages(path)
    headers, entries = _tokenize(pages)
    identifiers, attrs, resource_ids, rows = _collect_facts(headers, entries)

    title = headers[0][0] if headers and headers[0] else None
    subtitle = headers[0][1] if headers and len(headers[0]) > 1 else None
    base = {"source_format": "pdf_record_detail", "document_title": title, "document_subtitle": subtitle,
            "page_count": len(pages)}
    observed_at = (attrs.get("captured_at") or attrs.get("issued_at") or attrs.get("generated_at")
                   or captured_at)
    full_text = "\n".join(pages)
    location = "pages 1-%d" % len(pages) if len(pages) > 1 else "page 1"

    if rows:
        doc_attrs = dict(base)
        for k, v in attrs.items():
            doc_attrs["register_" + k if k in ("owner", "classification", "notes") else k] = v
        return _register_records(file_id, observed_at, rows, doc_attrs)

    out = []
    if resource_ids:
        for rid in resource_ids:
            system_id, category = _prefix_info(rid)
            if category is None and system_id == "support_platform":
                category = _support_category(attrs, full_text)
            elif category is None and system_id == "object_storage":
                category = "SUPPORT_ATTACHMENT" if attrs.get("file_name") else None
            rec_attrs = dict(base)
            rec_attrs.update(attrs)
            if identifiers["email"] and category == "SUPPORT_ATTACHMENT":
                rec_attrs["owner_email"] = identifiers["email"]
            out.append(_record(file_id, observed_at, system_id, rid, category,
                               dict(identifiers), rec_attrs, location))
        return out

    # No canonical resource id: key the record by the document's own reference.
    rid = None
    kind = None
    for key in ("statement_reference", "reference", "ticket_reference", "order_reference"):
        if attrs.get(key):
            rid, kind = attrs[key], key
            break
    if rid is None and subtitle:
        m = re.search(r"\b([A-Z]{2,}-[A-Z0-9-]+)\b", subtitle)
        if m:
            rid, kind = m.group(1), "subtitle_reference"
    if rid is None:
        return out
    rec_attrs = dict(base)
    rec_attrs.update(attrs)
    rec_attrs["resource_reference_kind"] = kind
    rec_attrs["has_canonical_resource_id"] = False
    out.append(_record(file_id, observed_at, None, rid, None, identifiers, rec_attrs, location))
    return out
