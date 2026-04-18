## Critic Instructions

> **READ THIS FIRST if you are the critic subagent.**
>
> Your job is to **challenge** the producer's artifact. You are a reviewer, not a summariser.
>
> - **DO NOT** summarise the artifact.
> - **DO NOT** reproduce the project doc or any section of it.
> - **DO NOT** restate what the producer wrote.
> - **WRITE ONLY your critique**: what is wrong, missing, risky, or untested.
> - Every issue must be rated **BLOCKING** / **SIGNIFICANT** / **MINOR**.
> - Your final output must be a verdict: **APPROVED** or **NEEDS_FIXES**.
>
> If you are the **qa_subagent** (REVIEW state critic): read the *review checklist* file (not the project doc). Your job is to find what the reviewer missed or glossed over. Verify claims against actual system state where possible.

---

## Inter-Agent Review: [STAGE] - [Project Name]

**Stage:** [BRIEF | ARCHITECTURE | PLAN | REVIEW]
**Project:** [project-name]
**Round:** [1 | 2 | 3]
**Date:** [YYYY-MM-DD]

> **Review-loop note:** round 3 is the hard cap unless an explicit operator override is recorded. If this round still leaves unresolved BLOCKING issues, record the operator checkpoint plus the canonical post-cap decision (`FREEZE_AND_ESCALATE`, `APPROVE`, or `CANCEL`) instead of silently starting round 4.

---

## Producer's Artifact Summary

**Producer role:** [pa | architect_subagent | planner_subagent | reviewer_subagent]
**Artifact location:** [projects/<name>.md#section or file path]

[Brief summary of what was produced - key decisions, approach, scope]

---

## Critic's Issues

**Critic role:** [domain_expert_subagent | senior_engineer_subagent | tech_lead_subagent | qa_subagent]

Use severity tags: **BLOCKING** (must fix before sign-off) / **SIGNIFICANT** (should fix) / **MINOR** (optional improvement)
Use resolution tags: **accept** / **reject** / **partial** (filled in by producer)

| # | Severity | Issue | Resolution | Notes |
|---|----------|-------|------------|-------|
| 1 | BLOCKING | [Issue description] | [accept/reject/partial] | [Producer's response] |
| 2 | SIGNIFICANT | [Issue description] | [accept/reject/partial] | [Producer's response] |
| 3 | MINOR | [Issue description] | [accept/reject/partial] | [Producer's response] |

---

## Producer's Responses

[For each issue above, explain how it was addressed or why it was rejected. Be specific about what changed in the artifact.]

**Issue 1:** [Response]
**Issue 2:** [Response]
**Issue 3:** [Response]

---

## Agreed Final Changes

[List the concrete changes that will be (or have been) made to the artifact as a result of this review round:]

- [ ] [Change 1]
- [ ] [Change 2]
- [ ] [Change 3]

---

## Sign-Off Block

Use this block together with the canonical `review-status` / `status --verbose` output.

Normal path:
- all three parties sign off
- `inter_agent_review.signed_off = true`
- `inter_agent_review.gate_satisfied = true`

Frozen-cap path:
- producer and/or critic may remain waived after the cap
- PM still signs off after the checkpoint, decision, and freeze artifact are recorded
- `inter_agent_review.signed_off` can stay `false`
- `inter_agent_review.gate_satisfied` is the go/no-go field that becomes `true` when the frozen-cap contract is valid

**Producer sign-off:**
- [ ] I have addressed all BLOCKING issues, or I have recorded what remains unresolved in the checkpoint / freeze path
- [ ] The artifact reflects the agreed changes or the explicit frozen carry-forward state
- **Signed:** [producer_role] | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | NEEDS_REVISION | WAIVED_FROZEN_CAP]

**Critic sign-off:**
- [ ] All BLOCKING issues are resolved, or they are explicitly captured in the checkpoint / freeze path
- [ ] Remaining SIGNIFICANT issues are either resolved or explicitly accepted as risk
- **Signed:** [critic_role] | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | NEEDS_REVISION | WAIVED_FROZEN_CAP]

**PM sign-off:**
- [ ] Artifact meets the goals stated in the brief
- [ ] No scope creep introduced
- [ ] Ready for operator review because `inter_agent_review.gate_satisfied` is true under the active path
- **Signed:** pa (PM) | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | ANOTHER_ROUND_REQUESTED | FREEZE_AND_ESCALATE | CANCEL]

## Round-Cap Checkpoint

Fill this section in whenever the critic does not approve a round. After round 3, this checkpoint is mandatory before any frozen-cap decision can be treated as valid.

- **Current round:** [1 | 2 | 3]
- **Max rounds:** 3
- **Another round permitted:** [yes | no]
- **Unresolved BLOCKING issues:**
  - [Issue]
- **Unresolved SIGNIFICANT issues:**
  - [Issue]
- **Producer response status:** [not started | in progress | responded]
- **Canonical next action:** [record-review-checkpoint | record-review-loop-decision | record-freeze-artifact | operator review]
- **Decision after cap:** [n/a | FREEZE_AND_ESCALATE | APPROVE | CANCEL]
- **Checkpoint file:** [projects/<name>-review-checkpoint-<state>-roundN.md]
- **Freeze artifact file:** [projects/<name>-freeze-artifact-<state>.md | n/a]

> If the canonical post-cap decision is `CANCEL`, forward transitions stay blocked until the project is explicitly resolved.

---

## Round History

| Round | Blocking issues | Significant issues | Minor issues | Outcome |
|-------|-----------------|--------------------|--------------|---------|
| 1     | [n]             | [n]                | [n]          | [agreed/another_round] |
