"""End-to-end checks of the desk workflow: python -m unittest discover tests"""
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHOST = os.path.join(ROOT, "bin", "ghost")


class DeskFlow(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ghost-state-")

    def ghost(self, *args, ok=True):
        env = dict(os.environ, GHOST_STATE_DIR=self.state)
        run = subprocess.run([GHOST, *args], capture_output=True, text=True, env=env, cwd=ROOT)
        if ok:
            self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout + run.stderr

    def test_not_a_deletion_request_is_left_alone(self):
        out = self.ghost("intake", "case_538")
        self.assertIn("Not a deletion request", out)
        self.assertIn("nothing to audit", self.ghost("audit", "case_538"))

    def test_people_approve_fixer_fixes_verifier_certifies(self):
        audit = self.ghost("audit", "case_483")
        self.assertIn("Workflow said **COMPLETE**. Evidence says **INCOMPLETE**", audit)
        self.assertIn("approve case_483", audit)

        refused = self.ghost("fix", "case_483", "--approved-by", "ghost-auditor", ok=False)
        self.assertIn("A person must approve", refused)

        self.ghost("fix", "case_483", "--approved-by", "Privacy Officer", "--skip", "6")
        failed = self.ghost("verify", "case_483")
        self.assertIn("Not verified", failed)
        self.assertIn("backup_4600", failed)
        self.assertNotIn("Deletion certificate", self.ghost("certificate", "case_483", ok=False))

        second = self.ghost("fix", "case_483", "--approved-by", "Privacy Officer")
        self.assertIn("done step 6", second)
        self.assertNotIn("done step 5", second)  # already carried out under the first approval
        self.assertIn("**Verified.**", self.ghost("verify", "case_483"))

        cert = self.ghost("certificate", "case_483")
        self.assertIn("held back steps 6", cert)
        self.assertIn("audit_record_4738 | PRESENT | rule_12 RETAIN", cert)

    def test_clean_case_goes_straight_to_verification(self):
        self.assertIn("Evidence agrees", self.ghost("audit", "case_46"))
        self.assertIn("**Verified.**", self.ghost("verify", "case_46"))

    def test_person_confirms_an_ambiguous_account(self):
        self.assertIn("Needs a person", self.ghost("audit", "case_47"))
        self.ghost("link", "case_47", "account_617", "--confirmed-by", "Privacy Officer")
        audit = self.ghost("audit", "case_47")
        self.assertNotIn("Needs a person", audit)
        self.assertIn("confirmed by Privacy Officer", audit)

    def test_scan_puts_cases_whose_data_can_come_back_first(self):
        out = self.ghost("scan", "--reported", "COMPLETE", "--top", "3")
        self.assertIn("254 reported COMPLETE requests", out)
        self.assertIn("**162 need work**", out)
        rows = [line for line in out.splitlines() if line.startswith("| case_")]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(line.rstrip().endswith("| yes |") for line in rows))

    def test_customer_reply_needs_verification_and_a_person(self):
        self.assertIn("must pass `ghost verify`", self.ghost("reply", "case_483", ok=False))
        self.ghost("audit", "case_483")
        self.ghost("fix", "case_483", "--approved-by", "Privacy Officer")
        self.ghost("verify", "case_483")
        draft = self.ghost("reply", "case_483")
        self.assertIn("Security audit logs: kept only for the required security retention period", draft)
        self.assertIn("Not sent.", draft)
        self.assertIn("A person must approve", self.ghost("reply", "case_483", "--approved-by", "ghost-verifier",
                                                         ok=False))
        self.assertIn("case_483 is closed", self.ghost("reply", "case_483", "--approved-by", "Privacy Officer"))
        self.assertIn("CLOSED", self.ghost("status", "case_483"))

    def test_cannot_fix_before_audit(self):
        self.assertIn("Nothing to fix", self.ghost("fix", "case_62", "--approved-by", "Someone", ok=False))


class SeparationOfDuties(unittest.TestCase):
    def deny(self, agent):
        with open(os.path.join(ROOT, "agents", agent, ".claude", "settings.json"), encoding="utf-8") as fh:
            return json.load(fh)["permissions"]["deny"]

    def test_each_agent_is_blocked_from_the_other_jobs(self):
        self.assertIn("Bash(../../bin/ghost fix:*)", self.deny("auditor"))
        self.assertIn("Bash(../../bin/ghost verify:*)", self.deny("fixer"))
        self.assertIn("Bash(../../bin/ghost fix:*)", self.deny("verifier"))


if __name__ == "__main__":
    unittest.main()
