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

All three parties must sign off before the transition becomes available for operator approval.

**Producer sign-off:**
- [ ] I have addressed all BLOCKING issues
- [ ] The artifact reflects the agreed changes
- **Signed:** [producer_role] | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | NEEDS_REVISION]

**Critic sign-off:**
- [ ] All BLOCKING issues are resolved
- [ ] Remaining SIGNIFICANT issues are either resolved or explicitly accepted as risk
- **Signed:** [critic_role] | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | NEEDS_REVISION]

**PM sign-off:**
- [ ] Artifact meets the goals stated in the brief
- [ ] No scope creep introduced
- [ ] Ready for operator review
- **Signed:** pa (PM) | **Date:** [YYYY-MM-DD] | **Status:** [APPROVED | ANOTHER_ROUND_REQUESTED]

---

## Round History

| Round | Blocking issues | Significant issues | Minor issues | Outcome |
|-------|-----------------|--------------------|--------------|---------|
| 1     | [n]             | [n]                | [n]          | [agreed/another_round] |
