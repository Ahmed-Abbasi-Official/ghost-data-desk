"""ghost: the tool the Ghost Data Desk agents run inside the Switch room.

    ghost stats                          how many "completed" deletions still left data behind
    ghost lookup <email>                 which deletion cases belong to this person (current or old email)
    ghost intake <case_id>               is it a deletion request, and who is the person
    ghost audit <case_id>                where the person's data still exists, and the plan to remove it
    ghost link <case_id> <account_id> --confirmed-by NAME
                                         a person picks the account when the evidence is ambiguous
    ghost fix <case_id> --approved-by NAME [--skip 3,5]
                                         run the approved remediation steps (sandbox estate)
    ghost verify <case_id>               independent re-check; writes a deletion certificate if it passes
    ghost certificate <case_id>          print the certificate of a verified case
    ghost reply <case_id> [--approved-by NAME]
                                         draft the answer to the customer; with a person's approval, queue it
                                         and close the case
    ghost scan [--reported STATUS] [--top N]
                                         audit every request at once and list the cases that need work first
    ghost status [case_id]               where every case is in the workflow

Every command prints Markdown that an agent can post to the room as it is. Emails and phone numbers are
masked, because the room is a team chat.
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
os.environ.setdefault("GHOST_DATASET_DIR", os.path.join(ROOT, "dataset"))

import classify  # noqa: E402
import linker  # noqa: E402
import planner  # noqa: E402
import state as engine_state  # noqa: E402
from common import DATASET, case_sort_key, prefix_info, read_csv  # noqa: E402
from main import load_context, load_requests  # noqa: E402

STATE_DIR = os.environ.get("GHOST_STATE_DIR", os.path.join(ROOT, "state"))
CASES_DIR = os.path.join(STATE_DIR, "cases")
CERTS_DIR = os.path.join(STATE_DIR, "certificates")
LINKS_FILE = os.path.join(STATE_DIR, "account_links.json")

TARGET = {  # what each remediation leaves behind
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
}
VERB = {
    "DISABLE_DATA_FLOW": "Disable the data flow", "BLOCK_RESTORE": "Block restores from the backup",
    "DELETE_RECORD": "Delete the record", "REDACT_RECORD": "Redact the personal data",
    "ANONYMIZE_RECORD": "Anonymize the record",
}
FINDING_TEXT = {
    "REPORT_REMAINING_RECORD": "record still exists",
    "REPORT_EXPOSED_PII": "personal data still readable",
    "REPORT_ACTIVE_DATA_FLOW": "connector still copying data",
    "REPORT_RESTORE_RISK": "backup can restore the data",
}
# Plain words for the customer reply.
CUSTOMER_NAME = {
    "USER_PROFILE": "your account profile", "SECONDARY_IDENTIFIER": "other identifiers linked to you",
    "SEARCH_INDEX_ENTRY": "search index entries", "CACHE_ENTRY": "cached copies",
    "SUPPORT_MESSAGE": "support conversations", "SUPPORT_ATTACHMENT": "files you sent to support",
    "ANALYTICS_PROFILE": "analytics data", "PRODUCT_TELEMETRY": "product usage data",
    "USER_EXPORT": "data exports", "MARKETING_PROFILE": "your marketing profile",
    "EMAIL_DELIVERY_LOG": "email delivery logs", "SECURITY_AUDIT_LOG": "security audit logs",
    "LEGAL_HOLD_RECORD": "records under legal hold", "BACKUP_ARCHIVE": "backups",
    "ACCOUNT_TOMBSTONE": "an internal reference record", "DATA_FLOW": "a data connector",
}
CUSTOMER_OUTCOME = {
    "ABSENT": "deleted", "REDACTED": "your personal details removed",
    "ANONYMIZED": "anonymized, so it can no longer be linked to you",
    "DISABLED": "stopped, so it no longer copies your data",
    "BLOCKED": "restoring your data from them is blocked, and they expire on their normal schedule",
}
CUSTOMER_KEPT = {
    "SECURITY_AUDIT_LOG": "kept only for the required security retention period, then deleted",
    "LEGAL_HOLD_RECORD": "kept while a legal hold is active",
}


# ---------------------------------------------------------------- engine


def analyse(link_overrides=None):
    """Run the deletion-audit engine over every request. Mirrors engine/main.py run(), plus human account links."""
    link_overrides = link_overrides or {}
    ctx = load_context(quiet=True)
    identities, events, observations = ctx["identities"], ctx["events"], ctx["observations"]
    index, rules = ctx["index"], ctx["rules"]
    requests = load_requests(os.path.join(DATASET, "deletion_requests.csv"))

    world = {}
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        if os.path.exists(os.path.join(DATASET, name)):
            for r in read_csv(name):
                world[r["case_id"]] = r
    canonical = [r["request_text"] for r in world.values()]
    labels = classify.classify_all(canonical, canonical)
    cases = []
    for r in world.values():
        uid = identities.resolve_email(r["email"])
        if labels[r["request_text"]]["label"] == "DELETE" and uid:
            cases.append(dict(r, uid=uid))
    links = linker.link_accounts(cases, observations, events, identities)
    engine_state.attribute_runs(cases, index)
    by_id = {c["case_id"]: c for c in cases}

    result = {}
    for r in requests:
        cid = r["case_id"]
        case = by_id.get(cid)
        link = links.get(cid, {"accounts": []})
        info = {"request": r, "label": labels[r["request_text"]], "uid": identities.resolve_email(r["email"]),
                "link_reason": link.get("reason"), "candidates": link.get("candidates", []),
                "human_link": link_overrides.get(cid)}
        if case is None:
            info.update(status="NOT_APPLICABLE", steps=[])
        else:
            accounts = [link_overrides[cid]["account"]] if cid in link_overrides else link["accounts"]
            info["status"], info["steps"] = planner.plan(engine_state.decide(case, index, rules, accounts))
        result[cid] = info
    return result, identities


# ---------------------------------------------------------------- small helpers


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_email(email):
    name, _, domain = email.strip().lower().partition("@")
    return (name[:1] + "•••@" + domain) if domain else "•••"  # bullets, not *, so chat Markdown leaves it alone


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


def case_file(cid):
    return os.path.join(CASES_DIR, cid + ".json")


def load_case(cid):
    return load_json(case_file(cid), None)


def evidence_index():
    return {r["file_id"]: r for r in read_csv("evidence_files.csv")}


def evidence_label(eid, evidence):
    e = evidence.get(eid)
    if not e:
        return eid or "-"
    return "%s (%s, %s)" % (eid, e["origin"].replace("_", " "), e["captured_at"][:10])


def rule_for(resource_id):
    _, category = prefix_info(resource_id)
    for r in read_csv("deletion_rules.csv"):
        if r["data_category"] == category:
            return r
    return None


POLICY_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE",
                 "BLOCK_RESTORE": "RETAIN", "VERIFY_RECORD_ABSENT": "DELETE", "VERIFY_RECORD_REDACTED": "REDACT",
                 "VERIFY_RECORD_ANONYMIZED": "ANONYMIZE", "VERIFY_RECORD_PRESENT": "RETAIN",
                 "VERIFY_RESTORE_BLOCKED": "RETAIN"}


def policy_note(step):
    """The deletion_rules.csv row behind a step, when that rule's action is the one taken."""
    rule = rule_for(step["resource_id"])
    if rule and rule["required_action"] == POLICY_ACTION.get(step["operation"]):
        return "%s %s" % (rule["rule_id"], rule["required_action"])
    return ""


