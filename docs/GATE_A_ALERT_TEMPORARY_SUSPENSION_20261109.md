# Gate A warning email temporary suspension

This candidate suppresses only repository-generated Gate A Approval Check warning
mail while the Daily Duck incident is being recovered. It does not suspend the
workflow, approval checks, candidate mail, or GitHub's standard notifications.

There are two custom-mail paths: the Gate A workflow's failure notification
(`FAILURE_STAGE=Gate A approval check`) and Automation Chain Audit's pending alerts
for `The Daily Duck - Gate A Approval Check`. Both use the single switch
`GATE_A_ALERTS_TEMPORARILY_SUSPENDED` in `scripts/send_workflow_failure_email.py`.
Gate A problems remain in the audit report and still cause audit failure. They
are excluded from pending email alerts, so the existing merge step does not mark
suppressed warnings as delivered. Mixed audit email retains the other warnings.

No workflow, protection hash, approval code, delivery/attempt marker, publication
code, or November 8 recovery branch is changed. Production application requires
separate Human Gate approval; local tests do not authorize push or merge.

To resume, confirm that current-issue candidate delivery, Gate A artifact retrieval
and reply processing have recovered, then obtain explicit notification-resumption
approval. Set `GATE_A_ALERTS_TEMPORARILY_SUSPENDED = False`, run the targeted
suspension/audit tests and protected tests, and review/apply that change through
the normal Human Gate. No marker or delivered-alert cache deletion is needed.
On the next normal run, direct warnings resume and current unseen Gate A problems
again become eligible for audit mail. Do not manually dispatch or test-send SMTP
as part of this reversal without separate authorization.
