"""Ghost Data deletion-audit agent: python code/main.py [--requests FILE] [--output FILE]

Writes output.csv (case_id, deletion_status, steps_json), an ID-only account review report and a
usage summary for the run.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classify  # noqa: E402
import llm  # noqa: E402
import linker  # noqa: E402
import planner  # noqa: E402
import state  # noqa: E402
import validate  # noqa: E402
from common import CODE_DIR, DATASET, REPO_ROOT, read_csv  # noqa: E402
from identity import Identities  # noqa: E402
from ingest import activity, build_index  # noqa: E402


def load_requests(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_context(quiet=False):
    """Everything that does not depend on the requests: identities, activity, parsed evidence, policy."""
    identities = Identities()
    events = activity.load()
    observations, manifest = build_index(verbose=not quiet)
    return {"identities": identities, "events": events, "observations": observations, "manifest": manifest,
            "rules": state.load_rules(), "index": state.EvidenceIndex(observations, events, identities)}


def run(requests_path, output_path, review_path=None, usage_path=None, quiet=False, ctx=None, exclude_ids=()):
    requests = load_requests(requests_path)
    ctx = ctx or load_context(quiet)
    identities, events, observations = ctx["identities"], ctx["events"], ctx["observations"]
    manifest, rules, index = ctx["manifest"], ctx["rules"], ctx["index"]

    # Linking is global: every participant-visible request takes part in batch elimination.
    world = {}
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        if os.path.exists(os.path.join(DATASET, name)):
            for r in read_csv(name):
                world[r["case_id"]] = r
    canonical = [r["request_text"] for r in world.values()]
    for cid in exclude_ids:
        world.pop(cid, None)
    for r in requests:
        world[r["case_id"]] = r
    labels = classify.classify_all([r["request_text"] for r in world.values()], canonical)
    cases = []
    for r in world.values():
        uid = identities.resolve_email(r["email"])
        if labels[r["request_text"]]["label"] == "DELETE" and uid:
            cases.append(dict(r, uid=uid))
    links = linker.link_accounts(cases, observations, events, identities)
    state.attribute_runs(cases, index)
    by_id = {c["case_id"]: c for c in cases}

    activity_times = {e["activity_id"]: e["time"] for e in events}
    rows, review, problems = [], [], []
    for r in requests:
        cid = r["case_id"]
        case = by_id.get(cid)
        if case is None:
            status, steps = "NOT_APPLICABLE", []
        else:
            link = links.get(cid, {"accounts": []})
            decisions = state.decide(case, index, rules, link["accounts"])
            status, steps = planner.plan(decisions)
            if link.get("reason"):
                review.append({"case_id": cid, "reason": link["reason"],
                               "candidate_accounts": " ".join(link["candidates"]),
                               "evidence_ids": " ".join(link["evidence_ids"]),
                               "reviewer_check": linker.REVIEW_HINT[link["reason"]],
                               "review_status": "PENDING"})
        bad = validate.check_row(dict(r), status, steps, manifest, activity_times)
        problems += ["%s: %s" % (cid, p) for p in bad]
        rows.append({"case_id": cid, "deletion_status": status, "steps_json": planner.dumps(steps)})
    problems += validate.check_file(rows, [r["case_id"] for r in requests])

    with open(output_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if review_path:
        with open(review_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["case_id", "reason", "candidate_accounts", "evidence_ids",
                                               "reviewer_check", "review_status"])
            w.writeheader()
            w.writerows(review)
    if usage_path:
        disagreements = sum(1 for v in labels.values() if not v["agree"])
        with open(usage_path, "w", encoding="utf-8") as fh:
            json.dump({"requests": len(requests), "provider": llm.PROVIDER, "model": llm.MODEL,
                       "calls": llm.USAGE.calls, "summary": {"|".join(k): v for k, v in llm.USAGE.summary().items()},
                       "classifier_disagreements": disagreements}, fh, indent=1)
    if not quiet:
        counts = {}
        for row in rows:
            counts[row["deletion_status"]] = counts.get(row["deletion_status"], 0) + 1
        print("wrote %s: %d rows %s; review queue %d; validation problems %d"
              % (output_path, len(rows), counts, len(review), len(problems)))
        for p in problems[:20]:
            print("  PROBLEM", p)
    return rows, review, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requests", default=os.path.join(DATASET, "deletion_requests.csv"))
    ap.add_argument("--output", default=os.path.join(REPO_ROOT, "output.csv"))
    ap.add_argument("--review", default=os.path.join(CODE_DIR, "evaluation", "account_review.csv"))
    ap.add_argument("--usage", default=os.path.join(CODE_DIR, "evaluation", "usage_run.json"))
    args = ap.parse_args()
    _, _, problems = run(args.requests, args.output, args.review, args.usage)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