def need_case(results, cid):
    if cid not in results:
        sys.exit("Unknown case `%s`. Case ids look like case_483." % cid)
    return results[cid]


def steps_by_phase(steps, phase):
    return [s for s in steps if s["phase"] == phase]


# ---------------------------------------------------------------- commands


def cmd_stats(args):
    results, _ = analyse(load_json(LINKS_FILE, {}))
    table = Counter((i["request"]["reported_status"], i["status"]) for i in results.values())
    claimed = sum(v for (rep, _), v in table.items() if rep == "COMPLETE")
    ghosts = table[("COMPLETE", "INCOMPLETE")]
    systems = Counter()
    for i in results.values():
        for s in steps_by_phase(i["steps"], "FINDING"):
            systems[s["system_id"]] += 1
    print("### Deletion requests: what the workflow said vs. what the evidence shows\n")
    print("| Workflow said | Evidence: COMPLETE | Evidence: INCOMPLETE | Not a deletion request |")
    print("|---|---|---|---|")
    for rep in ("COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"):
        print("| %s | %d | %d | %d |" % (rep, table[(rep, "COMPLETE")], table[(rep, "INCOMPLETE")],
                                         table[(rep, "NOT_APPLICABLE")]))
    print("\n**%d of %d deletions marked COMPLETE (%d%%) still had personal data at audit time.**"
          % (ghosts, claimed, round(100 * ghosts / claimed) if claimed else 0))
    print("\nWhere the leftover data sits most often: " +
          ", ".join("%s (%d)" % kv for kv in systems.most_common(5)))


