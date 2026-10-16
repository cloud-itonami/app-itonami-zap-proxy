# Security assessment approval records

`zap_proxy_targets` and `allow_active` are not authorization. A scan requires a
current record in the JSON file named by `ZAP_PROXY_APPROVALS_FILE`. The file
must be owned by the process user and must not be writable by group or others;
missing, malformed, or unreadable records fail closed.

Hosted adapters resolve the ledger checker and network helper only beneath
`ZAP_PROXY_REPO_ROOT`, an absolute trusted checkout path supplied by the host.
The Hermes adapter sets it to the checkout it launches. Direct KBB/JVM
invocations must set the same variable; helper lookup never falls back to the
current working directory.

The file is operator-managed and must not be populated from an inbox parser.
Before recording a grant, a security operator must verify the named approver's
authority against the asset-owner or delegation record, preserve a reference to
the written approval, and enter the approved asset paths, modes, and expiry.
The evidence references are identifiers only; do not copy message contents or
secrets into this ledger. Questions, acknowledgments, automatic replies, and
delivery notices are not approvals.

Example structure (replace all example values with verified records):

```json
{
  "version": 1,
  "authorized_approvers": ["asset-owner@example.test"],
  "operators": ["security-operator"],
  "approvals": [
    {
      "id": "approval-2026-001",
      "status": "approved",
      "approval_method": "human_verified_written_scope",
      "approver": "asset-owner@example.test",
      "authority_verified_by": "security-operator",
      "authority_evidence_ref": "asset-registry:record-7",
      "reviewed_by": "security-operator",
      "recorded_by": "security-operator",
      "approval_evidence_ref": "mail-record:message-42",
      "approved_at": 1792110000,
      "reviewed_at": 1792110300,
      "not_before": 1792110300,
      "expires_at": 1792196400,
      "assets": ["https://example.test/app/"],
      "methods": ["GET"],
      "modes": ["passive"],
      "max_pages": 50
    }
  ]
}
```

`assets` entries are URL path prefixes with no query or fragment. Paths are
segment-bounded: `/app/` does not cover `/application/`. Each `approval_id` must
be supplied to the scan tool. The record's `not_before`, `expires_at`, `methods`,
and `max_pages` cap are checked at run time. Active probes require `"active"` in
`modes`. Records cannot be valid for more than 31 days. The runtime checks every
discovered URL before sending it. JVM redirects are not followed; the Node
transport does not follow redirects.

There is deliberately no email-to-approval function. An email response is only
evidence for a human reviewer to inspect; it never creates an authorization by
itself. See the corrected non-sending copy in
[`security-assessment-outreach.en.txt`](security-assessment-outreach.en.txt) and
[`security-assessment-outreach.ja.txt`](security-assessment-outreach.ja.txt).
