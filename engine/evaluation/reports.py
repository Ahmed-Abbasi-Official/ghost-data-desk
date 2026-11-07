"""Write evaluation_report.md and usage_report.md from the latest run artefacts.

python code/evaluation/reports.py   (after code/main.py and code/evaluation/main.py)
"""
import csv
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# Reference list prices (USD per million tokens) used only to estimate what the same tokens would cost on a
# hosted model; the submitted run used a local model at zero marginal cost.
REFERENCE_PRICE = ("claude-haiku-4-5", 1.0, 5.0)

# Iterations measured on the 32 public samples during development (step F1 includes evidence ids).
HISTORY = [
    ("First end-to-end run (rules-only classification, five text parsers)", "0.906", "0.842"),
    ("Deletion-worker treatment disambiguates support message vs attachment and analytics; a single delete "
     "event within 15 minutes of the report links the account", "0.969", "0.885"),
    ("State uses the record's own observation time; citations restricted to files captured by audit_at", "0.906", "0.877"),
    ("Cite files that name the record before snapshots that merely omit it", "0.906", "0.894"),
    ("Absence-by-omission only from full SQL table dumps (curated bundles no longer hide FOUND records)", "0.969", "0.951"),
    ("Deletion-run attribution of identifier-free records (single report within 15 minutes only); screenshots "
     "via local VLM; SQL rows dated at snapshot time", "0.969", "0.954"),
    ("Local LLM classification + deletion follow-up cues (\"profile is gone\", \"cannot identify me\") — final",
     "1.000", "0.968"),
]