def cmd_lookup(args):
    results, identities = analyse(load_json(LINKS_FILE, {}))
    uid = identities.resolve_email(args.email)
    if not uid:
        print("No customer has `%s` as a current or past email." % mask_email(args.email))
        return
    hits = [cid for cid, i in results.items() if i["uid"] == uid]
    print("`%s` belongs to **%s**. Deletion cases: %s" % (mask_email(args.email), uid,
                                                          ", ".join(sorted(hits, key=case_sort_key)) or "none"))


def cmd_intake(args):
    results, identities = analyse(load_json(LINKS_FILE, {}))
    info = need_case(results, args.case_id)
    r = info["request"]
    print("### Intake %s" % args.case_id)
    print("> %s" % r["request_text"])
    label = info["label"]
    if info["status"] == "NOT_APPLICABLE":
        print("\n**Not a deletion request.** Do not delete anything. Rules said `%s`, the model said `%s`."
              % (label["rule"] or "no match", label["llm"] or "no answer"))
        print("Route it to the normal support queue (opt-out, access, export or correction).")
        save_json(case_file(args.case_id), {"case_id": args.case_id, "stage": "NOT_A_DELETION", "updated_at": now()})
        return
    user = identities.users[info["uid"]]
    old = [e for e in user["emails"] if e != r["email"].strip().lower()]
    print("\n**Deletion request.** Person: **%s**, written from %s." % (info["uid"], mask_email(r["email"])))
    print("Other identifiers to search for: %d older email(s) %s, %d phone number(s), %d email hash(es)."
          % (len(old), " ".join(mask_email(e) for e in old), len(user["phones"]), len(user["hashes"])))
    print("Requested %s. The deletion workflow reported **%s** at %s. Audit time: %s."
          % (r["requested_at"], r["reported_status"], r["reported_at"], r["audit_at"]))


def cmd_audit(args):
    results, _ = analyse(load_json(LINKS_FILE, {}))
    info = need_case(results, args.case_id)
    evidence = evidence_index()
    r, steps = info["request"], info["steps"]
    if info["status"] == "NOT_APPLICABLE":
        print("%s is not a deletion request, so there is nothing to audit." % args.case_id)
        return
    findings = steps_by_phase(steps, "FINDING")
    fixes = steps_by_phase(steps, "REMEDIATION")
    checks = steps_by_phase(steps, "VERIFICATION")
    claim = r["reported_status"]
    print("### Audit %s (person %s, evidence up to %s)" % (args.case_id, info["uid"], r["audit_at"]))
    if info["status"] == "COMPLETE":
        print("Workflow said **%s**. Evidence agrees: **no personal data left**. %d record(s) checked."
              % (claim, len(checks)))
    else:
        print("Workflow said **%s**. Evidence says **INCOMPLETE**: %d problem(s) in %d system(s)."
              % (claim, len(findings), len({s["system_id"] for s in findings})))
        print("\n| # | System | Record | Problem | Proof |")
        print("|---|---|---|---|---|")
        for s in findings:
            print("| %d | %s | %s | %s | %s |" % (s["step_no"], s["system_id"], s["resource_id"],
                                                 FINDING_TEXT[s["operation"]], evidence_label(s["evidence_id"], evidence)))
        print("\n**Remediation plan** (order matters: stop the copying first, then remove):")
        for s in fixes:
            why = " (policy %s)" % policy_note(s) if policy_note(s) else ""
            print("- step %d: %s, `%s` in %s%s" % (s["step_no"], VERB[s["operation"]], s["resource_id"],
                                                   s["system_id"], why))
    kept = [s for s in checks if s["operation"] == "VERIFY_RECORD_PRESENT"]
    for s in kept:
        rule = rule_for(s["resource_id"])
        print("- keep `%s` (%s): %s" % (s["resource_id"], rule["data_category"] if rule else "?",
                                        rule["condition"] if rule else "policy says retain"))
    if info["link_reason"] and not info["human_link"]:
        cands = " or ".join("`%s`" % c for c in info["candidates"]) or "no candidate found"
        print("\n**Needs a person:** the evidence does not prove which account belongs to %s (%s). Candidates: %s. "
              "The account step is left out until someone confirms with `ghost link`." % (info["uid"],
                                                                                         info["link_reason"], cands))
    if info["human_link"]:
        h = info["human_link"]
        print("\nAccount `%s` confirmed by %s at %s." % (h["account"], h["confirmed_by"], h["at"]))
    stage = "AUDITED_CLEAN" if info["status"] == "COMPLETE" else "AWAITING_APPROVAL"
    record = load_case(args.case_id) or {"case_id": args.case_id}
    record.update(stage=stage, verdict=info["status"], uid=info["uid"], steps=steps, audited_at=now(),
                  updated_at=now(), reported_status=claim)
    for key in ("ledger", "approval", "approvals", "failures"):
        record.pop(key, None)
    save_json(case_file(args.case_id), record)
    if stage == "AWAITING_APPROVAL":
        print("\nApproval needed: reply `approve %s` to run steps %s, or `approve %s skip <step numbers>`."
              % (args.case_id, ", ".join(str(s["step_no"]) for s in fixes), args.case_id))


