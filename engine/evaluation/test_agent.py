"""Unit tests: python -m pytest code/evaluation -q"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classify  # noqa: E402
import linker  # noqa: E402
import planner  # noqa: E402
import validate  # noqa: E402


class StubIdentities:
    def __init__(self, mapping):
        self.mapping = mapping

    def owner_of(self, value):
        return self.mapping.get(str(value).lower()) if value else None


def delete_event(aid, account, time, result):
    return {"activity_id": aid, "resource_id": account, "time": time, "result": result, "kind": "policy_action",
            "account_delete": True, "emails": [], "user_ids": [], "mentioned": []}


def case(cid, uid, status, reported="2026-09-05T12:00:00Z"):
    return {"case_id": cid, "uid": uid, "requested_at": "2026-09-03T12:00:00Z", "reported_at": reported,
            "reported_status": status, "audit_at": "2026-09-20T12:00:00Z"}


def test_missing_account_goes_to_review():
    out = linker.link_accounts([case("c1", "user_1", "COMPLETE")], [], [], StubIdentities({}))
    assert out["c1"]["accounts"] == []
    assert out["c1"]["reason"] == "ACCOUNT_NOT_FOUND"


def test_ambiguous_tie_is_not_guessed():
    events = [delete_event("a1", "account_11", "2026-09-05T10:50:00Z", "FAILED"),
              delete_event("a2", "account_22", "2026-09-05T11:00:00Z", "FAILED")]
    cases = [case("c1", "user_1", "NOT_STARTED"), case("c2", "user_2", "NOT_STARTED")]
    out = linker.link_accounts(cases, [], events, StubIdentities({}))
    for cid in ("c1", "c2"):
        assert out[cid]["accounts"] == []
        assert out[cid]["reason"] == "AMBIGUOUS_ACCOUNT_LINK"
        assert out[cid]["candidates"] == ["account_11", "account_22"]


def test_status_result_signal_resolves_batch():
    events = [delete_event("a1", "account_11", "2026-09-05T10:50:00Z", "SUCCESS"),
              delete_event("a2", "account_22", "2026-09-05T11:00:00Z", "FAILED")]
    cases = [case("c1", "user_1", "COMPLETE"), case("c2", "user_2", "NOT_STARTED")]
    out = linker.link_accounts(cases, [], events, StubIdentities({}))
    assert out["c1"]["accounts"] == ["account_11"] and out["c1"]["method"] == "BATCH"
    assert out["c2"]["accounts"] == ["account_22"]


def test_conflicting_signals_are_not_guessed():
    # Only candidates whose result disagrees with the status, and two of them, so timing cannot decide.
    events = [delete_event("a1", "account_11", "2026-09-05T10:40:00Z", "SUCCESS"),
              delete_event("a2", "account_22", "2026-09-05T10:45:00Z", "SUCCESS")]
    out = linker.link_accounts([case("c1", "user_1", "NOT_STARTED")], [], events, StubIdentities({}))
    assert out["c1"]["accounts"] == []
    assert out["c1"]["reason"] == "CONFLICTING_EVIDENCE"


def test_direct_evidence_wins_over_batch():
    obs = [{"kind": "record", "resource_id": "account_33", "observed_at": "2026-09-01T00:00:00Z",
            "file_id": "evidence_9", "identifiers": {"email": "a@x.com"}, "lookup_key": None}]
    events = [delete_event("a1", "account_11", "2026-09-05T10:50:00Z", "SUCCESS")]
    out = linker.link_accounts([case("c1", "user_1", "COMPLETE")], obs, events, StubIdentities({"a@x.com": "user_1"}))
    assert out["c1"]["accounts"] == ["account_33"] and out["c1"]["method"] == "DIRECT"


def test_marketing_opt_out_is_not_deletion():
    assert classify.rule_label("Please remove me from marketing lists only. I still want receipts.") == "NOT_APPLICABLE"
    assert classify.rule_label("Could you send me a copy? I still use it, so please don't delete it.") == "NOT_APPLICABLE"
    assert classify.rule_label("Je demande la suppression définitive du compte associé à cette adresse e-mail.") == "DELETE"


def _decision(rid, system, required, violation, evidence="evidence_1", kind="record"):
    return {"resource_id": rid, "system_id": system, "required": required, "violation": violation,
            "evidence": evidence, "kind": kind}


def test_plan_order_and_evidence_rules():
    status, steps = planner.plan([
        _decision("search_1", "search_service", "ABSENT", True),
        _decision("audit_record_1", "security_service", "PRESENT", False),
        _decision("data_flow_1", "integration_service", None, True, kind="flow"),
        _decision("account_1", "account_service", "ABSENT", False),
    ])
    assert status == "INCOMPLETE"
    ops = [(s["operation"], s["resource_id"]) for s in steps]
    assert ops == [("REPORT_REMAINING_RECORD", "search_1"), ("REPORT_ACTIVE_DATA_FLOW", "data_flow_1"),
                   ("DISABLE_DATA_FLOW", "data_flow_1"), ("DELETE_RECORD", "search_1"),
                   ("VERIFY_RECORD_ABSENT", "account_1"), ("VERIFY_RECORD_ABSENT", "search_1"),
                   ("VERIFY_RECORD_PRESENT", "audit_record_1"), ("VERIFY_DATA_FLOW_DISABLED", "data_flow_1")]
    assert all(s["evidence_id"] for s in steps if s["phase"] == "FINDING")
    assert not any(s["evidence_id"] for s in steps if s["phase"] != "FINDING")
    manifest = {"evidence_1": {"captured_at": "2026-09-01T00:00:00Z"}}
    assert validate.check_row({"audit_at": "2026-09-20T00:00:00Z"}, status, steps, manifest, {}) == []


def test_complete_case_cites_every_verification():
    status, steps = planner.plan([_decision("account_1", "account_service", "ABSENT", False)])
    assert status == "COMPLETE" and steps[0]["evidence_id"] == "evidence_1"
