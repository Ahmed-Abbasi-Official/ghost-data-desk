"""Phases 3-5 for one case: discover the user's resources, rebuild their state at audit_at,
apply deletion_rules.csv and detect recreation risks. Produces one decision per resource."""
from collections import defaultdict

from common import prefix_info, read_csv

REPLICA_ROLES = ("failover", "read_replica", "replica", "regional", "reporting")
ACTIVE_WORDS = {"ACTIVE", "ENABLED", "HEALTHY", "RUNNING", "CONTINUOUS"}
INACTIVE_WORDS = {"DISABLED", "PAUSED", "STOPPED", "INACTIVE", "BLOCKED"}
EXPOSED_WORDS = {"IDENTIFIABLE", "EXPOSED"}
CLEAN_WORDS = {"ANONYMIZED", "REDACTED", "DEIDENTIFIED", "DE_IDENTIFIED"}
DIRECT_FIELDS = ("name", "email", "phone", "email_hash")
WORKER_ACTIONS = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}
WORKER_HINT_CATEGORIES = {"SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}


def load_rules():
    rules = {}
    for row in read_csv("deletion_rules.csv"):
        rules[row["data_category"]] = row["required_action"]
    return rules


def _attrs(ob):
    return ob.get("attrs") or {}


def copy_key(ob):
    """Which physical copy an observation describes: the live service or a named replica."""
    role = str(ob.get("replica_role") or _attrs(ob).get("replica_role") or "").lower()
    if role.startswith(REPLICA_ROLES):
        return "replica:%s:%s" % (role, ob.get("table") or _attrs(ob).get("table") or "")
    return "live"


class EvidenceIndex:
    """Observations and activity grouped for fast per-case lookups."""

    def __init__(self, observations, events, identities):
        self.identities = identities
        self.by_resource = defaultdict(list)
        self.coverage_by_system = defaultdict(list)
        self.owner_hits = defaultdict(list)  # user_id -> [(observed_at, captured_at, resource_id, evidence)]
        for ob in observations:
            if ob.get("kind") == "coverage":
                self.coverage_by_system[ob.get("system_id")].append(ob)
                continue
            rid = ob.get("resource_id")
            if not rid:
                continue
            self.by_resource[rid].append(ob)
            for uid in self.owners(ob):
                self.owner_hits[uid].append((ob["observed_at"], ob["captured_at"], rid, ob["file_id"]))
        for rid in self.by_resource:
            self.by_resource[rid].sort(key=lambda o: (o["observed_at"], o["file_id"]))
        self.events = events
        self.events_by_resource = defaultdict(list)
        for ev in events:
            self.events_by_resource[ev["resource_id"]].append(ev)
            for m in ev["mentioned"]:
                self.events_by_resource[m].append(ev)

    def owners(self, ob):
        """user_ids whose identifiers appear in the record, its lookup key or its attributes."""
        found = set()
        values = list((ob.get("identifiers") or {}).values()) + [ob.get("lookup_key")]
        values += [v for v in _attrs(ob).values() if isinstance(v, str)]
        for v in values:
            uid = self.identities.owner_of(v)
            if uid:
                found.add(uid)
        return found


def visible(ob, audit_at):
    """The record's own observation time decides what the state was at audit_at."""
    return ob["observed_at"] <= audit_at


def citeable(ob, audit_at):
    """Only files captured at or before audit_at may be cited."""
    return ob["observed_at"] <= audit_at and ob["captured_at"] <= audit_at


def _cite(entries, audit_at, present):
    """Latest citeable evidence id whose observation shows the given presence."""
    best = None
    for ob, is_present in entries:
        if is_present == present and citeable(ob, audit_at):
            # A file that names the record beats a snapshot that merely omits it; then the latest one wins.
            rank = (ob.get("kind") == "record", ob["observed_at"], ob["file_id"])
            if best is None or rank > best[0]:
                best = (rank, ob)
    return best[1]["file_id"] if best else None


def discover(case, index, accounts):
    """Resources linked to the case's user using only evidence at or before audit_at."""
    uid, audit_at = case["uid"], case["audit_at"]
    found = {}
    for observed_at, captured_at, rid, file_id in index.owner_hits.get(uid, []):
        if observed_at <= audit_at:
            found.setdefault(rid, "evidence")
    for ev in index.events:
        if ev["kind"] == "link" and ev["time"] <= audit_at:
            if any(index.identities.owner_of(v) == uid for v in ev["emails"] + ev["user_ids"]):
                found.setdefault(ev["resource_id"], "catalog")
    for a in accounts:
        found[a] = "linker"
    for rid in case.get("run_resources", ()):
        found.setdefault(rid, "deletion_run")
    # A connector/restore that repopulated one of the user's resources is itself relevant, and vice versa.
    for ev in index.events:
        if ev["kind"] == "repopulate" and ev["time"] <= audit_at:
            if ev["resource_id"] in found:
                for m in ev["mentioned"]:
                    found.setdefault(m, "repopulated")
            elif any(m in found for m in ev["mentioned"]):
                found.setdefault(ev["resource_id"], "repopulates")
    # Accounts other than the linked primary profile are only kept when evidence names the user directly.
    return {r: why for r, why in found.items() if not (r.startswith("account_") and why != "linker" and accounts)}


def _category(rid, history):
    cats = [o.get("category") for o in history if o.get("category")]
    system_id, default = prefix_info(rid)
    if rid.startswith("support_") and "SUPPORT_ATTACHMENT" in cats:
        return system_id, "SUPPORT_ATTACHMENT"
    if rid.startswith("analytics_") and "PRODUCT_TELEMETRY" in cats:
        return system_id, "PRODUCT_TELEMETRY"
    return system_id, default


def _presence(rid, system_id, history, index, audit_at):
    """Latest state per physical copy at audit_at.

    Returns (present_entry, newest_entry, entries) where an entry is (observation, present?) and entries lists
    every visible observation/omission for citation.
    """
    latest, entries = {}, []
    for ob in history:
        if visible(ob, audit_at):
            entry = (ob, bool(ob.get("present", True)))
            entries.append(entry)
            latest[copy_key(ob)] = entry
    named = {copy_key(e[0]) for e in entries}
    for cov in index.coverage_by_system.get(system_id, []):
        if not visible(cov, audit_at) or rid in (cov.get("resource_ids") or []):
            continue  # when listed, the record observation itself carries the state
        # Full SQL table dumps prove absence by omission; curated bundles/exports only do so for records
        # that no file names explicitly.
        if copy_key(cov) in named and not cov.get("full_table"):
            continue
        entry = (cov, False)
        entries.append(entry)
        prev = latest.get(copy_key(cov))
        if prev is None or prev[0]["observed_at"] < cov["observed_at"]:
            latest[copy_key(cov)] = entry
    order = lambda e: (e[0]["observed_at"], e[0]["file_id"])
    present = [e for e in latest.values() if e[1]]
    newest = max(latest.values(), key=order) if latest else None
    newest_present = max(present, key=order) if present else None
    return newest_present, newest, entries


def _exposed(category, ob, uid, identities):
    if ob is None:
        return False
    status = str(ob.get("status") or "").upper()
    ids = ob.get("identifiers") or {}
    if category == "ACCOUNT_TOMBSTONE":
        return any(ids.get(f) for f in DIRECT_FIELDS)
    if status in EXPOSED_WORDS:
        return True
    if status in CLEAN_WORDS:
        return False
    user = identities.users.get(uid, {})
    for field, value in ids.items():
        if not value:
            continue
        if field == "user_id" and category not in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"):
            continue  # a stable user reference may remain on redacted records
        if identities.owner_of(value) == uid:
            return True
        if field == "name" and str(value).strip().lower() == str(user.get("name", "")).strip().lower():
            return True
    return False


def _flag(history, events, audit_at, attr, words_on, words_off, event_kind, event_attr):
    """Latest evidence value for a boolean control (data flow enabled / restore allowed) -> (value, evidence id)."""
    value, chosen = None, None
    for ob in reversed(history):
        if not visible(ob, audit_at):
            continue
        a = _attrs(ob)
        status = str(ob.get("status") or "").upper()
        if isinstance(a.get(attr), bool):
            value = a[attr]
        elif status in words_on:
            value = True
        elif status in words_off:
            value = False
        else:
            continue
        chosen = ob
        break
    if chosen is not None:
        same = [o for o in history if citeable(o, audit_at) and _flag_value(o, attr, words_on, words_off) == value]
        cite = max(same, key=lambda o: (o["observed_at"], o["file_id"]))["file_id"] if same else ""
        return value, cite
    for ev in sorted(events, key=lambda e: e["time"], reverse=True):
        if ev["time"] <= audit_at and ev["kind"] == event_kind and event_attr in ev:
            return ev[event_attr], ev["activity_id"]
    return None, None


def _flag_value(ob, attr, words_on, words_off):
    a, status = _attrs(ob), str(ob.get("status") or "").upper()
    if isinstance(a.get(attr), bool):
        return a[attr]
    return True if status in words_on else False if status in words_off else None


def decide(case, index, rules, accounts):
    """One decision per relevant resource: required treatment, violation flag and evidence ids."""
    uid, audit_at = case["uid"], case["audit_at"]
    decisions = []
    for rid in sorted(discover(case, index, accounts)):
        history = index.by_resource.get(rid, [])
        events = index.events_by_resource.get(rid, [])
        system_id, category = _category(rid, history)
        if not system_id:
            continue
        if not any(visible(o, audit_at) for o in history) and not any(
                e["time"] <= audit_at and e["kind"] != "link" for e in events):
            continue  # only a catalog mention, no state evidence at all
        d = {"resource_id": rid, "system_id": system_id, "category": category}
        if category == "DATA_FLOW":
            enabled, ev_id = _flag(history, events, audit_at, "enabled", ACTIVE_WORDS, INACTIVE_WORDS, "check_flow", "flow_enabled")
            if enabled is None:
                continue
            d.update(kind="flow", violation=enabled, evidence=ev_id)
        elif category == "BACKUP_ARCHIVE":
            allowed, ev_id = _flag(history, events, audit_at, "restore_allowed", {"ACTIVE", "ALLOWED", "RESTORABLE"},
                                   {"BLOCKED", "DISABLED"}, "check_restore", "restore_allowed")
            if allowed is None:
                continue
            d.update(kind="backup", violation=allowed, evidence=ev_id)
        else:
            action = rules.get(category, "DELETE")
            if category in WORKER_HINT_CATEGORIES:
                # Message vs attachment and attributable analytics are not visible in every copy; the treatment the
                # deletion workflow selected for this record (not its claimed result) disambiguates the category.
                chosen = [e for e in events if e["resource_id"] == rid and e["time"] <= audit_at
                          and e["event_type"] in WORKER_ACTIONS and e["kind"] == "policy_action"]
                if chosen:
                    action = WORKER_ACTIONS[chosen[-1]["event_type"]]
            newest_present, newest, entries = _presence(rid, system_id, history, index, audit_at)
            if action == "RETAIN":
                retain_until = next((str(_attrs(o).get("retention_until")) for o in reversed(history)
                                     if _attrs(o).get("retention_until")), None)
                if retain_until and retain_until < audit_at:
                    action = "DELETE"
            absent_cite = _cite(entries, audit_at, False) or _claim(events, audit_at)
            if action == "RETAIN":
                if newest_present is None:
                    continue
                d.update(kind="record", required="PRESENT", violation=False, evidence=_cite(entries, audit_at, True))
            elif action == "DELETE":
                if newest_present is not None:
                    d.update(kind="record", required="ABSENT", violation=True, evidence=_cite(entries, audit_at, True))
                else:
                    d.update(kind="record", required="ABSENT", violation=False, evidence=absent_cite)
            else:  # REDACT / ANONYMIZE
                required = "REDACTED" if action == "REDACT" else "ANONYMIZED"
                if newest_present is None:
                    d.update(kind="record", required="ABSENT", violation=False, evidence=absent_cite)
                else:
                    ob = newest_present[0]
                    exposed = _exposed(category, ob, uid, index.identities)
                    same = [e for e in entries if e[1] and _exposed(category, e[0], uid, index.identities) == exposed]
                    d.update(kind="record", required=required, violation=exposed, evidence=_cite(same, audit_at, True))
        decisions.append(d)
    return decisions


def _claim(events, audit_at):
    past = [e for e in events if e["time"] <= audit_at]
    return past[-1]["activity_id"] if past else ""


RUN_BEFORE_HOURS, RUN_AFTER_HOURS = 0.25, 0.0  # only actions in the last minutes before a single report


def attribute_runs(cases, index):
    """Give unowned records touched by the deletion worker to the one deletion case whose run covers them.

    Records that carry no identifier anywhere (e.g. already anonymised analytics) can only be tied to a case
    through the deletion run. When more than one case's run covers the action, nothing is attributed.
    """
    from datetime import datetime, timedelta
    t = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    owned = set()
    for rid, obs in index.by_resource.items():
        if any(index.owners(o) for o in obs):
            owned.add(rid)
    for ev in index.events:
        if ev["kind"] == "link":
            owned.add(ev["resource_id"])
    windows = [(c, t(c["reported_at"]) - timedelta(hours=RUN_BEFORE_HOURS),
                t(c["reported_at"]) + timedelta(hours=RUN_AFTER_HOURS)) for c in cases]
    for c in cases:
        c["run_resources"] = []
    for ev in index.events:
        rid = ev["resource_id"]
        if ev["kind"] != "policy_action" or rid.startswith("account_") or rid in owned:
            continue
        when = t(ev["time"])
        # The run belongs to the report that follows it: the earliest reported_at at or after the action.
        hits = [c for c, lo, hi in windows if lo <= when <= t(c["reported_at"]) and ev["time"] <= c["audit_at"]
                and c["requested_at"] <= ev["time"]]
        if hits:
            first = min(c["reported_at"] for c in hits)
            hits = [c for c in hits if c["reported_at"] == first]
        if len(hits) == 1 and rid not in hits[0]["run_resources"]:
            hits[0]["run_resources"].append(rid)