def cmd_link(args):
    results, _ = analyse(load_json(LINKS_FILE, {}))
    info = need_case(results, args.case_id)
    if not args.account_id.startswith("account_"):
        sys.exit("An account id looks like account_617.")
    if info["candidates"] and args.account_id not in info["candidates"]:
        print("Note: `%s` was not one of the evidence candidates (%s). Recording the decision anyway."
              % (args.account_id, ", ".join(info["candidates"])))
    links = load_json(LINKS_FILE, {})
    links[args.case_id] = {"account": args.account_id, "confirmed_by": args.confirmed_by, "at": now()}
    save_json(LINKS_FILE, links)
    print("Recorded: %s owns `%s` (confirmed by %s). Run `ghost audit %s` again for the updated plan."
          % (info["uid"], args.account_id, args.confirmed_by, args.case_id))


def cmd_fix(args):
    record = load_case(args.case_id)
    if not record or record.get("stage") not in ("AWAITING_APPROVAL", "VERIFY_FAILED"):
        sys.exit("Nothing to fix: %s has no audit waiting for approval. Run `ghost audit %s` first."
                 % (args.case_id, args.case_id))
    if args.approved_by.lower().startswith("ghost-"):
        sys.exit("Refused: an agent cannot approve its own remediation. A person must approve.")
    skip = {int(x) for x in args.skip.split(",") if x.strip()} if args.skip else set()
    estate = load_json(os.path.join(STATE_DIR, "estate.json"), {})
    copies = estate.setdefault(args.case_id, {})
    found = {f["resource_id"]: f["state"] for f in steps_by_phase(record["steps"], "FINDING")}
    ledger = record.get("ledger", [])
    done, held = [], []
    for s in steps_by_phase(record["steps"], "REMEDIATION"):
        if copies.get(s["resource_id"], {}).get("state") == TARGET[s["operation"]]:
            continue  # already carried out under an earlier approval
        if s["step_no"] in skip:
            held.append(s)
            continue
        before = copies.get(s["resource_id"], {}).get("state", found.get(s["resource_id"], "PRESENT"))
        copies[s["resource_id"]] = {"system_id": s["system_id"], "state": TARGET[s["operation"]], "updated_at": now()}
        ledger.append({"at": now(), "step_no": s["step_no"], "operation": s["operation"],
                       "system_id": s["system_id"], "resource_id": s["resource_id"], "from": before,
                       "to": TARGET[s["operation"]], "actor": "ghost-fixer", "approved_by": args.approved_by})
        done.append(s)
    save_json(os.path.join(STATE_DIR, "estate.json"), estate)
    approval = {"by": args.approved_by, "at": now(), "skipped": sorted(skip)}
    record.setdefault("approvals", []).append(approval)
    record.update(stage="FIXED" if not held else "PARTLY_FIXED", ledger=ledger, updated_at=now(), approval=approval)
    save_json(case_file(args.case_id), record)
    print("### Remediation %s (approved by %s)\n" % (args.case_id, args.approved_by))
    for s in done:
        print("- done step %d: %s `%s` -> %s" % (s["step_no"], VERB[s["operation"]], s["resource_id"],
                                                 TARGET[s["operation"]]))
    for s in held:
        print("- held back step %d: %s `%s` (not approved)" % (s["step_no"], VERB[s["operation"]], s["resource_id"]))
    print("\nSandbox estate updated; every change is in the ledger. Ready for independent verification.")


