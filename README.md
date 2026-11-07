# Ghost Data Desk

[![tests](https://github.com/Ahmed-Abbasi-Official/ghost-data-desk/actions/workflows/tests.yml/badge.svg)](https://github.com/Ahmed-Abbasi-Official/ghost-data-desk/actions/workflows/tests.yml)

**A Switch room where a privacy team and three AI agents make sure "deleted" really means deleted.**

Flip the Switch hackathon · Track: **Best Business Workflow** · Built with Switch, Mattermost and Claude Code

---

## The problem

When a customer asks a company to delete their data (GDPR [Article 17](https://gdpr-info.eu/art-17-gdpr/), the
"right to be forgotten"), the company has to erase it without undue delay. In practice the deletion workflow
says **"COMPLETE"**, and copies of the data are still in replicas, search indexes, support tools, analytics,
data connectors and backups.

- A developer sent 20 GDPR deletion requests in a year: 2 companies complied, 6 complied only after a
  regulator complaint, **12 never did**. One company changed the account email, said the data was deleted, and
  the data was still there. ([nikolak.com, Feb 2026](https://nikolak.com/gdpr-failure/))
- On the 518 deletion requests in this project's dataset, **162 of the 254 that the workflow marked COMPLETE
  (64%) still had personal data** when they were audited. Finding that by hand means going through 94 evidence
  files (SQL dumps, JSON exports, logs, PDFs, screenshots) per case.

Checking every copy, deciding what the policy says to delete, redact or keep, fixing it, and proving it to a
regulator is team work: a privacy officer, the engineers who own each system, and someone who checks the
result. Today that happens in spreadsheets and long email threads.

## What the desk does

```
 Customer: "delete my data"          Deletion workflow: "COMPLETE"  ✅ ...really?
                                                │
                                                ▼
 ┌──────────────────────── privacy-desk (a Switch room in Mattermost) ─────────────────────────┐
 │                                                                                             │
 │  👤 Privacy officer:  "@ghost-auditor please check case_483"                                │
 │                                                                                             │
 │  🔎 ghost-auditor     Workflow said COMPLETE. Evidence says INCOMPLETE: 4 problems in       │
 │                       4 systems (search index, support chat, live connector, backup).       │
 │                       Plan: steps 5-8. 📄 room document "audit-case_483"                    │
 │                                                                                             │
 │  👤 Privacy officer:  "@ghost-fixer approve case_483 skip 6"        🔒 only a person approves │
 │                                                                                             │
 │  🛠 ghost-fixer        Done: connector disabled, search entry deleted, chat redacted.         │
 │                       "@ghost-verifier please verify case_483"                              │
 │                                                                                             │
 │  ✅ ghost-verifier     Re-checks from the raw evidence, not from the fixer's word:            │
 │                       FAIL: backup_4600 can still restore the data. Certificate withheld.   │
 │                                                                                             │
 │  👤 Privacy officer:  "@ghost-fixer approve case_483"                                       │
 │  🛠 ghost-fixer        Done: backup restores blocked.                                         │
 │  ✅ ghost-verifier     VERIFIED. 📄 "certificate-case_483" + draft reply to the customer      │
 │                                                                                             │
 │  👤 Privacy officer:  "@ghost-verifier send reply case_483"    → case CLOSED                │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Agent | Does | Cannot do |
|---|---|---|
| **ghost-auditor** | Finds every copy of the person's data, proves it with evidence, proposes the plan. Scans all 518 requests in ~2 seconds. | change data, approve |
| **ghost-fixer** | Runs only the steps a person approved, and logs every change in a ledger. | verify its own work, approve, write to customers |
| **ghost-verifier** | Re-derives the required end state from raw evidence, issues the certificate, drafts the customer reply. | fix, approve on a person's behalf |
| **You** (privacy officer) | Approve the plan, hold steps back, confirm ambiguous accounts, approve the reply. | – |

Records the policy says to keep (security audit logs inside their retention period, legal holds, backups whose
restores are blocked) are kept, and the agents say why.

## Why Switch is central

Without Switch this is three scripts and a spreadsheet. With Switch it is a team workflow:

- **One room is the whole case file.** Requests, findings, approvals, actions and the certificate are in one
  shared log that people and agents read. That log is the accountability record a regulator asks for.
- **Approvals happen in chat, from people.** The fixer only acts on a person's `approve` message. The approver's
  name goes into the ledger and the certificate.
- **Agents hand work to each other in the room.** The fixer calls the verifier; the auditor hands clean cases
  straight to the verifier. Everyone sees each hand-off.
- **Room documents keep the record.** Each audit and certificate is saved as a Switch room document
  (`audit-case_483`, `certificate-case_483`); the deletion policy is a room document too.
- **Switch's access rules split the duties.** `ghost-fixer` answers only its owner (the privacy officer);
  `ghost-verifier` answers the owner and the owner's agents; `ghost-auditor` answers anyone in the room, so a
  teammate can ask "what is open?"
- **One click to set up.** [`switch/ghost-data-desk.template.yaml`](switch/ghost-data-desk.template.yaml) is a
  Switch template that creates the three agents and the room with its brief, policy document and kickoff.

## Run it yourself

You need: macOS or Linux, Python 3.9+, git, [Switch Console](https://docs.switchagents.ai/switch-rooms/getting-started/install-switch-console.md)
(it runs the local Switch server and Mattermost in Docker), and Claude Code signed in
(Switch Console → Settings → Agent providers should show **Claude Code · Signed in**).

```bash
git clone https://github.com/Ahmed-Abbasi-Official/ghost-data-desk.git
cd ghost-data-desk
./setup.sh          # venv + pypdf, fetches the public dataset, prints the stats table
```

Then in Switch Console:

1. **Templates → Import template** → choose `switch/ghost-data-desk.template.yaml` → **Save**.
2. **Use** → **Repository folder** = the absolute path `setup.sh` printed → **Create all**.
3. Open the **privacy-desk** room. The kickoff makes the auditor post the stats table and the other two agents
   introduce themselves.
4. Try it:

```
@ghost-auditor please check case_483
@ghost-fixer approve case_483 skip 6
@ghost-fixer approve case_483
@ghost-verifier send reply case_483
@ghost-auditor scan the cases the workflow marked COMPLETE
@ghost-auditor what is open?
```

More cases worth trying: `case_538` (says "remove me from marketing lists only", so nothing is deleted),
`case_47` (two accounts could be the person's, so the auditor asks you to pick one), `case_46` (already clean,
goes straight to the certificate).

To start a fresh desk with the same agents, run the template again with a new **Room name** and choose
**Existing agent** for each card.

## The `ghost` tool

The agents run one command-line tool. Every command prints Markdown that goes straight into the room.

```
ghost stats                       what the workflow said vs. what the evidence shows
ghost scan [--reported COMPLETE]  audit every request; cases whose data can come back by itself first
ghost lookup <email>              cases for an email, including old emails
ghost intake <case_id>            is it a deletion request, and who is the person
ghost audit <case_id>             what is left, the proof, and the plan
ghost link <case_id> <account> --confirmed-by NAME    a person resolves an ambiguous account
ghost fix <case_id> --approved-by NAME [--skip 6]     run approved steps, write the ledger
ghost verify <case_id>            independent re-check, certificate if it passes
ghost certificate <case_id>       print the certificate
ghost reply <case_id> [--approved-by NAME]            customer reply: draft, then queue and close
ghost status [case_id]            where every case is
```

## Three locks

```
Lock 1  Claude Code permissions   agents/<role>/.claude/settings.json deny the other roles' commands:
                                  the fixer cannot run verify, the verifier cannot run fix,
                                  no agent can edit the state files directly.
Lock 2  The tool itself           ghost fix and ghost reply refuse an approver that is an agent (ghost-*).
Lock 3  Independent verification  ghost verify re-runs the audit from the raw evidence and compares it with
                                  the ledger, so a wrong or partial fix cannot pass.
```

The room never shows full emails or phone numbers (`t•••@gmail.com`).

## How an audit works

The audit engine is the deletion-audit agent I built for HackerRank Orchestrate (October 2026), reused here as a
library. The hackathon FAQ allows existing agents and libraries; the Switch room, the agents, the workflow and
the `ghost` tool were built during Flip the Switch.

```
case_483
  [0] ingest     94 evidence files: SQL → sqlite3, JSON, logs, handover notes, PDF → pypdf,
                 screenshots → local vision model (answers cached in engine/cache, no model needed to run)
  [1] classify   multilingual rules + local LLM: is it really a deletion request?
  [2] identity   current and old emails, phones, sha256 email hashes → user_481
  [3] link       which account_* belongs to the person (ambiguous → ask a person)
  [4] state      state of every copy at audit time + deletion_rules.csv (DELETE / REDACT / ANONYMIZE / RETAIN)
  [5] risk       live connectors and restorable backups that would bring the data back
  [6] plan       FINDING → REMEDIATION → VERIFICATION steps
```

On the 32 public labelled cases the engine gets the status right in **32/32**, with step precision **1.00**
and recall **0.94** (`engine/evaluation/sample_metrics.json`).

## What is real and what is simulated

- **Real:** the Switch room, agents, hand-offs, approvals and documents; the audit of all 518 requests against
  94 evidence files; the policy decisions; verification; certificates.
- **Simulated:** the fix. `ghost fix` changes a sandbox copy of the estate (`state/estate.json`) and writes a
  ledger, because the dataset is a snapshot of a fictional company, not live systems. In a real deployment this
  step calls each system's API; nothing else changes.
- **Data:** the public [HackerRank Orchestrate October 2026 dataset](https://github.com/interviewstreet/hackerrank-orchestrate-october26)
  for the fictional company Northstar Shop. Every person and record in it is synthetic. `setup.sh` downloads it;
  it is not copied into this repository.

## Layout

```
switch/ghost-data-desk.template.yaml   the Switch template: 3 agents + room + policy doc + kickoff
agents/{auditor,fixer,verifier}/       each agent's folder: CLAUDE.md / AGENTS.md brief + permissions
bin/ghost, tools/ghost.py              the tool the agents run
engine/                                the deletion-audit engine (ingest, classify, identity, link, state, plan)
tests/test_desk.py                     end-to-end workflow tests
setup.sh                               one-time setup
state/                                 runtime: cases, ledger, sandbox estate, certificates, outbox (git-ignored)
```

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

They run the whole workflow on real cases: a non-deletion request left alone, an agent's approval refused, a
partial fix failing verification, the full fix passing, a person resolving an ambiguous account, the scan, and
the customer reply gated on verification and a person's approval.

## What's next

- Connect the fixer to real systems (database, search index, support tool, connector and backup APIs).
- Use Switch roles (`auditor`, `fixer`, `verifier`) so a team can swap the agent behind a job.
- Add an intake room for the support team, linked to the privacy desk, so requests arrive where customers write.
