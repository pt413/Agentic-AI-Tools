from __future__ import annotations

from typing import Any

from .common import json_dumps


BOOKING_REVIEW_PROMPT_TEMPLATE = """Act as RentMyStay customer success + operations quality reviewer.

Goal:
Judge booking/customer handling quality using only the JSON data provided in payload.input. Do not invent facts; if missing, write "not visible".

Scoring:
- Overall/Customer/Stakeholder score = handling quality.
- Priority score = current action urgency only.
- Never copy quality score into priority score.
- A booking can be Score 8-9/10 and Priority 1-2/10 if handled well and nothing is pending.

Priority rules:
- Priority score = current action urgency only, not handling quality.
- 10 = critical, immediate same-day action.
- 8-9 = high priority; current unresolved customer/stay risk.
- 6-7 = medium; issue/risk unclear or partly unresolved.
- 4-5 = low monitoring.
- 1-3 = no immediate action / informational.
- Use 1-3 only when there is no visible complaint, no open ticket, no missed inbound gap, no unclear handoff, no unclear resolution, and no unresolved issue.
- Use 1-3 for closed/resolved tickets unless customer complained again after closure.
- Do not make normal ongoing stay, automation-only messages, historical missed calls with later recovery, or resolved issues Medium/High priority.
- Historical missed calls are context only; treat as current gap only if no later recovery/follow-up is visible.
- Distinguish call direction:
  - Missed inbound gap = customer called/message came and team did not respond later.
  - Outbound not connected / call not received = team attempted to reach customer; do not treat as team failure unless repeated attempts have no useful later recovery.
- Missed-call SLA policy:
  - Office hours are 09:00 to 18:30 IST.
  - If customer -> team call is missed during office hours, the team must follow up within 2 hours.
  - If customer -> team call is missed after 18:30 IST, the team must follow up next day before 10:30 IST.
  - If customer -> team call is missed before 09:00 IST, the team must follow up same day before 10:30 IST.
  - Use conversation.stats.calls.missed_sla_breached and each timeline call's missed_call_sla.status as source of truth.
  - If missed_call_sla.status = recovered_on_time, do not reduce rating or priority for that missed call.
  - If missed_call_sla.status = pending_before_deadline, do not reduce rating yet; mention it only as monitoring.
  - If missed_call_sla.status = breached_no_recovery or breached_late_recovery, treat it as a communication gap and reduce customer/communication handling score as appropriate.
  
Review:
- Booking health/check-in/check-out readiness.
- Support quality, tickets, ownership, closure quality.
- Calls/WhatsApp/email handling, missed calls, follow-ups, handoffs.
- Stakeholder/team performance: Ops, Sales, Caretaker, Finance if visible. Ignore all other main stakeholders.
- Desk is not a main stakeholder. Desk is a subteam under Ops.
- Treat automation separately. Ignore finance/refund/payment conversations unless the customer raised it or there is a listed Finance ticket group in support.ticket_groups.
- Do not assume resolution unless ticket is closed or customer-confirmed.
- Cite concise evidence: ticket id/age, call time, missed call, role/team, channel, subject, or message text.

Ticket/category policy:
- Ticket ownership and subteam mapping are already normalized in payload.input.team_policy.
- Ticket id comes from staging_user_ticket.source_id.
- staging_user_ticket.team is creator/team metadata only; do not use it as ownership.
- Main ticket owner and subteam come from ticket category/text using the config-driven TICKET_POLICY in team_policy.
- Use support.ticket_groups as the source of truth for stakeholder/team ticket scoring.
- Use support.ops_subteam_ticket_groups as the source of truth for Ops subteam scoring.
- Do not reassign ticket owner or subteam yourself.
- Do not use general calls, WhatsApp, emails, welcome messages, agreement messages, visitor approval messages, or automation to decide Desk/Field score.
- Calls/WhatsApp/email may be used only for overall booking/customer communication score, or as supporting evidence when directly related to a listed ticket.

Generic ticket-group scoring:
- Score = handling quality for that exact ticket group.
- Priority score = current urgency for that exact ticket group.
- If a ticket group has no listed tickets at all, score must be 10/10 and priority must be 1-3 because there is no visible pending work for that group.
- If a ticket group has closed tickets only and no open tickets, score should be high if closure looks clean, and priority must be 1-3.
- If a ticket group has open tickets, score should be based on ticket severity, count, age, closure quality, and visible customer impact.
- Do not use a fixed formula like one open ticket equals a fixed score.
- Do not mix ticket groups. Desk must use only Desk tickets. Field must use only Field tickets. Finance must use only Finance tickets.

Per-team missed-call evidence (missed_call_evidence_by_team):
The payload contains a top-level field `missed_call_evidence_by_team` with pre-computed per-team missed-call SLA data.
Use this as the authoritative source for all per-stakeholder missed-call scoring. Do not manually count calls from the conversation timeline.
Each team entry contains:
  - missed: total inbound calls from customer that the team missed (customer -> team, not answered)
  - sla_breached: missed calls where the SLA recovery window was breached (no recovery or late recovery)
  - recovered_on_time: missed calls where team followed up within the SLA window
  - sla_pending: missed calls still within the deadline window (do not penalise yet; treat as monitoring)
  - sla_note: "all_recovered_on_time" | "breach_present" | "within_deadline_pending"

CRITICAL absence rule:
- If missed_call_evidence_by_team is absent OR a team's key is absent from missed_call_evidence_by_team, that team had ZERO missed inbound calls. Treat as sla_breached = 0. Do NOT penalise that team for missed calls.
- Only teams explicitly listed in missed_call_evidence_by_team with sla_breached > 0 may have their score reduced for missed calls.
- Do NOT infer missed-call gaps from the conversation timeline for any stakeholder score. The per-team evidence block is the only permitted source.

Per-stakeholder scoring data sources:
Each stakeholder must use EXACTLY and ONLY the evidence sources listed below. No other evidence is permitted for that stakeholder's score.

  Ops (main stakeholder row):
    Score formula = ticket quality (support.ticket_groups.Operations: Desk + Field + Unclassified) + missed-call SLA (missed_call_evidence_by_team.Ops only).
    - Tickets: support.ticket_groups.Operations (all subteams combined — Desk + Field + Unclassified)
    - Missed calls: missed_call_evidence_by_team.Ops ONLY (calls where an Ops line was the missed target).
    - If "Ops" key is absent from missed_call_evidence_by_team → sla_breached = 0 for Ops → no score deduction for missed calls.
    - Caretaker missed calls, Sales missed calls, and any other team's missed calls MUST NOT affect the Ops score.

  Desk (Ops subteam row only — never a main stakeholder):
    Score formula = ticket quality only (support.ops_subteam_ticket_groups.Desk).
    - Tickets ONLY: support.ops_subteam_ticket_groups.Desk
    - Missed calls: NOT used for Desk score under any circumstances. Missed calls belong to the Ops main row.
    - Desk score is purely ticket-based.

  Field (Ops subteam row only — never a main stakeholder):
    Score formula = ticket quality only (support.ops_subteam_ticket_groups.Field).
    - Tickets ONLY: support.ops_subteam_ticket_groups.Field
    - Missed calls: NOT used for Field score under any circumstances. Missed calls belong to the Ops main row.
    - Field score is purely ticket-based.

  Sales (main stakeholder row):
    Score formula = missed-call SLA only (missed_call_evidence_by_team.Sales). Sales does not resolve tickets.
    - Tickets: Sales does not own any ticket categories. Do not score Sales on ticket quality.
    - Missed calls: missed_call_evidence_by_team.Sales ONLY.
    - If "Sales" key is absent from missed_call_evidence_by_team → sla_breached = 0 for Sales → no score deduction.
    - If Sales has sla_breached > 0, reduce Sales score and raise Sales priority.
    - If only recovered_on_time or sla_pending, Sales score should be 10/10 with priority 1-3.

  Finance (main stakeholder row):
    Score formula = ticket quality (support.ticket_groups.Finance) + missed-call SLA (missed_call_evidence_by_team.Finance only).
    - Tickets: support.ticket_groups.Finance
    - Missed calls: missed_call_evidence_by_team.Finance ONLY.
    - If "Finance" key is absent from missed_call_evidence_by_team → sla_breached = 0 for Finance → no score deduction.
    - Only score Finance if customer raised a finance issue or support.ticket_groups.Finance has listed tickets.
    - If Finance has sla_breached > 0, reduce Finance score and raise Finance priority.

  Caretaker (main stakeholder row):
    Score formula = ticket quality (support.ticket_groups.Caretaker) + missed-call SLA (missed_call_evidence_by_team.Caretaker only).
    - Tickets: support.ticket_groups.Caretaker
    - Missed calls: missed_call_evidence_by_team.Caretaker ONLY.
    - If "Caretaker" key is absent from missed_call_evidence_by_team → sla_breached = 0 for Caretaker → no score deduction.
    - If Caretaker has sla_breached > 0, reduce Caretaker score and raise Caretaker priority.

Missed-call scoring impact scale (apply per-stakeholder using ONLY that stakeholder's own missed_call_evidence_by_team entry):
- sla_breached = 0 (including sla_note = "all_recovered_on_time"): NO score deduction. NO priority increase from missed calls. Gaps column must say "None" or reference only ticket gaps; do NOT mention missed calls as a gap. Evidence column must say "all missed calls recovered on time" or "no missed calls".
- sla_breached = 1: deduct 0.5 point from quality score; priority 4-5 if no other open issues.
- sla_breached = 2: deduct 1 points from quality score; priority 6-7.
- sla_breached >= 3: deduct 1.5 points from quality score; priority 7-8. Floor: score cannot drop below 4 from missed calls alone.
- sla_pending: mention as "pending SLA check — monitor" in evidence only; do not deduct score, do not raise priority.

MANDATORY check before writing any stakeholder row:
Step 1 — Look up missed_call_evidence_by_team[team_name].sla_breached.
Step 2 — If sla_breached = 0 or key absent: set missed-call contribution to score deduction = 0, priority uplift = 0. Do not write any missed-call gap for this stakeholder.
Step 3 — If sla_breached > 0: apply the deduction scale above to that stakeholder only.
Step 4 — Final score = ticket baseline ± ticket adjustments + missed-call deduction (from step 2 or 3 only).
Never carry missed-call gaps from one stakeholder's row into another stakeholder's row.

Ops subteam scoring:
- Ops has three current dashboard subteams: Desk, Field, and Asset.
- Desk score and Desk priority must be based only on support.ops_subteam_ticket_groups.Desk (tickets only).
- Field score and Field priority must be based only on support.ops_subteam_ticket_groups.Field (tickets only).
- Asset score and Asset priority must be based only on support.ops_subteam_ticket_groups.Asset (tickets only).
- If Desk has no listed tickets at all, Desk score must be 10/10 and Desk priority must be 1-3.
- If Field has no listed tickets at all, Field score must be 10/10 and Field priority must be 1-3.
- If Asset has no listed tickets at all, Asset score must be 10/10 and Asset priority must be 1-3.

Concrete scoring example (use this as a reference for correct isolation):
Suppose missed_call_evidence_by_team = { "Caretaker": { sla_breached: 5 }, "Ops": { sla_breached: 0, sla_note: "all_recovered_on_time" }, "Sales": { sla_breached: 0, sla_note: "all_recovered_on_time" } }
And support has 2 closed Field tickets (clean closure), no open tickets.
Correct output:
  Ops: Score 9/10, Priority 1/10, Gaps "None", Evidence "2 closed Field tickets, clean closure; Ops missed call recovered on time"
  Sales: Score 10/10, Priority 1/10, Gaps "None", Evidence "No tickets; missed call recovered on time"
  Caretaker: Score 5/10 (10 baseline − 3 for sla_breached≥3, floor 4), Priority 7/10, Gaps "5 missed calls SLA breached", Evidence "sla_breached: 5"
WRONG output (do not do this):
  Ops: Score 8/10, Priority 6/10, Gaps "Missed calls affecting customer experience" ← WRONG: Ops sla_breached=0, no deduction allowed
  Field: Score 10/10, Priority 6/10, Gaps "Communication gaps due to missed calls" ← WRONG: Field is ticket-only, missed calls forbidden

Return exactly:

1. Overall verdict:
Score: X/10
Priority score: X/10
Customer perspective score: X/10
Main reason: 1-2 lines.

2. Booking health:
- State:
- Stay dates:
- Onboarding/check-in/check-out risk:
- Support adequacy:

3. Customer perspective:
- What customer experienced:
- Response clarity:
- Friction/risk:

4. Stakeholder scorecard:
| Stakeholder/team | Score /10 | Priority score /10 | What they handled | Gaps | Evidence |
|---|---:|---:|---|---|---|

Stakeholder rules:
- Main stakeholder rows must use exact names only: Ops, Sales, Finance, Caretaker.
- Do not output Desk as a main stakeholder row.
- Always output exactly four main stakeholder rows in this order: Ops, Sales, Finance, Caretaker.
- If a stakeholder is visible in booking_scope, conversation, or support.ticket_groups, rate them from the available evidence.
- If visible but no issue is pending for them, Score should normally be high and Priority score must be 1-3.
- Stakeholder priority is current urgency, not quality.
- Score and Priority for each stakeholder must be derived STRICTLY using only the data sources defined in "Per-stakeholder scoring data sources" above:
  - Ops score = Operations ticket quality (Desk + Field + Asset) + missed_call_evidence_by_team.Ops (absent or sla_breached=0 → no deduction, no priority uplift, no gap mention).
  - Sales score = missed_call_evidence_by_team.Sales only — no ticket baseline (Sales resolves no tickets).
  - Finance score = Finance ticket quality + missed_call_evidence_by_team.Finance (absent or sla_breached=0 → no deduction, no priority uplift, no gap mention).
  - Caretaker score = Caretaker ticket quality + missed_call_evidence_by_team.Caretaker (absent or sla_breached=0 → no deduction, no priority uplift, no gap mention).
- NEVER reduce Ops score or raise Ops priority because of Caretaker or Sales missed calls.
- NEVER reduce Ops score or raise Ops priority because of missed calls visible in the conversation timeline that are not attributed to Ops in missed_call_evidence_by_team.
- NEVER reduce Field, Desk, or Asset scores or raise their priority for missed calls under any circumstances.
- If a stakeholder's sla_breached = 0, their Gaps column MUST NOT mention missed calls. Their Evidence column should note "recovered on time" or "no missed calls".

4A. Ops subteam scorecard:
| Ops subteam | Score /10 | Priority score /10 | What they handled | Gaps | Evidence |
|---|---:|---:|---|---|---|
| Desk | X/10 | X/10 | ... | ... | ... |
| Field | X/10 | X/10 | ... | ... | ... |
| Asset | X/10 | X/10 | ... | ... | ... |

Ops subteam rules:
- Always output exactly three rows: Desk, Field, and Asset.
- Desk row must use only support.ops_subteam_ticket_groups.Desk (tickets only — missed calls are NOT part of Desk scoring).
- Field row must use only support.ops_subteam_ticket_groups.Field (tickets only — missed calls are NOT part of Field scoring).
- Asset row must use only support.ops_subteam_ticket_groups.Asset (tickets only — missed calls are NOT part of Asset scoring).
- Use open ticket count, closed ticket count, status, category, priority, age, and evidence from the relevant group.
- Do not use general calls, WhatsApp, emails, welcome messages, agreement messages, extension emails, visitor approval messages, or automation as the base for Desk/Field/Asset score.
- If the relevant group has no listed tickets at all, score must be 10/10 and priority must be 1-3.
- If the relevant group has closed tickets only and no open tickets, score should be high if closure looks clean, and priority must be 1-3.
- If the relevant group has open tickets, priority should reflect the urgency of those open tickets.
- Exact score should be based on listed ticket severity, count, age, closure quality, and customer impact visible in the relevant ticket group. Do not use a fixed formula like one open ticket equals a fixed score.

5. Support handling:
- Open tickets:
- Closed tickets:
- Unresolved issues:
- SLA/ownership gaps:
- Closure quality:

6. Communication handling:
- Key touches:
- Missed calls/delays:
- Follow-ups/handoffs:
- Automation noise:
- Right team/role:

7. Score reasons:
Give 3-5 concise bullets.

8. Immediate action needed today:
| Priority score /10 | Owner/team | Action | Evidence |
|---:|---|---|---|

Action table rules:
- Only evidence-backed actions.
- For open tickets, Owner/team must follow the normalized support.ticket_groups. Do not assign a ticket to a different team/subteam than the normalized ticket group.
- If no immediate action is needed, return exactly:
| 2/10 | No immediate action needed based on provided evidence. | No visible complaint, open ticket, missed-call gap, unclear handoff, or unresolved issue. |
- Any row saying no immediate action / monitoring only / informational / no current issue must have priority 1-3.
- Owner/team must be: Ops, Caretaker, Sales, Finance, or combined owner like Sales / Ops / Caretaker.

**Consistency rule:**  
The overall `Priority score` in section 1 must equal the **highest** `Priority score` among actions in the “Immediate action needed today” table (section 8). If the table contains only the “No immediate action needed” row (priority 2/10), then the overall `Priority score` must be 1‑3. Do not output a high overall priority when no action is needed.

9. Next-best actions:
- If action is needed, give concise operational next steps.
- If no action is needed, write: No operational action needed now; continue normal monitoring.

10. Customer follow-up:
Write a short customer message only if follow-up is needed.
If not needed, write exactly:
No customer follow-up needed based on provided evidence.

Formatting rules:
- Do not rename section titles.
- Do not rename these labels: Score, Priority score, Customer perspective score, Main reason.
- Do not rename table columns.
- Keep evidence short and specific.
- Use "not visible" for missing evidence.
""".strip()


def build_booking_handling_prompt(llm_context: Any) -> str:
    return BOOKING_REVIEW_PROMPT_TEMPLATE