def cmd_verify(args):
    record = load_case(args.case_id)
    if not record or record.get("stage") not in ("FIXED", "PARTLY_FIXED", "AUDITED_CLEAN", "VERIFY_FAILED"):
        sys.exit("Nothing to verify: %s has not been fixed (or found clean) yet." % args.case_id)
    # Independent: re-derive the required end state from the raw evidence, not from the auditor's saved plan.
    results, _ = analyse(load_json(LINKS_FILE, {}))
    info = need_case(results, args.case_id)
    evidence = evidence_index()
    estate = load_json(os.path.join(STATE_DIR, "estate.json"), {})
    ledger = {(e["operation"], e["resource_id"]): e for e in record.get("ledger", [])}
    failures, lines = [], []
    for s in steps_by_phase(info["steps"], "REMEDIATION"):
        if (s["operation"], s["resource_id"]) not in ledger:
            failures.append("step %d not carried out: %s `%s`" % (s["step_no"], VERB[s["operation"]], s["resource_id"]))
    for s in steps_by_phase(info["steps"], "VERIFICATION"):
        want = s["state"]
        fixed = estate.get(args.case_id, {}).get(s["resource_id"])
        if fixed:
            got, proof = fixed["state"], "remediation ledger %s" % fixed["updated_at"]
        else:
            finding = [f for f in steps_by_phase(info["steps"], "FINDING") if f["resource_id"] == s["resource_id"]]
            got = finding[0]["state"] if finding else want
            proof = evidence_label(s["evidence_id"], evidence) if s["evidence_id"] else "audit evidence"
        ok = got == want
        lines.append("| %s | %s | %s | %s | %s |" % ("pass" if ok else "**FAIL**", s["system_id"], s["resource_id"],
                                                    "%s (needs %s)" % (got, want) if not ok else want, proof))
        if not ok:
            failures.append("`%s` is %s, policy needs %s" % (s["resource_id"], got, want))
    print("### Verification %s\n" % args.case_id)
    print("| Result | System | Record | State | Proof |")
    print("|---|---|---|---|---|")
    print("\n".join(lines))
    if failures:
        record.update(stage="VERIFY_FAILED", updated_at=now(), failures=failures)
        save_json(case_file(args.case_id), record)
        print("\n**Not verified.** Certificate withheld:")
        for f in failures:
            print("- " + f)
        return
    record.update(stage="VERIFIED", updated_at=now(), failures=[])
    save_json(case_file(args.case_id), record)
    path = write_certificate(args.case_id, info, record, evidence)
    print("\n**Verified.** Every copy is in the state the policy requires. Certificate: `%s`"
          % os.path.relpath(path, ROOT))


