"""Parser for application screenshots (dataset/evidence/images/*.png).

One vision-model call per image (code/llm.py, cached on disk) transcribes the visible UI into a fixed,
generic JSON layout (header, summary cards, main table, right-hand detail panel, activity feed).
Everything after that is deterministic: resource ids are taken only from text matching the canonical
prefix pattern, emails are validated against the identity snapshot and audit history, and facts are
grouped into SCHEMA.md record / coverage dicts. Nothing here is specific to an individual file.
"""
import csv
import datetime as _dt
import os
import re
import sys

_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)
import llm  # noqa: E402

_REPO = os.path.dirname(_CODE_DIR)
_DATASET = os.path.join(_REPO, "dataset")

RESOURCE_RE = re.compile(
    r"\b((?:account|search|support|analytics|export|marketing|email_delivery|audit_record|legal_hold|backup"
    r"|tombstone|data_flow|cache|object)_\d+)\b")
RESOURCE_FULL_RE = re.compile(
    r"^(account|search|support|analytics|export|marketing|email_delivery|audit_record|legal_hold|backup"
    r"|tombstone|data_flow|cache|object)_\d+$")
USER_RE = re.compile(r"\buser_\d+\b")
USER_FULL_RE = re.compile(r"^user_\d+$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"^\+?\d[\d\s().\-]{6,}\d$")
FILE_RE = re.compile(r"^[\w.\-]+\.(?:pdf|jpe?g|png|gif|zip|csv|json|txt|docx?|xlsx?|tar|gz|zst|eml)$", re.I)
ORDER_RE = re.compile(r"\bORD-\d+\b")
TICKET_RE = re.compile(r"\bCS-\d+\b")

PREFIX_TABLE = [  # longest prefixes first
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
    ("object_", "object_storage", None),
]

ABSENT_STATUSES = {"NOT_FOUND", "DELETED", "PURGED", "REMOVED", "ABSENT", "ERASED", "DESTROYED", "404"}
ACTIVE_WORDS = {"healthy", "active", "running", "continuous", "continuous sync", "live", "enabled", "syncing",
                "streaming", "on"}
INACTIVE_WORDS = {"disabled", "paused", "stopped", "inactive", "off", "suspended", "halted"}
TRUE_WORDS = {"yes", "true", "enabled", "allowed", "on", "active", "y"}
FALSE_WORDS = {"no", "false", "disabled", "blocked", "off", "inactive", "n", "denied", "not allowed"}
NULL_CELLS = {"", "—", "–", "-", "--", "n/a", "none", "null", "redacted", "[redacted]", "***"}
NAME_LABELS = {"name", "display name", "display_name", "customer", "customer name", "full name", "contact"}
IDENTIFIER_LABELS = {"email", "e-mail", "email address", "phone", "phone number", "mobile", "name", "display name",
                     "display_name", "customer name", "full name", "customer reference", "customer_reference",
                     "user", "user id", "user_id", "subject", "subject reference", "_id", "id"}
USER_LABEL_HINTS = ("user", "subject", "customer reference", "customer_reference", "customer key")

SYSTEM_PROMPT = (
    "You are a precise OCR and user-interface transcription engine. You read one screenshot of an internal "
    "business web application and transcribe the visible text into JSON. Copy every value character for "
    "character exactly as displayed: identifiers made of a word and digits joined by an underscore, user "
    "references, email addresses, phone numbers, dates, times, file names and paths. Never guess, complete, "
    "normalize, translate or invent text. Use null for anything that is not visible. Reply with JSON only."
)

USER_PROMPT = """Transcribe this application screenshot into ONE JSON object with exactly these keys:
{
  "app_name": "product name shown in the top bar, left of the search box",
  "search_box": "text typed in the top search box, or null when it only shows a grey placeholder starting with 'Search'",
  "breadcrumb": "the breadcrumb line above the main heading",
  "page_title": "the large main heading",
  "page_badge": "text of the small rounded badge right of the main heading, or null",
  "page_subtitle": "the small grey line directly under the main heading, copied exactly with its ' · ' separators",
  "subject_person_name": "the person's full name when the main heading is a person's name, otherwise null",
  "cards": [{"label": "small grey label of a summary card", "value": "large value of that card"}],
  "table": {
    "title": "title of the large table panel",
    "count_text": "small text at the top right of that panel, for example 'Updated moments ago · 3 records'",
    "columns": ["every column header, left to right"],
    "rows": [["one string per column, in column order"]]
  },
  "detail_panel": {
    "title": "heading of the panel on the right side",
    "fields": [{"label": "grey label", "value": "value next to it"}]
  },
  "activity": [{"when": "bold first line of an Activity item", "text": "grey second line"}]
}
Rules:
- Include EVERY summary card, EVERY table row (they are numbered 1, 2, 3 ... at the left; do not copy those numbers), EVERY column, EVERY right-panel field and EVERY activity item, top to bottom.
- Copy identifiers exactly, digit by digit (for example word_123, word_word_007, user_04, ORD-2026, CS-1234). Copy each email address and phone number exactly, letter by letter.
- A cell that shows only a dash stays "—". A value that wraps onto several lines is joined into one string.
- Do not add explanations or keys that are not listed above."""


# ----------------------------------------------------------------------------------------------- helpers

def _s(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v or None


def _is_null(v):
    return v is None or v.strip().lower() in NULL_CELLS


def _snake(label):
    out = re.sub(r"[^0-9a-z]+", "_", (label or "").lower()).strip("_")
    return out or "field"


def _upper_snake(v):
    out = re.sub(r"[^0-9A-Za-z]+", "_", v).strip("_").upper()
    return out or None


def _lev(a, b, cap):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


_KNOWN = None


def _known():
    """Emails and phones from the identity snapshot and identity audit history (participant-facing)."""
    global _KNOWN
    if _KNOWN is not None:
        return _KNOWN
    emails, phones, users = {}, {}, set()
    try:
        with open(os.path.join(_DATASET, "user_identities.csv"), encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                users.add((row.get("user_id") or "").strip())
                if row.get("email"):
                    emails.setdefault(row["email"].strip().lower(), row["email"].strip())
                if row.get("phone"):
                    phones.setdefault(re.sub(r"\D", "", row["phone"]), row["phone"].strip())
        with open(os.path.join(_DATASET, "user_audit_log.csv"), encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                field = (row.get("field_name") or "").strip()
                for key in ("old_value", "new_value"):
                    val = (row.get(key) or "").strip()
                    if not val:
                        continue
                    if field == "email":
                        emails.setdefault(val.lower(), val)
                    elif field == "phone":
                        phones.setdefault(re.sub(r"\D", "", val), val)
    except OSError:
        pass
    _KNOWN = {"emails": emails, "phones": phones, "users": users}
    return _KNOWN


def _validate_email(raw):
    """Return (email or None, note or None). Snap to a unique known email within edit distance 2."""
    if raw is None:
        return None, None
    m = EMAIL_RE.search(raw)
    if not m:
        return None, None
    cand = m.group(0).strip(".")
    known = _known()["emails"]
    if not known:
        return cand, "email_not_checked"
    if cand.lower() in known:
        return known[cand.lower()], None
    best, best_d = [], 3
    for low, orig in known.items():
        d = _lev(cand.lower(), low, 2)
        if d < best_d:
            best, best_d = [orig], d
        elif d == best_d and d <= 2:
            best.append(orig)
    if best_d <= 2 and len(best) == 1:
        return best[0], "snapped_from:" + cand
    return None, "unverified:" + cand


def _validate_phone(raw):
    if raw is None or not PHONE_RE.match(raw):
        return None, None
    digits = re.sub(r"\D", "", raw)
    phones = _known()["phones"]
    if digits in phones:
        return phones[digits], None
    best, best_d = [], 2
    for d_known, orig in phones.items():
        d = _lev(digits, d_known, 1)
        if d < best_d:
            best, best_d = [orig], d
        elif d == best_d and d <= 1:
            best.append(orig)
    if best_d <= 1 and len(best) == 1:
        return best[0], "snapped_from:" + raw
    return raw, "unverified"


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


def _iso(v):
    """ISO timestamp for values that carry a full date (with year); None otherwise."""
    if not v:
        return None
    t = v.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?Z?$", t)
    if m:
        y, mo, d, hh, mi, ss = m.groups()
        if hh is None:
            return "%s-%s-%s" % (y, mo, d)
        return "%s-%s-%sT%s:%s:%sZ" % (y, mo, d, hh, mi, ss or "00")
    m = re.match(r"^(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?(?: ?(UTC|Z|GMT))?$", t)
    if m:
        d, mon, y, hh, mi, ss, tz = m.groups()
        mo = _MONTHS.get(mon.lower())
        if not mo:
            return None
        try:
            _dt.date(int(y), mo, int(d))
        except ValueError:
            return None
        if hh is None:
            return "%s-%02d-%02d" % (y, mo, int(d))
        if tz is None:
            return None  # time without an explicit zone: keep raw only
        return "%s-%02d-%02dT%02d:%s:%sZ" % (y, mo, int(d), int(hh), mi, ss or "00")
    return None


def _bool(v):
    if v is None:
        return None
    low = v.strip().lower()
    if low in TRUE_WORDS:
        return True
    if low in FALSE_WORDS:
        return False
    return None


def _system_for(rid):
    for prefix, system, category in PREFIX_TABLE:
        if rid.startswith(prefix):
            return system, category
    return None, None


def _ids_in(text):
    return RESOURCE_RE.findall(text or "")


def _norm_pairs(items, k1, k2):
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            a, b = _s(it.get(k1)), _s(it.get(k2))
            if a is None and b is None:
                continue
            out.append((a or "", b))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            out.append((_s(it[0]) or "", _s(it[1])))
    return out


def _normalize(raw):
    """Coerce the model JSON into a predictable shape (missing pieces become empty)."""
    raw = raw if isinstance(raw, dict) else {}
    table = raw.get("table")
    if isinstance(table, list):
        table = table[0] if table and isinstance(table[0], dict) else {}
    table = table if isinstance(table, dict) else {}
    cols = [(_s(c) or "") for c in (table.get("columns") or []) if not isinstance(c, (dict, list))]
    rows = []
    for r in table.get("rows") or []:
        if isinstance(r, dict):
            if not cols:
                cols = [_s(k) or "" for k in r.keys()]
            cells = [_s(r.get(c)) for c in cols]
        elif isinstance(r, list):
            cells = [_s(c) if not isinstance(c, (dict, list)) else None for c in r]
        else:
            continue
        if cells and cells[0] and re.match(r"^\d{1,2}$", cells[0]) and len(cells) == len(cols) + 1:
            cells = cells[1:]  # a transcribed row number
        if any(c for c in cells):
            rows.append(cells)
    panel = raw.get("detail_panel") if isinstance(raw.get("detail_panel"), dict) else {}
    app_name, crumb = _s(raw.get("app_name")), _s(raw.get("breadcrumb"))
    crumbs = [c.strip() for c in (crumb or "").split("/") if c.strip()]
    if len(crumbs) >= 2 and (app_name is None or app_name == crumbs[0]):
        app_name = crumbs[1]  # breadcrumb is "<company> / <application> / <page>"
    return {
        "app_name": app_name,
        "search_box": _s(raw.get("search_box")),
        "breadcrumb": _s(raw.get("breadcrumb")),
        "page_title": _s(raw.get("page_title")),
        "page_badge": _s(raw.get("page_badge")),
        "page_subtitle": _s(raw.get("page_subtitle")),
        "person_name": _s(raw.get("subject_person_name")),
        "cards": _norm_pairs(raw.get("cards"), "label", "value"),
        "table_title": _s(table.get("title")),
        "count_text": _s(table.get("count_text")),
        "columns": cols,
        "rows": rows,
        "panel_title": _s(panel.get("title")),
        "panel_fields": _norm_pairs(panel.get("fields"), "label", "value"),
        "activity": _norm_pairs(raw.get("activity"), "when", "text"),
    }


# ----------------------------------------------------------------------------------------------- entities

class _Entity:
    def __init__(self, rid, display_key, location, order):
        self.rid = rid
        self.display_key = display_key
        self.location = location
        self.order = order
        self.facts = []          # (label, value) in display order
        self.tokens = []         # unlabeled header tokens (subtitle pieces)
        self.person_name = None
        self.badge = None
        self.content_rows = []   # rows of a table that belongs to this entity
        self.content_cols = []
        self.activity = []
        self.panel_activity = []
        self.mentions = []
        self.activity_mentions = []
        self.lookup_key = None
        self.lookup_note = None
        self.extra = {}


def _subtitle_tokens(text):
    return [t.strip() for t in re.split(r"\s+[·•|]\s+", text or "") if t.strip()]


def _search_lookup(query):
    """Key used by a typed search query such as 'email:someone@x.com' or 'customer_reference:user_1'."""
    if not query or query.lower().startswith("search"):
        return None, None
    value = query.split(":", 1)[1].strip() if re.match(r"^[\w.]+\s*:", query) else query.strip()
    value = value.strip("\"'") or None
    if value and "@" in value:
        email, note = _validate_email(value)
        return email, note
    return value, None


def _build(n, file_id, captured_at):
    known_numeric = re.compile(r"\d")
    entities, by_rid = [], {}

    def get(rid, display_key, location):
        key = rid or ("display:" + (display_key or location))
        if key in by_rid:
            return by_rid[key]
        e = _Entity(rid, display_key, location, len(entities))
        entities.append(e)
        by_rid[key] = e
        return e

    page_text_values = {(v or "").lower() for _, v in n["cards"] + n["panel_fields"]}

    # 1. page header subject: resource id in heading / subtitle, or a bare user reference as the heading
    header_ids = _ids_in(" ".join(x for x in (n["page_title"], n["page_subtitle"]) if x))
    header = None
    if header_ids:
        header = get(header_ids[0], None, "page header")
        header.mentions.extend(i for i in header_ids[1:])
    elif n["page_title"] and USER_FULL_RE.match(n["page_title"]):
        header = get(None, n["page_title"], "page header")
    if header is not None:
        header.person_name = n["person_name"]
        if n["page_badge"] and not known_numeric.search(n["page_badge"]) \
                and n["page_badge"].lower() not in page_text_values:
            header.badge = n["page_badge"]
        header.extra["page_badge"] = n["page_badge"]
        if n["page_title"] and n["page_title"] != n["person_name"]:
            header.extra["page_title"] = n["page_title"]
        for tok in _subtitle_tokens(n["page_subtitle"]):
            if tok not in header_ids:
                header.tokens.append(tok)
        header.facts.extend(n["cards"])

    # 2. main table rows
    cols = n["columns"]
    row_ids = [(_ids_in(" ".join(c for c in r if c)) or [None])[0] for r in n["rows"]]
    rows_with_ids = sum(1 for x in row_ids if x)
    row_entities = []
    if rows_with_ids:
        for idx, (cells, rid) in enumerate(zip(n["rows"], row_ids), 1):
            if not rid:
                continue
            e = get(rid, None, "table '%s' row %d" % (n["table_title"] or "", idx))
            for ci, cell in enumerate(cells):
                label = cols[ci] if ci < len(cols) else "column_%d" % (ci + 1)
                if cell is not None and cell != rid:
                    e.facts.append((label, cell))
                for other in _ids_in(cell or ""):
                    if other != rid and other not in e.mentions:
                        e.mentions.append(other)
            row_entities.append(e)
    elif header is not None:
        header.content_rows = n["rows"]
        header.content_cols = cols
    else:
        # no resource ids anywhere in the table: one id-less record per row, keyed by its first cell
        for idx, cells in enumerate(n["rows"], 1):
            key = next((c for c in cells if c and not _is_null(c)), None)
            if not key:
                continue
            e = get(None, key, "table '%s' row %d" % (n["table_title"] or "", idx))
            for ci, cell in enumerate(cells):
                label = cols[ci] if ci < len(cols) else "column_%d" % (ci + 1)
                if cell is not None:
                    e.facts.append((label, cell))
            row_entities.append(e)

    # 3. right-hand detail panel: belongs to the resource / row it names, else to the page subject
    panel_ids = _ids_in(n["panel_title"] or "")
    panel_owner = None
    if panel_ids:
        panel_owner = get(panel_ids[0], None, "detail panel '%s'" % n["panel_title"])
    elif n["panel_title"]:
        for e in row_entities:
            if e.display_key and e.display_key == n["panel_title"]:
                panel_owner = e
                break
    if panel_owner is None and header is not None:
        panel_owner = header
    if panel_owner is not None:
        for label, value in n["panel_fields"]:
            if value is not None and value != panel_owner.rid:
                panel_owner.facts.append(("panel: " + label if panel_owner is header and n["panel_title"]
                                          and not panel_ids else label, value))
            for other in _ids_in(value or ""):
                if other != panel_owner.rid and other not in panel_owner.mentions:
                    panel_owner.mentions.append(other)
        if n["panel_title"] and panel_owner is header:
            panel_owner.extra["detail_panel_title"] = n["panel_title"]

    # 4. activity feed: each item to every record it names; the whole feed to the panel subject
    for when, text in n["activity"]:
        item = ("%s: %s" % (when, text)) if when and text else (when or text)
        named = set(_ids_in(item))
        for e in entities:
            if (e.rid and e.rid in named) or (e.display_key and e.display_key in item and e is not header):
                e.activity.append(item)
        if panel_owner is not None:
            panel_owner.panel_activity.append(item)
            for other in sorted(named, key=item.index):
                if other != panel_owner.rid and other not in panel_owner.mentions and other not in by_rid \
                        and other not in panel_owner.activity_mentions:
                    panel_owner.activity_mentions.append(other)

    # 5. search query context for result rows
    lookup, lookup_note = _search_lookup(n["search_box"])
    if lookup is not None or lookup_note:
        for e in row_entities:
            e.lookup_key = lookup
            e.lookup_note = lookup_note

    records = [_record(e, n, file_id, captured_at, header) for e in entities]
    coverage = _coverage(n, row_ids, file_id, captured_at)
    return records + coverage


def _record(e, n, file_id, captured_at, header):
    system_id, category = _system_for(e.rid) if e.rid else (None, None)
    ident = {"user_id": None, "email": None, "phone": None, "name": None, "email_hash": None}
    attrs = {}
    notes = []

    def take_email(label, value):
        email, note = _validate_email(value)
        if note:
            notes.append("%s %s" % (label or "value", note))
        if email:
            if ident["email"] is None:
                ident["email"] = email
            elif email.lower() != ident["email"].lower():
                attrs.setdefault("other_emails", []).append(email)

    def take_value(label, value):
        if _is_null(value):
            return
        low = (label or "").lower()
        if EMAIL_RE.search(value) and "@" in value:
            take_email(label, value)
        if USER_FULL_RE.match(value) and (not label or any(h in low for h in USER_LABEL_HINTS) or low == ""):
            if ident["user_id"] is None:
                ident["user_id"] = value
        elif PHONE_RE.match(value) and (not label or "phone" in low or "mobile" in low or value.startswith("+")):
            phone, note = _validate_phone(value)
            if note:
                notes.append("phone %s" % note)
            if phone and ident["phone"] is None:
                ident["phone"] = phone
        elif low in NAME_LABELS and not EMAIL_RE.search(value) and not _ids_in(value) \
                and not re.search(r"\d", value) and not FILE_RE.match(value):
            if ident["name"] is None:
                ident["name"] = value

    for tok in e.tokens:
        take_value("", tok)
        if not (EMAIL_RE.search(tok) or USER_FULL_RE.match(tok) or PHONE_RE.match(tok)):
            attrs.setdefault("subtitle_tokens", []).append(tok)
    for label, value in e.facts:
        take_value(label.split(": ", 1)[-1] if label.startswith("panel: ") else label, value)
    if e.rid is None and e.display_key and USER_FULL_RE.match(e.display_key):
        ident["user_id"] = e.display_key
    if e.person_name and not re.search(r"\d|@", e.person_name):
        ident["name"] = e.person_name

    # facts -> attrs (raw), plus normalized well-known keys; pure identifier fields live in `identifiers`
    for label, value in e.facts:
        if _is_null(value):
            continue
        base = label.split(": ", 1)[-1].lower().strip()
        if base in IDENTIFIER_LABELS:
            continue
        if "@" in value and EMAIL_RE.search(value):
            email, _ = _validate_email(value)
            if email and email.lower() != EMAIL_RE.search(value).group(0).lower():
                value = value.replace(EMAIL_RE.search(value).group(0), email)
        key = _snake(label)
        if key in attrs and attrs[key] != value:
            i = 2
            while "%s_%d" % (key, i) in attrs:
                i += 1
            key = "%s_%d" % (key, i)
        attrs[key] = value
    for label, value in e.facts:
        low = label.lower().split(": ", 1)[-1]
        iso = _iso(value)
        if re.search(r"retention|retain until|retained until|keep until", low):
            attrs["retention_until"] = iso or value
        elif re.search(r"legal hold until|hold until", low):
            attrs["legal_hold_until"] = iso or value
        elif re.search(r"legal hold|hold status", low):
            attrs["hold_status"] = value
        elif re.search(r"expire", low):
            attrs["expires_at"] = iso or value
        elif re.search(r"restore (allowed|enabled|access)|allow restore", low):
            b = _bool(value)
            if b is not None:
                attrs["restore_allowed"] = b
        elif re.search(r"download (available|enabled)", low):
            b = _bool(value)
            if b is not None:
                attrs["download_available"] = b
        elif re.search(r"archive present", low):
            b = _bool(value)
            if b is not None:
                attrs["archive_present"] = b
        elif low in ("enabled", "active", "toggle", "sync enabled"):
            b = _bool(value)
            if b is not None:
                attrs["enabled"] = b
        elif low in ("selector", "filter", "match"):
            attrs["selector"] = value
        elif low in ("connector", "connector name"):
            attrs["connector_name"] = value
        elif low in ("_index", "index", "index name"):
            attrs["index_name"] = value
        elif low in ("file", "file name", "filename", "name") and FILE_RE.match(value or ""):
            attrs["file_name"] = value
        elif low in ("account status",):
            attrs["account_status"] = value
        elif low in ("source", "source resource", "source resource id"):
            attrs["source_resource_id"] = value
        elif low in ("http status", "response", "last response"):
            attrs["http_status"] = value
        if re.search(r"(^| )(created|recorded_at|indexed_at|modified|last indexed|last contact|updated)", low) \
                and iso and "T" in iso:
            attrs.setdefault(_snake(low) + "_iso", iso)

    # status
    status = None
    for label, value in e.facts:
        if label.lower().split(": ", 1)[-1] == "status" and not _is_null(value):
            status = _upper_snake(value)
            break
    if status is None and e.badge:
        status = _upper_snake(e.badge)
    present = not (status in ABSENT_STATUSES)

    # page context
    attrs["app_name"] = n["app_name"]
    if n["breadcrumb"]:
        attrs["breadcrumb"] = n["breadcrumb"]
    if e is not header:
        attrs["page_title"] = n["page_title"]
        if n["page_subtitle"]:
            attrs["page_subtitle"] = n["page_subtitle"]
        if n["page_badge"]:
            attrs["page_badge"] = n["page_badge"]
        if n["cards"]:
            attrs["page_cards"] = {lab: val for lab, val in n["cards"]}
        if n["table_title"] and e.location.startswith("table"):
            attrs["table"] = n["table_title"]
        for lab, val in n["cards"]:
            if lab.lower() in ("index", "index name") and "index_name" not in attrs:
                attrs["index_name"] = val
    for k, v in e.extra.items():
        if v is not None:
            attrs[k] = v
    if n["search_box"]:
        attrs["search_query"] = n["search_box"]
    if e.display_key:
        attrs["display_key"] = e.display_key
    if e.rid is None:
        attrs["resource_id_not_shown"] = True
    if e.content_rows:
        attrs["table"] = n["table_title"]
        attrs["table_rows"] = [
            {(e.content_cols[i] if i < len(e.content_cols) else "column_%d" % (i + 1)): c
             for i, c in enumerate(r)} for r in e.content_rows]
        msg_col = next((i for i, c in enumerate(e.content_cols) if "message" in c.lower()), None)
        sender_col = next((i for i, c in enumerate(e.content_cols)
                           if c.lower() in ("sender", "from", "author")), None)
        if msg_col is not None:
            first = next((r[msg_col] for r in e.content_rows if msg_col < len(r) and r[msg_col]), None)
            if first:
                attrs["message_excerpt"] = first
        if sender_col is not None:
            senders = []
            for r in e.content_rows:
                if sender_col < len(r) and r[sender_col] and r[sender_col] not in senders:
                    senders.append(r[sender_col])
            attrs["conversation_participants"] = senders
    if e.activity:
        attrs["activity"] = e.activity
    if e.panel_activity:
        attrs["panel_activity"] = e.panel_activity
    if e.mentions:
        attrs["linked_resource_ids"] = e.mentions
    if e.activity_mentions:
        attrs["activity_resource_ids"] = e.activity_mentions
    if notes:
        attrs["validation_notes"] = notes

    # textual content of this record, for category decisions and shared-context hints
    blob = " ".join([str(v) for _, v in e.facts] + e.tokens + e.activity + [n["page_title"] or "",
                    n["page_subtitle"] or "", n["breadcrumb"] or ""] +
                    [str(c) for r in e.content_rows for c in r if c])
    if system_id == "support_platform":
        names_file = any(FILE_RE.match(str(v or "")) for _, v in e.facts)
        category = "SUPPORT_ATTACHMENT" if (names_file or re.search(r"\battachment", blob, re.I)) \
            and not e.content_rows else "SUPPORT_MESSAGE"
    elif system_id == "analytics_platform" and re.search(r"telemetry", blob, re.I):
        category = "PRODUCT_TELEMETRY"
    elif e.rid is None or system_id == "object_storage":
        name = attrs.get("file_name") or e.display_key or ""
        if re.search(r"export", name, re.I):
            category = "USER_EXPORT"
        elif re.search(r"attachment", blob, re.I) or (FILE_RE.match(name) and re.search(r"support", blob, re.I)):
            category = "SUPPORT_ATTACHMENT"
        elif e.rid is None and e.display_key and USER_FULL_RE.match(e.display_key) \
                and re.search(r"retained|tombstone|deleted", blob, re.I):
            category = "ACCOUNT_TOMBSTONE"
        else:
            category = None
        if e.rid is None:
            attrs["category_basis"] = "content (no resource id on screen)"
    if e.rid is None and e.display_key and FILE_RE.match(e.display_key):
        attrs.setdefault("file_name", e.display_key)
        parts = [p for p in [n["page_title"]] + [t.strip() for t in (n["page_subtitle"] or "").split("/")] if p]
        attrs["object_key"] = "/".join(parts + [e.display_key])

    if system_id == "support_platform":
        hints = {}
        orders = sorted(set(ORDER_RE.findall(blob + " " + (attrs.get("detail_panel_title") or ""))))
        tickets = sorted(set(TICKET_RE.findall(blob)))
        if orders:
            hints["order_refs"] = orders
        if tickets:
            hints["ticket_refs"] = tickets
        agents = [v for lab, v in e.facts if re.search(r"assigned|agent", lab, re.I) and v]
        if agents:
            hints["agents"] = sorted(set(agents))
        others = [p for p in attrs.get("conversation_participants", []) if p != ident["name"]]
        if others:
            hints["other_participants"] = others
        if hints:
            attrs["owner_scope"] = hints
    if system_id == "integration_service" and "enabled" not in attrs:
        words = [str(attrs.get(k, "")).lower() for k in ("status", "mode", "sync_mode")]
        if any(w in ACTIVE_WORDS for w in words):
            attrs["enabled"] = True
            attrs["enabled_basis"] = "status/mode shown on screen"
        elif any(w in INACTIVE_WORDS for w in words):
            attrs["enabled"] = False
            attrs["enabled_basis"] = "status/mode shown on screen"
    if system_id == "export_service" and "download_available" not in attrs and status in ("READY", "AVAILABLE"):
        attrs["download_available"] = True
        attrs["download_available_basis"] = "status shown on screen"
    if system_id == "integration_service" and ident["email"] and "selector" in attrs:
        attrs["selector_email"] = ident["email"]

    return {
        "kind": "record",
        "file_id": file_id,
        "observed_at": captured_at,
        "system_id": system_id,
        "resource_id": e.rid,
        "category": category,
        "present": present,
        "status": status,
        "identifiers": ident,
        "lookup_key": e.lookup_key,
        "attrs": attrs,
        "location": e.location,
    }


def _coverage(n, row_ids, file_id, captured_at):
    """One coverage dict when the main table is a complete, unfiltered list of resources of one system."""
    ids = [r for r in row_ids if r]
    if not ids or len(ids) != len(n["rows"]):
        return []
    systems = {_system_for(r)[0] for r in ids}
    if len(systems) != 1:
        return []
    if n["search_box"] and not n["search_box"].lower().startswith("search"):
        return []  # query result, not a full list
    context = " ".join(x for x in (n["page_subtitle"], n["page_badge"], n["table_title"]) if x).lower()
    if re.search(r"\bfilter|\bquery|\bhits?\b|\bresults?\b|\bmatch", context):
        return []
    for text in (n["page_badge"], n["count_text"]):
        m = re.search(r"(\d[\d,]*)\s+(?!ms\b)[a-z]", (text or "").lower())
        if m and int(m.group(1).replace(",", "")) != len(n["rows"]):
            return []  # the list shown is only part of a larger collection
    return [{
        "kind": "coverage",
        "file_id": file_id,
        "observed_at": captured_at,
        "system_id": systems.pop(),
        "table": "%s / %s" % (n["app_name"] or "console", n["table_title"] or "table"),
        "replica_role": "primary",
        "resource_ids": ids,
    }]


# ----------------------------------------------------------------------------------------------- entry

def extract(path):
    """Raw (cached) model transcription of one screenshot."""
    return llm.chat_json(purpose="image_extract", system=SYSTEM_PROMPT, user=USER_PROMPT, images=[path],
                         max_tokens=4096)


def parse(path, file_id, captured_at):
    try:
        raw = extract(path)
    except Exception as exc:  # model unavailable and nothing cached: no observations from this image
        sys.stderr.write("images.parse: no transcription for %s (%s)\n" % (os.path.basename(path), exc))
        return []
    return _build(_normalize(raw), file_id, captured_at)


if __name__ == "__main__":
    import json
    for p in sys.argv[1:]:
        print(json.dumps(parse(p, "evidence_x", "1970-01-01T00:00:00Z"), indent=1, ensure_ascii=False))
