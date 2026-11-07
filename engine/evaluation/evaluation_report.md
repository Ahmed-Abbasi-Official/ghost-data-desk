# Evaluation report

All numbers come from `python code/evaluation/main.py`, which runs the full agent on the 32 public sample
requests and compares with `dataset/sample_output.csv`. Custom cases are in `custom_cases.csv`.

## Sample metrics (32 cases)

| Metric | Value |
|---|---|
| Case-level status accuracy | 100.0% |
| Step precision (all 7 fields incl. evidence_id) | 100.0% |
| Step recall (all 7 fields) | 93.8% |
| Step F1 (all 7 fields) | 96.8% |
| Step F1 ignoring evidence_id | 96.8% |
| Field-level accuracy (expected steps, per field) | 93.8% |
| Sequence accuracy (exact ordered steps_json per case) | 65.6% |

Step match = same phase, system_id, resource_id, operation, state and evidence_id. Field accuracy aligns
each expected step with the predicted step on the same resource and operation and scores each field.

## Remaining sample errors

| Missing operation | Resource type | Count |
|---|---|---|
| VERIFY_RECORD_ANONYMIZED | analytics | 6 |
| VERIFY_RECORD_ABSENT | account | 4 |
| VERIFY_RECORD_PRESENT | audit_record | 2 |

Most remaining misses are records that carry no identifier anywhere (already-anonymised analytics,
profiles visible only as a deletion-worker event) where several cases share one deletion batch. The
agent does not guess them; see the review queue below.

## Custom evaluation cases

Each case is a variant of a public sample (or a new request) whose correct answer is known.

| Case | Purpose | Expectation | Result |
|---|---|---|---|
| custom_01 | Marketing opt-out that uses the word "remove" must not authorise deletion | NOT_APPLICABLE | PASS (NOT_APPLICABLE, 0 steps) |
| custom_02 | Current email in upper case with stray spaces resolves to the same person as the historical email | SAME_AS_BASE | PASS (INCOMPLETE, 4 steps) |
| custom_03 | A different reported_status claim must not change the audited answer | SAME_AS_BASE | PASS (INCOMPLETE, 14 steps) |
| custom_04 | Evidence captured after audit_at (failover snapshot at 23:00) must not be cited | NO_LATE_EVIDENCE | PASS (COMPLETE, 1 steps) |
| custom_05 | A deletion request written in Urdu is still a deletion request | SAME_AS_BASE | PASS (INCOMPLETE, 5 steps) |

Unit tests (`python -m pytest code/evaluation -q`) cover the account linker (missing account, ambiguous tie,
conflicting signals, status-result batch match, direct evidence), request classification traps, step
ordering and evidence rules.

## Full run (518 requests)

| Status | Rows |
|---|---|
| COMPLETE | 156 |
| INCOMPLETE | 352 |
| NOT_APPLICABLE | 10 |

Account review queue (`code/evaluation/account_review.csv`, IDs only, status PENDING; no human
review has taken place):

| Reason | Cases |
|---|---|
| ACCOUNT_NOT_FOUND | 10 |
| AMBIGUOUS_ACCOUNT_LINK | 108 |

## Failures found and improvements made

| Iteration | Status acc. | Step F1 |
|---|---|---|
| First end-to-end run (rules-only classification, five text parsers) | 0.906 | 0.842 |
| Deletion-worker treatment disambiguates support message vs attachment and analytics; a single delete event within 15 minutes of the report links the account | 0.969 | 0.885 |
| State uses the record's own observation time; citations restricted to files captured by audit_at | 0.906 | 0.877 |
| Cite files that name the record before snapshots that merely omit it | 0.906 | 0.894 |
| Absence-by-omission only from full SQL table dumps (curated bundles no longer hide FOUND records) | 0.969 | 0.951 |
| Deletion-run attribution of identifier-free records (single report within 15 minutes only); screenshots via local VLM; SQL rows dated at snapshot time | 0.969 | 0.954 |
| Local LLM classification + deletion follow-up cues ("profile is gone", "cannot identify me") — final | 1.000 | 0.968 |

Other findings during development:
- Keyword rules alone mislabel free-form sample texts ("download your data link", "downloadable archive"),
  so only explicit keep-the-account statements veto deletion; the local LLM classifies the rest.
- The local 9B model labelled "remove me from marketing lists only" as DELETE in a smoke test; the
  keep-the-account veto catches it (custom_01).
- Direct identifier links cover only 14/32 sample profiles. Batch matching (delete result vs reported
  status, which agreed on 181/182 directly linked cases) plus elimination raised this to 27/32 with no
  wrong link; the remaining profiles go to the review queue instead of being guessed.
- Curated JSON bundles list only some records, so a record missing from a bundle is not proof of
  deletion when another file names it; full SQL table dumps are.