def write_certificate(cid, info, record, evidence):
    r = info["request"]
    out = ["# Deletion certificate: %s" % cid, "",
           "- Person: %s" % info["uid"],
           "- Requested: %s" % r["requested_at"],
           "- Workflow reported: %s at %s" % (r["reported_status"], r["reported_at"]),
           "- Audit found: %s (evidence up to %s)" % (info["status"], r["audit_at"]),
           "- Verified: %s by ghost-verifier, independent of ghost-fixer" % now(), ""]
    if record.get("approvals"):
        out += ["## Approvals", ""]
        for a in record["approvals"]:
            held = " (held back steps %s)" % ", ".join(map(str, a["skipped"])) if a["skipped"] else ""
            out.append("- Remediation approved by %s at %s in the Switch room%s." % (a["by"], a["at"], held))
        out.append("")
    if record.get("ledger"):
        out += ["## Actions taken", "", "| Step | Action | System | Record | From | To | Time |", "|---|---|---|---|---|---|---|"]
        out += ["| %d | %s | %s | %s | %s | %s | %s |" % (e["step_no"], e["operation"], e["system_id"],
                                                      e["resource_id"], e["from"], e["to"], e["at"])
                for e in record["ledger"]]
        out.append("")
    out += ["## Final state", "", "| System | Record | State | Policy |", "|---|---|---|---|"]
    for s in steps_by_phase(info["steps"], "VERIFICATION"):
        out.append("| %s | %s | %s | %s |" % (s["system_id"], s["resource_id"], s["state"], policy_note(s) or "-"))
    out += ["", "Records kept on purpose are kept because the deletion policy requires it (retention or legal hold).",
            "This demo runs remediation against a sandbox copy of the estate, not live systems."]
    os.makedirs(CERTS_DIR, exist_ok=True)
    path = os.path.join(CERTS_DIR, cid + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return path


def cmd_certificate(args):
    path = os.path.join(CERTS_DIR, args.case_id + ".md")
    if not os.path.exists(path):
        sys.exit("No certificate for %s yet: it has not passed `ghost verify`." % args.case_id)
    with open(path, encoding="utf-8") as fh:
        print(fh.read())


def cmd_scan(args):
    started = time.perf_counter()
    results, _ = analyse(load_json(LINKS_FILE, {}))
    seconds = time.perf_counter() - started
    files = len(read_csv("evidence_files.csv"))
    picked = {cid: i for cid, i in results.items()
              if not args.reported or i["request"]["reported_status"] == args.reported}
    open_cases = []
    for cid, i in picked.items():
        if i["status"] != "INCOMPLETE":
            continue
        findings = steps_by_phase(i["steps"], "FINDING")
        ops = {s["operation"] for s in findings}
        can_return = bool(ops & {"REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"})
        open_cases.append((not can_return, -len(findings), case_sort_key(cid), cid, i, findings, can_return))
    open_cases.sort()
    verdicts = Counter(i["status"] for i in picked.values())
    scope = "reported %s" % args.reported if args.reported else "all"
    print("### Scan: %d %s requests in %.1f s\n" % (len(picked), scope, seconds))
    print("Checked against %d evidence files. **%d need work**, %d are clean, %d are not deletion requests."
          % (files, verdicts["INCOMPLETE"], verdicts["COMPLETE"], verdicts["NOT_APPLICABLE"]))
    comeback = sum(1 for c in open_cases if c[6])
    print("%d of the open cases can get the data back by themselves (live connector or restorable backup), "
          "so they come first.\n" % comeback)
    print("| Case | Person | Workflow said | Problems | Systems | Data can come back |")
    print("|---|---|---|---|---|---|")
    for _, _, _, cid, i, findings, can_return in open_cases[:args.top]:
        print("| %s | %s | %s | %d | %s | %s |" % (cid, i["uid"], i["request"]["reported_status"], len(findings),
                                                 ", ".join(sorted({s["system_id"] for s in findings})),
                                                 "yes" if can_return else "no"))
    if len(open_cases) > args.top:
        print("\n...and %d more. Bring any case id to the auditor to start it." % (len(open_cases) - args.top))


def customer_reply(cid, info, record):
    r = info["request"]
    changed, kept = defaultdict(set), set()
    for s in steps_by_phase(info["steps"], "VERIFICATION"):
        _, category = prefix_info(s["resource_id"])
        name = CUSTOMER_NAME.get(category)
        if not name:
            continue
        if s["state"] == "PRESENT":
            kept.add((name, CUSTOMER_KEPT.get(category, "kept because our deletion policy requires it")))
        else:
            changed[CUSTOMER_OUTCOME[s["state"]]].add(name)
    lines = ["**To:** %s" % mask_email(r["email"]),
             "**Subject:** Your deletion request (reference %s)" % cid, "",
             "Hello,", "",
             "On %s you asked us to delete your account and personal data. We have finished the work and "
             "checked it independently." % r["requested_at"][:10], ""]
    order = ["deleted", "your personal details removed", "anonymized, so it can no longer be linked to you",
             "stopped, so it no longer copies your data",
             "restoring your data from them is blocked, and they expire on their normal schedule"]
    for outcome in order:
        for name in sorted(changed.get(outcome, ())):
            lines.append("- %s%s: %s" % (name[0].upper(), name[1:], outcome))
    if kept:
        lines += ["", "What we keep, and why:"]
        lines += ["- %s%s: %s" % (n[0].upper(), n[1:], why) for n, why in sorted(kept)]
    lines += ["", "If you have any questions, reply to this email and quote %s." % cid, "",
              "Northstar Shop privacy team"]
    return "\n".join(lines)


def cmd_reply(args):
    record = load_case(args.case_id)
    if not record or record.get("stage") not in ("VERIFIED", "CLOSED"):
        sys.exit("No reply yet: %s must pass `ghost verify` first." % args.case_id)
    results, _ = analyse(load_json(LINKS_FILE, {}))
    text = customer_reply(args.case_id, need_case(results, args.case_id), record)
    if not args.approved_by:
        print("### Draft reply to the customer (%s)\n" % args.case_id)
        print(text)
        print("\nNot sent. A person approves it with `send reply %s`." % args.case_id)
        return
    if args.approved_by.lower().startswith("ghost-"):
        sys.exit("Refused: an agent cannot approve a message to a customer. A person must approve.")
    outbox = os.path.join(STATE_DIR, "outbox", args.case_id + ".md")
    os.makedirs(os.path.dirname(outbox), exist_ok=True)
    with open(outbox, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    record.update(stage="CLOSED", updated_at=now(), reply={"approved_by": args.approved_by, "at": now()})
    save_json(case_file(args.case_id), record)
    print("Reply approved by %s and queued (`%s`). **%s is closed.**"
          % (args.approved_by, os.path.relpath(outbox, ROOT), args.case_id))


def cmd_status(args):
    os.makedirs(CASES_DIR, exist_ok=True)
    records = [load_json(os.path.join(CASES_DIR, f), {}) for f in os.listdir(CASES_DIR) if f.endswith(".json")]
    if args.case_id:
        rec = [r for r in records if r.get("case_id") == args.case_id]
        if not rec:
            print("%s has not been picked up yet." % args.case_id)
            return
        r = rec[0]
        print("**%s**: %s (updated %s)" % (r["case_id"], r.get("stage"), r.get("updated_at")))
        if r.get("approval"):
            print("Approved by %s at %s." % (r["approval"]["by"], r["approval"]["at"]))
        for f in r.get("failures", []):
            print("- open issue: " + f)
        return
    if not records:
        print("No cases picked up yet.")
        return
    by_stage = defaultdict(list)
    for r in records:
        by_stage[r.get("stage", "?")].append(r["case_id"])
    for stage in sorted(by_stage):
        print("- **%s**: %s" % (stage, ", ".join(sorted(by_stage[stage], key=case_sort_key))))


def main():
    ap = argparse.ArgumentParser(prog="ghost", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p = sub.add_parser("lookup")
    p.add_argument("email")
    for name in ("intake", "audit", "verify", "certificate"):
        sub.add_parser(name).add_argument("case_id")
    p = sub.add_parser("link")
    p.add_argument("case_id")
    p.add_argument("account_id")
    p.add_argument("--confirmed-by", required=True)
    p = sub.add_parser("fix")
    p.add_argument("case_id")
    p.add_argument("--approved-by", required=True)
    p.add_argument("--skip", default="")
    p = sub.add_parser("reply")
    p.add_argument("case_id")
    p.add_argument("--approved-by", default="")
    p = sub.add_parser("scan")
    p.add_argument("--reported", choices=["COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"])
    p.add_argument("--top", type=int, default=10)
    sub.add_parser("status").add_argument("case_id", nargs="?")
    args = ap.parse_args()
    {"stats": cmd_stats, "lookup": cmd_lookup, "intake": cmd_intake, "audit": cmd_audit, "link": cmd_link,
     "fix": cmd_fix, "verify": cmd_verify, "certificate": cmd_certificate, "reply": cmd_reply, "scan": cmd_scan,
     "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