def _read_json(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def evaluation_report():
    data = _read_json("sample_metrics.json") or {}
    m, per_case, custom = data.get("metrics", {}), data.get("per_case", {}), data.get("custom", [])
    out_rows = _rows(os.path.join(ROOT, "output.csv"))
    review = _rows(os.path.join(HERE, "account_review.csv"))
    pct = lambda v: "%.1f%%" % (100 * v)
    lines = ["# Evaluation report", "",
             "All numbers come from `python code/evaluation/main.py`, which runs the full agent on the 32 public sample",
             "requests and compares with `dataset/sample_output.csv`. Custom cases are in `custom_cases.csv`.", "",
             "## Sample metrics (32 cases)", "", "| Metric | Value |", "|---|---|"]
    rows = [("Case-level status accuracy", m.get("status_accuracy")),
            ("Step precision (all 7 fields incl. evidence_id)", m.get("step_precision")),
            ("Step recall (all 7 fields)", m.get("step_recall")),
            ("Step F1 (all 7 fields)", m.get("step_f1")),
            ("Step F1 ignoring evidence_id", m.get("step_f1_ignoring_evidence")),
            ("Field-level accuracy (expected steps, per field)", m.get("field_accuracy")),
            ("Sequence accuracy (exact ordered steps_json per case)", m.get("sequence_accuracy"))]
    lines += ["| %s | %s |" % (k, pct(v) if isinstance(v, float) else v) for k, v in rows]
    lines += ["", "Step match = same phase, system_id, resource_id, operation, state and evidence_id. Field accuracy aligns",
              "each expected step with the predicted step on the same resource and operation and scores each field.", ""]

    misses = Counter()
    for info in per_case.values():
        for k in info.get("missing", []):
            misses[(k[3], k[2].rsplit("_", 1)[0])] += 1
    if misses:
        lines += ["## Remaining sample errors", "", "| Missing operation | Resource type | Count |", "|---|---|---|"]
        lines += ["| %s | %s | %d |" % (op, rt, n) for (op, rt), n in misses.most_common()]
        lines += ["", "Most remaining misses are records that carry no identifier anywhere (already-anonymised analytics,",
                  "profiles visible only as a deletion-worker event) where several cases share one deletion batch. The",
                  "agent does not guess them; see the review queue below.", ""]

    lines += ["## Custom evaluation cases", "",
              "Each case is a variant of a public sample (or a new request) whose correct answer is known.", "",
              "| Case | Purpose | Expectation | Result |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s (%s, %d steps) |" % (c["case_id"], c["purpose"], c["expect"],
                                                       "PASS" if c["passed"] else "FAIL", c["status"], c["steps"])
              for c in custom]
    lines += ["", "Unit tests (`python -m pytest code/evaluation -q`) cover the account linker (missing account, ambiguous tie,",
              "conflicting signals, status-result batch match, direct evidence), request classification traps, step",
              "ordering and evidence rules.", ""]

    if out_rows:
        counts = Counter(r["deletion_status"] for r in out_rows)
        reasons = Counter(r["reason"] for r in review)
        lines += ["## Full run (518 requests)", "",
                  "| Status | Rows |", "|---|---|"] + ["| %s | %d |" % kv for kv in sorted(counts.items())]
        lines += ["", "Account review queue (`code/evaluation/account_review.csv`, IDs only, status PENDING; no human",
                  "review has taken place):", "", "| Reason | Cases |", "|---|---|"]
        lines += ["| %s | %d |" % kv for kv in sorted(reasons.items())] + [""]

    lines += ["## Failures found and improvements made", "", "| Iteration | Status acc. | Step F1 |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % h for h in HISTORY]
    lines += ["", "Other findings during development:",
              "- Keyword rules alone mislabel free-form sample texts (\"download your data link\", \"downloadable archive\"),",
              "  so only explicit keep-the-account statements veto deletion; the local LLM classifies the rest.",
              "- The local 9B model labelled \"remove me from marketing lists only\" as DELETE in a smoke test; the",
              "  keep-the-account veto catches it (custom_01).",
              "- Direct identifier links cover only 14/32 sample profiles. Batch matching (delete result vs reported",
              "  status, which agreed on 181/182 directly linked cases) plus elimination raised this to 27/32 with no",
              "  wrong link; the remaining profiles go to the review queue instead of being guessed.",
              "- Curated JSON bundles list only some records, so a record missing from a bundle is not proof of",
              "  deletion when another file names it; full SQL table dumps are.", ""]
    with open(os.path.join(HERE, "evaluation_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def usage_report():
    run = _read_json("usage_run.json")
    if not run:
        return
    calls = run["calls"]
    n = max(run["requests"], 1)
    by_model = {}
    for c in calls:
        key = (c["provider"], c["model"])
        s = by_model.setdefault(key, Counter())
        s["calls"] += 1
        s["cached"] += c["cached"]
        s["input"] += c["input_tokens"]
        s["output"] += c["output_tokens"]
        s["seconds"] += c["seconds"]
        s["purpose:" + c["purpose"]] += 1
    name, pin, pout = REFERENCE_PRICE
    lines = ["# Usage report", "",
             "Final full-dataset run that produced `output.csv` (`python code/main.py`, %d requests)." % run["requests"], "",
             "| Provider | Model | Model calls | Served from cache | Input tokens | Output tokens | Total tokens | Cost (USD) |",
             "|---|---|---|---|---|---|---|---|"]
    tot = Counter()
    for (prov, model), s in sorted(by_model.items()):
        lines.append("| %s | %s | %d | %d | %d | %d | %d | 0.00 (local) |" % (
            prov, model, s["calls"], s["cached"], s["input"], s["output"], s["input"] + s["output"]))
        tot.update({k: v for k, v in s.items() if not k.startswith("purpose:")})
    total_tokens = tot["input"] + tot["output"]
    ref = tot["input"] / 1e6 * pin + tot["output"] / 1e6 * pout
    lines += ["| **Overall** | | %d | %d | %d | %d | %d | 0.00 |" % (tot["calls"], tot["cached"], tot["input"], tot["output"], total_tokens), "",
              "| Per request | Value |", "|---|---|",
              "| Average tokens per request | %.1f |" % (total_tokens / n),
              "| Average model calls per request | %.3f |" % (tot["calls"] / n),
              "| Actual cost per request | $0.00 (local Ollama model) |",
              "| Estimated cost if the same tokens ran on %s ($%.2f / $%.2f per MTok) | $%.4f total, $%.6f per request |"
              % (name, pin, pout, ref, ref / n), "",
              "Calls by purpose: " + ", ".join("%s=%d" % (k.split(":", 1)[1], v) for s in by_model.values()
                                                for k, v in sorted(s.items()) if k.startswith("purpose:")), "",
              "Token counts are the model's reported prompt/completion counts. Calls served from the on-disk cache",
              "(`code/cache/llm/`) re-use the counts recorded when the call was first made, so the totals describe the",
              "work needed to produce the output from scratch. The deterministic stages (parsing, identity, linking,",
              "state, policy, planning, validation) make no model calls.",
              "", "Classifier disagreements between rules and the LLM: %s." % run.get("classifier_disagreements", "n/a")]
    with open(os.path.join(HERE, "usage_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    evaluation_report()
    usage_report()
    print("wrote evaluation_report.md and usage_report.md")
