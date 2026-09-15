# Case study: evidence to one approved synthetic DEV update

One Helix specialist needs two separate MCP capability providers. **Helix MCP
Knowledge** supplies versioned documentary and project evidence; **Helix MCP
Gateway** observes an authorized environment and governs any write. This case
shows how those boundaries supported one small, human-approved change to a
synthetic configuration item in DEV.
The documentary retrieval was checked in a local Knowledge session; the
approved Gateway write was a separate validation. The two paths are joined
here as a reviewable method, not represented as one uninterrupted agent run.

## Outcome first

In the private validation, one non-sensitive field on one synthetic DEV record
was initially empty. Gateway produced a temporary, non-mutating update plan for
a fictional marker. The user reviewed the exact proposal and approved it in a
later message. Gateway applied the plan once, and a bounded read confirmed the
marker. The human operator later cleared the demonstration field through the
authorized DEV interface; a subsequent Gateway read confirmed the original
empty state. No QA or PROD write was part of this validation.

This page omits the physical form, field, entry identifier, dataset selector,
marker text, private runbook, BMC document text, credentials, endpoints, raw
records, plan ID and digest. It is a **sanitized case narrative**, not a public
replay of a private Helix environment.

## Where the two servers contributed

| Stage | Capability and evidence | What it established — and what it did not |
| --- | --- | --- |
| Documentary context | Knowledge searched official CMDB 26.1 evidence with `source_scope=bmc_official` and an explicitly selected validation runbook with `source_scope=project`; selected sections were expanded. | Versioned sources and a local procedure were retrievable. Neither source proved the live record's state or granted write permission. |
| Live observation | Gateway selected `dev`, checked readiness and used a bounded form read for one authorized synthetic record and its current value. | The field was empty at the time of that read. Documentation alone could not establish this. |
| Exact proposal | Gateway policy admitted only the demonstration's DEV form and non-sensitive field; `plan_update_entry` stored one proposal, digest, expiry and current-state precondition. | Planning did not change Helix and was not human approval. The form and field names remain private. |
| Human review | The proposal was shown with current and proposed values, reason, target, digest and remaining lifetime. Approval arrived in a later user message; the same pending plan was retrieved and compared. | Approval applied to that plan, not to a replacement or a broader remediation objective. |
| Action and verification | `apply_update_entry` returned a known applied result; an immediate bounded `query_form` read matched the fictional proposal. | The read verified this field on this record, not broader CMDB quality or production suitability. |
| Audit and cleanup | Gateway recorded allowlisted operation outcomes; the human later cleared the marker and a bounded read checked the empty state. | Audit metadata proves tool outcomes, not business payload equality. Cleanup was a separate human action, not a second MCP write. |

Knowledge's two retrieval scopes must remain separate. A project runbook can
identify an installation-specific selector and purpose but cannot override BMC
product constraints, Gateway policy or Helix account permissions. Conversely,
Gateway's successful read does not make an agent's interpretation of the
documentation correct. The agent must say which layer supports each claim.

## The governed sequence

The [complete integrated procedure](integrated-controlled-update-case.md)
specifies the private prerequisites and failure paths. Its public prompt can
be shortened to this reviewable task shape:

```text
Use Knowledge to confirm the selected official product/version evidence and
the explicit authorized project procedure. Expand the relevant sections and
keep their provenance separate. Use Gateway only against the authorized
synthetic DEV target. Read exactly one record and prepare one non-sensitive
update plan. Display the target, current and proposed values, reason, plan
digest and expiry, then stop. In a later turn, apply only if I explicitly
approve that unchanged pending plan. Verify the result with a bounded read.
Never replace an expired or altered plan silently, retry an uncertain write,
or modify QA or PROD as part of this task.
```

The key transition is **plan → separate human approval → same-plan apply**.
The agent can gather evidence and prepare a reviewable operation autonomously;
it does not receive authority to execute a sensitive change invisibly.

## What was checked

On 15 September 2026, a read-only check against the author's managed Knowledge
`1.31.5` instance found official CMDB 26.1 and the private validation project
separately. Each bounded search returned version-tagged results in its requested
scope, and `get_section` returned chunks from the same selected document.
The selected matches were lexical; semantic search was disabled. Titles,
sections and available source metadata were inspected privately but are not
copied here.

During the controlled Gateway validation on the same date, closed-schema audit
metadata recorded a successful DEV update plan, successful plan retrieval,
one successful DEV apply call and an immediate successful bounded read. The
approved-plan comparison and the field-value equality came from the reviewed
tool results, **not** from the audit file: the audit deliberately excludes
plan IDs, digests, fields, values and returned records. The later cleanup read
was also observed separately. Gateway's reference version was `0.10.3`.

Separate hermetic Gateway tests passed for an expired plan and a mismatched
digest. They showed rejection before a fake Helix client write, but those
negative paths were **not** attempted as live writes in DEV. A successful
update in this case likewise does not demonstrate a concurrent-change
conflict; Gateway's stored precondition is a control of the implementation,
not an exercised failure in this run.

## Limits and safe reproduction

This was a controlled internal validation, not a benchmark of uninterrupted
autonomous-agent operation across both MCP sessions. The managed Knowledge
evidence check and the approved Gateway execution are reported as distinct
observations; this page does not assume that a reader's MCP client has indexed
the same private project or claim that one public transcript proves every step
in a single session. Actual deployment-version
alignment must be confirmed by an authorized operator before repeating a
version-specific decision.

To repeat the method, an installation needs its **own** authorized synthetic
DEV record, privately reviewed mapping, non-sensitive test field, local
runbook, matching product/version context, narrowly scoped Gateway policy
and a human who can approve and supervise cleanup. Never copy this author's
private identifiers or assume a high-level `form_update` capability permits
the exact field. Do not plan if the read finds an unexpected value or more
than one record; do not apply if the plan changes or expires; investigate an
`outcome_unknown` rather than retrying automatically.

For the Knowledge-only, fully public and copyright-safe first answer, see the
[fictional evidence case](knowledge-source-grounded-answer-case.md). For the
Gateway write-plan controls without the documentary layer, see the
[controlled form-update case](https://github.com/hvolckaert/helix-mcp-gateway/blob/main/docs/use-cases/controlled-form-update.md).
