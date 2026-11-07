"""Phase 1: does the request text authorise deletion?

Two independent signals: multilingual intent rules and a local LLM over the unique texts (batched, cached).
A non-deletion cue (opt-out only, access/copy, correction, explicit "don't delete") vetoes deletion.
"""
import os
import re

import llm

DELETE_CUES = [
    r"\bdelet", r"\berase", r"\berasure", r"\bremov", r"\bclos(?:e|ed|ing)\b", r"\bclean",
    r"account[- ]deletion", r"suppression", r"exclus[aã]o", r"eliminaci[oó]n", r"削除", r"삭제",
    # follow-ups that ask to confirm a deletion or de-identification
    r"(?:profile|account|data) is gone", r"cannot identify me", r"still exist",
]
# Explicit statements that the person wants to keep the account or is not asking for deletion.
KEEP_CUES = [
    r"not asking for delet", r"not a request to (?:delete|erase|remove|close)", r"(?:don't|do not) (?:delete|close|remove)",
    r"keep the account", r"leave (?:the|my) (?:account|profile)", r"still (?:need|want|use)", r"continue using",
    r"without removing", r"lists? only",
]

SYSTEM = (
    "You classify customer privacy requests for an online shop. Label DELETE only when the person asks to delete, "
    "erase, or permanently close their account or personal data (any language). Label NOT_APPLICABLE for every "
    "other request: access or a copy of data, data export or download, correcting or changing details, marketing "
    "or promotional opt-out (even if it says 'remove me from lists'), login help, invoices, or any text that says "
    "not to delete. Answer with JSON only."
)


def rule_label(text):
    t = text.lower()
    if any(re.search(p, t) for p in KEEP_CUES):
        return "NOT_APPLICABLE"
    if any(re.search(p, t) for p in DELETE_CUES):
        return "DELETE"
    return None


def llm_labels(texts, batch=25):
    labels = {}
    if llm.PROVIDER == "none" or os.environ.get("GHOST_CLASSIFY_LLM", "1") == "0":
        return labels
    for start in range(0, len(texts), batch):
        chunk = texts[start:start + batch]
        numbered = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(chunk))
        user = ("Classify each numbered request. Return {\"labels\": [{\"i\": <number>, \"label\": "
                "\"DELETE\" or \"NOT_APPLICABLE\"}]} with one entry per request.\n\n" + numbered)
        try:
            out = llm.chat_json("classify_request", SYSTEM, user, max_tokens=2048)
        except Exception:
            continue
        for item in out.get("labels", []):
            try:
                idx = int(item["i"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(chunk) and item.get("label") in ("DELETE", "NOT_APPLICABLE"):
                labels[chunk[idx]] = item["label"]
    return labels


def classify_all(texts, canonical=()):
    """Return {text: {"label", "rule", "llm", "agree"}} for every distinct text.

    `canonical` texts (the participant request files) are always batched the same way so cached answers are
    reused; any other text (e.g. a custom evaluation case) goes into its own extra batch.
    """
    unique = sorted(set(texts))
    base = sorted(set(canonical))
    extra = sorted(set(unique) - set(base))
    model = llm_labels(base) if base else {}
    model.update(llm_labels(extra))
    result = {}
    for t in unique:
        rule, model_label = rule_label(t), model.get(t)
        if rule == "NOT_APPLICABLE":
            label = "NOT_APPLICABLE"
        elif rule == "DELETE":
            label = "DELETE"
        else:
            label = model_label or "NOT_APPLICABLE"
        result[t] = {"label": label, "rule": rule, "llm": model_label,
                     "agree": rule is None or model_label is None or rule == model_label}
    return result
