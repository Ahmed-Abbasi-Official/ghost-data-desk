"""Score the agent on the public samples: python code/evaluation/main.py [--show N]

Metrics: case status accuracy, step-level precision/recall/F1 (exact step match incl. evidence and without
evidence), field-level accuracy over expected steps, and sequence accuracy (exact ordered match per case).
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import main as agent  # noqa: E402
from common import DATASET  # noqa: E402

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def key(s, with_evidence=True):
    k = (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"])
    return k + (s["evidence_id"],) if with_evidence else k


def prf(tp, n_pred, n_gold):
    p = tp / n_pred if n_pred else 1.0
    r = tp / n_gold if n_gold else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def score(gold, pred):
    m = Counter()
    per_case = {}
    for cid, (g_status, g_steps) in gold.items():
        p_status, p_steps = pred.get(cid, ("MISSING", []))
        m["cases"] += 1
        m["status_ok"] += g_status == p_status
        for ev in (True, False):
            gk, pk = Counter(key(s, ev) for s in g_steps), Counter(key(s, ev) for s in p_steps)
            tag = "ev" if ev else "noev"
            m["tp_" + tag] += sum((gk & pk).values())
            m["pred_" + tag] += len(p_steps)
            m["gold_" + tag] += len(g_steps)
        by_res = {(s["resource_id"], s["operation"]): s for s in p_steps}
        for s in g_steps:
            hit = by_res.get((s["resource_id"], s["operation"]))
            for f in FIELDS:
                m["field_total"] += 1
                m["field_ok"] += bool(hit) and hit[f] == s[f]
        exact = [key(s) for s in g_steps] == [key(s) for s in p_steps]
        m["seq_ok"] += exact
        per_case[cid] = {"status": (g_status, p_status), "exact": exact,
                         "missing": sorted(set(map(key, g_steps)) - set(map(key, p_steps))),
                         "extra": sorted(set(map(key, p_steps)) - set(map(key, g_steps)))}
    return m, per_case


def report(m):
    p1, r1, f1 = prf(m["tp_ev"], m["pred_ev"], m["gold_ev"])
    p2, r2, f2 = prf(m["tp_noev"], m["pred_noev"], m["gold_noev"])
    return {
        "cases": m["cases"],
        "status_accuracy": m["status_ok"] / m["cases"],
        "step_precision": p1, "step_recall": r1, "step_f1": f1,
        "step_f1_ignoring_evidence": f2, "step_precision_ignoring_evidence": p2, "step_recall_ignoring_evidence": r2,
        "field_accuracy": m["field_ok"] / m["field_total"] if m["field_total"] else 1.0,
        "sequence_accuracy": m["seq_ok"] / m["cases"],
    }


def run_custom(ctx):
    """Run each custom case alone (its base sample case is removed from the linking world) and check it."""
    import tempfile
    gold = load(os.path.join(DATASET, "sample_output.csv"))
    with open(os.path.join(HERE, "custom_cases.csv"), encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    manifest = ctx["manifest"]
    results = []
    for row in rows:
        with tempfile.TemporaryDirectory() as tmp:
            req = os.path.join(tmp, "req.csv")
            with open(req, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(row))
                w.writeheader()
                w.writerow(row)
            out = os.path.join(tmp, "out.csv")
            agent.run(req, out, None, None, quiet=True, ctx=ctx, exclude_ids=[row["base_case"]] if row["base_case"] else [])
            status, steps = load(out)[row["case_id"]]
        expect = row["expect"]
        if expect == "NOT_APPLICABLE":
            ok = status == "NOT_APPLICABLE" and steps == []
        elif expect == "SAME_AS_BASE":
            g_status, g_steps = gold[row["base_case"]]
            ok = status == g_status and [key(s) for s in steps] == [key(s) for s in g_steps]
        elif expect == "NO_LATE_EVIDENCE":
            ok = all(not s["evidence_id"] or (s["evidence_id"] in manifest and
                                               manifest[s["evidence_id"]]["captured_at"] <= row["audit_at"]) for s in steps)
        else:
            ok = False
        results.append({"case_id": row["case_id"], "base_case": row["base_case"], "purpose": row["purpose"],
                        "expect": expect, "status": status, "steps": len(steps), "passed": ok})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=40, help="print diffs for up to N failing cases")
    args = ap.parse_args()
    out = os.path.join(HERE, "sample_predictions.csv")
    ctx = agent.load_context(quiet=True)
    agent.run(os.path.join(DATASET, "sample_deletion_requests.csv"), out,
              os.path.join(HERE, "sample_account_review.csv"), None, quiet=True, ctx=ctx)
    gold, pred = load(os.path.join(DATASET, "sample_output.csv")), load(out)
    m, per_case = score(gold, pred)
    metrics = report(m)
    print(json.dumps(metrics, indent=1))
    shown = 0
    for cid, info in per_case.items():
        if info["exact"] and info["status"][0] == info["status"][1]:
            continue
        if shown >= args.show:
            break
        shown += 1
        print("\n%s gold=%s pred=%s" % (cid, *info["status"]))
        for k in info["missing"]:
            print("   - missing", k)
        for k in info["extra"]:
            print("   + extra  ", k)
    custom = run_custom(ctx)
    print("\ncustom cases:")
    for c in custom:
        print("  %s %-5s %s -> %s (%d steps)" % (c["case_id"], "PASS" if c["passed"] else "FAIL", c["purpose"], c["status"], c["steps"]))
    with open(os.path.join(HERE, "sample_metrics.json"), "w", encoding="utf-8") as fh:
        json.dump({"metrics": metrics, "per_case": per_case, "custom": custom}, fh, indent=1, default=list)


if __name__ == "__main__":
    main()
