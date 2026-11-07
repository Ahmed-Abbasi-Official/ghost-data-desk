# Evidence observation schema

Every parser module in `code/ingest/` exposes:

```python
def parse(path: str, file_id: str, captured_at: str) -> list:
    """Return a list of observation dicts for one evidence file."""
```

Python 3.9, standard library only (plus `pypdf` for PDFs). No network. Only `images.py` and `pdf_docs.py` may call
the LLM, and only through `code/llm.py` `chat_json(...)` (cached). Deterministic output order (file order).

Every returned dict has `"kind"`: `"record"` (an observation, below) or `"coverage"` (see end of file).

## Observation dict

```python
{
    "kind": "record",
    "file_id": "evidence_94",
    "observed_at": "2026-09-26T08:57:37Z",  # per-record timestamp when the file has one, else captured_at
    "system_id": "account_service",          # canonical id, see table below
    "resource_id": "account_7566",
    "category": "USER_PROFILE",              # deletion_rules.csv category, or "DATA_FLOW"; None if unknown
    "present": False,                        # does the record exist at observed_at? (404 / rows=0 / NOT_FOUND / missing -> False)
    "status": "NOT_FOUND",                   # normalized UPPER_SNAKE raw status word from the file, or None
    "identifiers": {                         # exactly as they appear in the record; None when null/absent/redacted
        "user_id": None,                     # user_XX / customer_reference / subject_reference / subject id
        "email": None,
        "phone": None,
        "name": None,
        "email_hash": None,
    },
    "lookup_key": "l.fontaine46@yahoo.com",  # key used by a lookup/query that produced this row, if any (else None)
    "attrs": {},                             # every other fact that may matter, see below
    "location": "line 19",                   # provenance: line number, table name, JSON path
}
```

`identifiers` describe what the stored record still exposes. `lookup_key` is only the query key and is
not proof that the record still contains that identifier. Keep them separate.

Put in `attrs` (when available): `enabled` (bool), `restore_allowed` (bool), `archive_present` (bool),
`retention_until`, `legal_hold_until`/`hold_status`, `connector_name`, `last_delivery_status`,
`index_name`, `source_resource_id`, `table`, `replica_role` (primary / replica / failover / regional name /
reporting), `account_status`, `event_code`, `owner_scope` / shared-context hints for support records,
`file_name`, `object_key`, `download_available`, `expires_at`, `message_excerpt`, `http_status`, `rows`,
`result`, and anything else that reads like a state or a linkage.

## Canonical system ids and categories

| resource prefix | system_id | category |
|---|---|---|
| `account_` | `account_service` | `USER_PROFILE` |
| `search_` | `search_service` | `SEARCH_INDEX_ENTRY` |
| `support_` | `support_platform` | `SUPPORT_MESSAGE` or `SUPPORT_ATTACHMENT` (decide from content) |
| `analytics_` | `analytics_platform` | `ANALYTICS_PROFILE` (or `PRODUCT_TELEMETRY` if the record says telemetry) |
| `export_` | `export_service` | `USER_EXPORT` |
| `marketing_` | `marketing_platform` | `MARKETING_PROFILE` |
| `email_delivery_` | `email_delivery_service` | `EMAIL_DELIVERY_LOG` |
| `audit_record_` | `security_service` | `SECURITY_AUDIT_LOG` |
| `legal_hold_` | `legal_service` | `LEGAL_HOLD_RECORD` |
| `backup_` | `backup_service` | `BACKUP_ARCHIVE` |
| `tombstone_` | `identity_service` | `ACCOUNT_TOMBSTONE` |
| `data_flow_` | `integration_service` | `DATA_FLOW` |
| `cache_` | `cache_service` | `CACHE_ENTRY` |
| `object_` | `object_storage` | decide from content |

If a file states a different system for a resource, keep the file's system in `attrs["stated_system"]`
and still use the canonical `system_id`.

## Coverage dict (needed for absence-by-omission)

When a file lists a whole table / collection / store snapshot (SQL table, JSON collection, admin-console list),
also emit one coverage dict per table, even when the table is empty:

```python
{
    "kind": "coverage",
    "file_id": "evidence_54",
    "observed_at": "2026-09-02T23:00:00Z",   # snapshot time of that table
    "system_id": "account_service",          # canonical id of the resources the table holds
    "table": "customer_profiles_failover_replica",
    "replica_role": "failover",              # primary / replica / read_replica / failover / regional:<name> / reporting / snapshot
    "resource_ids": ["account_005", "account_8741"],  # every resource id in the table, file order
}
```

A record missing from a coverage table it would belong to is treated as absent from that copy at observed_at.
Lookup traces that report NOT_FOUND / 404 for one resource are ordinary records with `present: False`.
