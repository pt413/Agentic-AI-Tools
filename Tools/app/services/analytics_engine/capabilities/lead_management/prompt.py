from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from .cleaning import clean_counts_for_mode, clean_events_for_mode
from .common import (
    DEFAULT_SCHEMA,
    LEAD_REVIEW_CONTEXT_VERSION,
    clean_text,
    compact_dict,
    fmt_dt,
    get_session,
    resolve_window,
    StaffRoleResolver,
)
from .evidence import (
    _append_unique_id,
    enrich_lead_contacts,
    fetch_booking_confirm_rows,
    fetch_call_rows,
    fetch_email_rows,
    fetch_lead_row,
    fetch_site_visit_rows,
    fetch_travel_cart_rows,
    fetch_whatsapp_rows,
    normalized_id_list,
)
from .summary import build_lead_effectiveness_summary, event_counts

def _lead_payload_for_llm(contacts: Dict[str, Any], lead_row: Dict[str, Any]) -> Dict[str, Any]:
    booking_ids = normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))
    return compact_dict({
        "booking_ids": booking_ids,
        "owner": contacts.get("executive_id"),
        "phones": contacts.get("phones"),
        "emails": contacts.get("emails"),
        "status": lead_row.get("raw_status"),
        "source": lead_row.get("origin"),
        "created_at": lead_row.get("created_at"),
        "closed_at": lead_row.get("closed_at"),
    })


def clean_lead_for_mode(lead: Dict[str, Any], mode: str = "raw") -> Dict[str, Any]:
    cleaned = compact_dict(dict(lead or {}))
    if mode in {"llm", "evidence", "public"}:
        for key in ("user_id", "user_ids", "person_id", "booking_id", "raw_status", "origin", "executive_id"):
            cleaned.pop(key, None)
    return cleaned


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    start_dt, end_dt, window_label = resolve_window(args.days)
    with get_session(args.database_url) as db:
        lead_row = fetch_lead_row(db, args.schema, args.lead_id)
        contacts = enrich_lead_contacts(db, args.schema, args.lead_id, lead_row)
        role_resolver = StaffRoleResolver(db, args.schema)
        events: List[Dict[str, Any]] = []
        events.extend(fetch_whatsapp_rows(db, args.schema, args.lead_id, contacts, start_dt, end_dt, args.limit, args.max_text, role_resolver))
        events.extend(fetch_call_rows(db, args.schema, args.lead_id, start_dt, end_dt, args.limit, args.max_text, role_resolver))
        events.extend(fetch_email_rows(db, args.schema, contacts.get("emails"), start_dt, end_dt, args.limit, args.max_text, role_resolver))
        events.extend(fetch_site_visit_rows(db, args.schema, args.lead_id, start_dt, end_dt, args.limit, role_resolver))
        booking_events = fetch_booking_confirm_rows(db, args.schema, args.lead_id, contacts, start_dt, end_dt, args.limit, args.max_text)
        events.extend(booking_events)
        if booking_events:
            booking_ids = normalized_id_list(contacts.get("booking_ids") or contacts.get("booking_id"))
            for booking_event in booking_events:
                _append_unique_id(booking_ids, booking_event.get("booking_id"))
            if booking_ids:
                contacts["booking_ids"] = booking_ids
                contacts["booking_id"] = booking_ids[0]
        events.extend(fetch_travel_cart_rows(db, args.schema, contacts.get("user_ids") or contacts.get("user_id"), start_dt, end_dt, args.limit, args.max_text))

    if args.hide_automation:
        events = [row for row in events if not str(row.get("category") or "").startswith("auto_")]
    events.sort(key=lambda row: (fmt_dt(row.get("time")), str(row.get("channel")), str(row.get("source_id"))))

    counts = event_counts(events)
    lead_payload = _lead_payload_for_llm(contacts, lead_row)
    return {
        "context_version": LEAD_REVIEW_CONTEXT_VERSION,
        "input": {
            "lead_id": args.lead_id,
            "window": window_label,
            "from": start_dt.isoformat(sep=" "),
            "to": end_dt.isoformat(sep=" "),
            "schema": args.schema,
        },
        "lead": lead_payload,
        "summary": build_lead_effectiveness_summary(lead_payload, events, counts),
        "counts": counts,
        "events": clean_events_for_mode(events, mode="raw"),
    }
def build_lead_handling_prompt(lead_data: Dict[str, Any]) -> str:
    return (
        "Act as a RentMyStay lead handling quality reviewer.\n\n"

        "Your job is to review the provided lead communication data and produce:\n"
        "1. Lead handling quality review\n"
        "2. Customer experience review\n"
        "3. Stakeholder/team review\n"
        "4. Separate lead priority score for manager/team attention\n\n"

        "Use only LEAD COMMUNICATION DATA.\n"
        "Do not invent facts.\n"
        "If something is missing, write 'not visible in provided data'.\n\n"

        "INPUT FORMAT:\n"
        "The payload is raw-direction-only / raw-direction-trace.\n\n"
        "Main fields:\n"
        "- context = lead id, window, schema, timezone\n"
        "- lead = owner, phones, emails, status, source, created_at, conversion_time\n"
        "- scope = score phase, conversion visibility, handoff window, working hours, missing context\n"
        "- metrics = aggregate counts\n"
        "- stakeholders = visible teams and actors\n"
        "- timeline[] = raw event details plus normalized direction\n"
        "- data_limits = known limitations\n\n"

        "IMPORTANT EVIDENCE RULE:\n"
        "Use timeline[] as the main evidence source.\n"
        "Each timeline event contains mostly raw source fields plus a normalized direction field.\n\n"

        "Do not treat these raw fields as final scoring truth:\n"
        "- priority\n"
        "- needs_review\n"
        "- counts_against_sales_score\n"
        "- score_impact\n"
        "- scoring_note\n\n"

        "They are source metadata only. You must independently judge quality and priority from the visible evidence.\n\n"

        "For scoring and priority, use:\n"
        "- event_type\n"
        "- channel\n"
        "- flow\n"
        "- direction\n"
        "- status\n"
        "- duration_sec\n"
        "- business_hours / within_business_hours\n"
        "- after_hours\n"
        "- transcript/text\n"
        "- actor\n"
        "- actor_role\n"
        "- timeline sequence\n"
        "- metrics only as summary support\n\n"

        "STRUCTURE INTERPRETATION RULES:\n"
        "The payload may use context_version like review_lead_communication:v11_raw_direction_trace.\n"
        "In this format, timeline[] is the source of truth for event-by-event judgment.\n"
        "metrics and stakeholders are summary helpers only; do not score from counts alone.\n\n"

        "Lead.owner is the assigned lead owner, but timeline actor/actor_role shows who actually handled each visible event.\n"
        "If caretaker, sales, support, ops, or other actors appear in timeline, judge each only for their visible actions.\n\n"

        "stakeholders.*.events only means visible event count. It does not automatically mean good or bad performance.\n"
        "A team with many events may still have weak handling if there is no clear resolution.\n"
        "A team with few events may still be acceptable if their role was limited or not enough evidence is visible.\n\n"

        "BUSINESS CALIBRATION:\n"
        "RentMyStay is an operational business with real-world constraints.\n"
        "Do not judge every lead against a perfect enterprise call-center standard.\n"
        "Minor delays, brief calls, automation messages, delegated handling, shared-line calls, or normal back-and-forth should not heavily reduce the score if the team eventually handled the customer properly.\n"
        "Judge based on practical business outcome, visible recovery, customer progress, and whether customer risk was reasonably managed.\n\n"

        "Do not over-penalize:\n"
        "- Small response delays if the customer was eventually handled.\n"
        "- Short connected calls if nearby messages/calls show the issue was understood or handled.\n"
        "- Automation messages if they are followed by human action when needed.\n"
        "- After-hours customer inbound calls/messages if the team recovered in the next working window.\n"
        "- Missing perfect documentation if the visible communication still shows reasonable handling.\n"
        "- Normal customer confusion if the team clarified it later.\n"
        "- Limited/truncated data unless the visible evidence clearly shows a handling failure.\n"
        "- Absence of a direct call/message from the assigned owner when another team member visibly handled the customer.\n\n"

        "Penalize clearly when:\n"
        "- Customer showed booking/payment/site-visit intent and there was no visible follow-up.\n"
        "- Customer repeatedly reached out and the team did not recover.\n"
        "- Business-hours inbound calls were missed and no callback/recovery is visible.\n"
        "- Team gave confusing, wrong, contradictory, or incomplete guidance.\n"
        "- Customer was left waiting for confirmation, payment, booking, refund, cancellation, or site-visit support.\n"
        "- There is visible risk of losing the lead due to weak follow-up.\n"
        "- Handoff/onboarding failure is clearly connected to poor sales communication or wrong expectation-setting.\n"
        "- Site visit happened but no clear post-visit closing/status follow-up is visible.\n\n"

        "OWNER / DELEGATION / SHARED-HANDLING RULES:\n"
        "Do not assume the assigned lead owner ignored the customer only because there is no direct call/message from that exact owner.\n"
        "In RentMyStay operations, the sales owner may coordinate through caretaker, ops, support, shared business numbers, or another staff member's phone.\n"
        "Caretaker/ops calls near a site visit or lead activity may be part of valid lead handling support.\n\n"

        "When judging the assigned sales owner:\n"
        "- Give partial credit if the customer was visibly handled by another relevant team member and journey progress is visible.\n"
        "- Do not penalize the owner only for using another staff member, caretaker, or shared line.\n"
        "- Do not say the owner ignored the customer unless customer need remained unresolved and no team recovery is visible.\n"
        "- If responsibility is unclear, call it a team/process visibility gap instead of blaming one person.\n"
        "- However, if a site visit happened and no clear post-visit closing/status follow-up is visible, keep the sales/owner score moderate, not high.\n\n"

        "When judging caretaker/ops calls:\n"
        "- Customer-to-caretaker or caretaker-to-customer calls can be valid lead handling support, especially around site visits.\n"
        "- Give caretaker/ops credit for visible connected calls and site-visit support.\n"
        "- Do not treat caretaker involvement as separate from lead handling if it clearly supports the customer journey.\n"
        "- Do not give very high scores unless outcome/resolution is visible through transcript, message, booking, travel cart, or explicit site-visit result.\n\n"

        "Shared-line / alternate-number rule:\n"
        "A business outbound or inbound call from another staff number may still represent company follow-up.\n"
        "Do not require the same assigned owner phone to appear in every follow-up.\n"
        "Judge whether the customer was handled by the company, not only whether the assigned owner personally called.\n\n"

        "Evidence caution:\n"
        "You may consider likely operational delegation, but do not invent hidden coordination.\n"
        "Use wording like 'possibly coordinated through caretaker' or 'handled by team, owner-specific involvement not directly visible' when evidence is indirect.\n\n"

        "PHASE RULES:\n"
        "- Pre-booking runs until visible conversion, lead.closed_at, booking confirmation, or conversion_time.\n"
        "- Use only pre-booking evidence for lead-handling score.\n"
        "- If conversion is 'not_visible' or conversion_time is missing, treat visible lead communication as pre-booking unless clearly post-booking.\n"
        "- Handoff/onboarding is 48h after conversion.\n"
        "- Post-booking events must not reduce lead-handling score unless they show poor handoff, wrong expectation setting, or unresolved pre-booking risk.\n\n"

        "WORKING HOURS:\n"
        "Working hours are 10:00 AM to 8:30 PM Asia/Kolkata.\n"
        "If raw event metadata says 10:00-20:00, apply 10:00-20:30 instead.\n"
        "Use scope.working_hours as the final working-hours policy when available.\n"
        "Do not penalize sales/team for customer inbound outside working hours unless there was no recovery in the next working window.\n"
        "Business-hours missed inbound calls/messages are important only when not recovered later.\n\n"

        "DIRECTION RULES:\n"
        "- direction = customer_inbound means customer initiated contact.\n"
        "- direction = business_outbound means staff/business initiated contact.\n"
        "- direction = customer_activity means non-message lead/customer progress activity.\n"
        "- direction = system_activity means system/business state event.\n\n"

        "CALL RULES:\n"
        "- flow shows business relationship, for example customer_to_caretaker, caretaker_to_customer, sales_to_customer.\n"
        "- direction shows raw normalized direction: customer_inbound, business_outbound, customer_activity, system_activity.\n"
        "- customer_inbound + connected = customer reached business and the call connected.\n"
        "- customer_inbound + missed during working hours = business missed customer; negative only if not recovered soon.\n"
        "- customer_inbound + missed outside working hours = context only; judge recovery in next working window.\n"
        "- business_outbound + missed = business attempted to call and customer did not answer; do not treat it as a missed customer request.\n"
        "- business_outbound + connected = successful outbound contact.\n"
        "- Connected calls under 30 seconds must not be treated as weak by duration alone.\n"
        "- If transcript is missing/null/'no transcript', do not infer call quality beyond flow, direction, status, duration, and sequence.\n\n"

        "IMPORTANT CALL GUARDRAIL:\n"
        "If flow = sales_to_customer and status = missed, it means sales attempted an outbound call and customer did not answer.\n"
        "Do not interpret it as customer_to_sales missed.\n\n"
        "The same applies to:\n"
        "- caretaker_to_customer\n"
        "- ops_to_customer\n"
        "- support_to_customer\n\n"

        "ACTIVITY RULES:\n"
        "- event_type = activity is supporting progress evidence only.\n"
        "- site_visit, travel_cart, booking attempt, and similar activity are not direct communication.\n"
        "- Use activity to support lead progress and next-step evidence.\n"
        "- Do not give communication-quality credit for activity unless matching call/message evidence exists.\n\n"

        "SITE VISIT / POST-VISIT FOLLOW-UP RULE:\n"
        "If a site_visit is visible, treat it as strong journey progress.\n"
        "A site_visit plus multiple connected caretaker calls usually means the customer was not ignored.\n"
        "However, site_visit alone does not prove conversion, closure, or satisfaction.\n"
        "If no booking/travel_cart/conversion/post-visit customer message is visible, mention that site-visit outcome is not visible.\n"
        "This should usually create a medium follow-up need, not a high/critical priority, unless there is a recent unresolved customer request.\n\n"

        "EMAIL / WHATSAPP RULES:\n"
        "- Use visible text only.\n"
        "- If text is truncated, say content is partial/truncated.\n"
        "- Duplicate emails/messages should be treated as data limits and must not inflate performance.\n"
        "- Automation/system messages are separate from human follow-up.\n"
        "- Automation alone is not enough when the customer needs a human answer.\n"
        "- But automation should not be treated as negative if the customer did not need further help or human recovery happened later.\n"
        "- Repeated feedback/template emails should not be treated as meaningful human sales follow-up.\n"
        "- A template email entity can receive a lower quality score even if many email events exist.\n"
        "- Duplicate/template email issues are usually low priority unless they confuse or block the customer.\n\n"

        "CONVERSION RULE:\n"
        "If conversion_time is null, booking_events is 0, and scope.conversion is not_visible, do not assume the lead was lost or converted.\n"
        "Say conversion is not visible in provided data.\n\n"

        "QUALITY SCORING RULES:\n"
        "- Overall score /10 = visible handling quality across relevant phases.\n"
        "- Lead-handling score /10 = pre-booking human/team handling quality only.\n"
        "- Customer-perspective score /10 = likely customer experience from visible handling.\n"
        "- Score only visible teams/actors.\n"
        "- If not enough evidence to score an actor, write 'not scorable /10'.\n\n"

        "QUALITY SCORE CALIBRATION:\n"
        "Use practical business scoring. Do not be overly strict, and do not require transcripts for every good score.\n"
        "Judge the full visible journey: response sequence, connected calls, missed/recovered calls, site visit/progress, follow-up pattern, and visible customer risk.\n\n"

        "Score range interpretation:\n"
        "10 = Exceptional. Only for near-perfect handling with transcript/text or explicit outcome evidence showing excellent customer handling, clear closure, and no avoidable risk.\n"
        "9 = Excellent. Only when transcript/text or explicit outcome evidence supports excellent handling, strong ownership, clear next step/closure, and very low customer risk.\n"
        "8-8.5 = Very good. Strong practical handling is visible from sequence/progress even if transcript is missing. Customer was handled, cadence was good, and no major unresolved risk is visible.\n"
        "7-7.5 = Good/acceptable. Mostly handled, with some visible gaps, missing transcript, unclear outcome, or incomplete documentation, but no major unresolved customer risk.\n"
        "6-6.5 = Fair. Mixed handling. Some useful follow-up exists, but ownership, recovery, or next step is unclear.\n"
        "5-5.5 = Below average. Customer intent/risk was visible but handling was inconsistent, delayed, or incomplete.\n"
        "4-4.5 = Poor. Major follow-up gaps, weak recovery, or customer left unclear.\n"
        "3-3.5 = Very poor. Repeated misses or serious unresolved customer risk.\n"
        "1-2.5 = Critical failure. Lead was effectively neglected or badly mishandled.\n\n"

        "Transcript-aware scoring rule:\n"
        "- Do not require transcript to give 7, 7.5, 8, or 8.5 when the event sequence shows strong handling.\n"
        "- Without transcript/text or explicit outcome, normally cap individual human actor scores at 8.5.\n"
        "- Give 9 or 10 only when transcript/text or explicit outcome evidence shows excellent handling, clear resolution/closure, or very strong customer reassurance.\n"
        "- If no transcript is available but there are multiple connected calls, no missed customer inbound gaps, and visible progress such as site visit/booking/travel cart, use a reasoning-based score up to 8.5.\n"
        "- Missing transcript should reduce confidence, not automatically reduce score harshly.\n\n"

        "Important quality scoring calibration:\n"
        "- Do not reserve 8 only for perfect handling. 8 can be given for strong practical business handling.\n"
        "- 9 can be given without transcript when the visible sequence is clearly strong and customer risk is controlled.\n"
        "- 9.5 and 10 are reserved for transcript/text-backed or explicit-outcome-backed excellent cases.\n"
        "- Do not give very low scores only because data is imperfect. Penalize only when visible evidence supports it.\n"
        "- If data is limited but no clear issue is visible, use a cautious middle-to-good score instead of harsh punishment.\n"
        "- If the team recovered well after a miss, mention the miss but reduce the penalty.\n\n"

        "SCORING GUIDANCE:\n"
        "Score precision:\n"
        "- Use whole numbers or .5 increments only, such as 9,9.5.\n"
        "- Do not use false precision like 8.1, 8.3, 7.8.\n\n"

        "Actor scoring guide:\n"
        "- 9.5-10: only when transcript/text or explicit outcome proves excellent handling, clear next step/closure, and very low customer risk.\n"
        "- 8-9: strong visible handling, multiple useful connected interactions, journey progress visible, no major unresolved risk, even if transcript is missing.\n"
        "- 7-8: good visible contribution, but limited evidence, missing outcome, or incomplete documentation.\n"
        "- 6-7: moderate contribution, indirect/delegated handling visible, but owner-specific follow-up or closure is unclear.\n"
        "- 4-6: weak or mostly template/process communication, limited human value, or unclear customer support.\n"
        "- 1-4: severe neglect, repeated unrecovered customer inbound misses, or harmful/confusing handling.\n\n"

        "Assigned owner scoring:\n"
        "- If team/caretaker handling is visible, do not score the assigned owner as if the lead was ignored.\n"
        "- If owner-specific direct engagement is limited but team progress is visible, a moderate score like 6-7 is often appropriate.\n"
        "- If owner-specific follow-up is visible and team progress is strong, owner score can be 7-8 even without transcript.\n"
        "- If post-site-visit closing/follow-up is not visible, do not give the assigned owner 9 or 10.\n\n"

        "Caretaker scoring:\n"
        "- Multiple connected caretaker calls around a site visit usually deserve good credit.\n"
        "- If outcome/transcript is missing but sequence is strong, caretaker score can be 8-9.\n"
        "- Give 9.5 or 10 only if transcript/text or explicit site-visit outcome shows excellent handling or clear closure.\n"
        "- A caretaker with only one short visible call should usually be around 6-7 unless more outcome evidence exists.\n\n"

        "Template/system email scoring:\n"
        "- Repeated duplicate/template emails should usually score low-to-moderate, around 4-5.5, unless they clearly helped the customer.\n"
        "- Template emails alone should not drive a high score, but they also should not heavily reduce score if human handling/progress is visible elsewhere.\n\n"
        "LEAD PRIORITY SCORE RULE:\n"
        "Also produce a separate LEAD PRIORITY SCORE.\n\n"

        "Priority score means urgency for manager/team attention right now.\n"
        "It is NOT the same as lead handling quality.\n"
        "Do not copy the handling score into priority.\n\n"

        "A badly handled old lead can still have low priority if there is no current action needed.\n"
        "A well-handled lead can still have high priority if the customer is waiting now.\n\n"

        "Priority score scale:\n"
        "10 = Critical. Immediate same-day escalation needed. Use only for severe unresolved risk.\n"
        "8-9 = High. Manager/team should review and act soon.\n"
        "6-7 = Medium. Follow-up needed, but not a crisis.\n"
        "4-5 = Low. Monitor only or normal follow-up.\n"
        "1-3 = Informational. No immediate action visible.\n\n"

        "Priority calibration:\n"
        "- Do not make every gap high priority.\n"
        "- Priority should increase only when there is current unresolved customer/business risk.\n"
        "- If the issue was recovered later, reduce priority even if earlier handling was imperfect.\n"
        "- If the evidence is unclear, prefer Medium or Low instead of High.\n"
        "- Old/stale weak handling should not become high priority unless there is recent actionable risk.\n\n"

        # "Actor priority scoring:\n"
        # "- Actor priority score /10 = urgency/action needed for that actor/team now, not how good or bad they performed.\n"
        # "- A high-quality actor can have low priority if no action is needed.\n"
        # "- A lower-quality actor can have medium priority if they need follow-up or process correction.\n"
        # "- Do not give high actor priority only because their quality score is low.\n\n"

        "Increase priority when the latest actionable events or unresolved sequence show:\n"
        "- Customer shows booking/payment/refund/cancellation/site-visit/check-in intent and no clear human recovery is visible.\n"
        "- Customer says payment is done and asks for confirmation, receipt, invoice, booking status, refund, or cancellation.\n"
        "- Customer inbound call during working hours is missed and no callback/recovery is visible. Number of misscalls does not increase priority\n"
        "- Customer sends repeated inbound messages without proper response.\n"
        "- Customer asks urgent location/site-visit/check-in related questions and no timely human response is visible.\n"
        "- Site visit or booking intent is visible but closing/follow-up is missing.\n"
        "- Customer appears blocked, frustrated, confused, or waiting for action from the team.\n"
        "- There is visible risk of losing the lead, payment, booking, or customer trust.\n\n"

        "Reduce priority when:\n"
        "- The issue is clearly resolved later.\n"
        "- Team gave a clear human reply and customer acknowledged positively.\n"
        "- Only automation/system messages are visible and no customer concern is pending.\n"
        "- Customer only says ok/thanks/acknowledgement and no pending issue is visible.\n"
        "- There is no current customer risk or action needed.\n"
        "- The lead is old/stale and no recent actionable customer request is visible.\n\n"

        "Priority examples:\n"
        "PRIORITY: 9/10 — High — Customer showed payment/booking intent and no clear human confirmation is visible after that.\n"
        "PRIORITY: 7/10 — Medium — Follow-up is needed, but there is partial recovery and no immediate critical risk.\n"
        "PRIORITY: 5/10 — Low — Some handling gaps are visible, but no current unresolved customer risk is clear.\n"
        "PRIORITY: 2/10 — Low — Only resolved or informational communication is visible, with no immediate action needed.\n\n"

        "EXPECTED STYLE FOR ACTOR REASONS:\n"
        "Prefer balanced, operationally realistic reasons.\n"
        "Good examples:\n"
        "- 'Owner-specific direct engagement is limited, but customer handling through caretaker/site-visit flow is visible. Sales should confirm site-visit outcome and next step.'\n"
        "- 'Multiple connected caretaker calls and site-visit support are visible, but outcome/transcript is missing, so score is capped around 8.'\n"
        "- 'Repeated template emails are visible, but they do not count as meaningful human follow-up.'\n\n"
        "Avoid unfair or over-certain reasons like:\n"
        "- 'Sales ignored the customer' unless no team recovery is visible.\n"
        "- 'Caretaker resolved the issue' unless resolution/outcome is visible.\n"
        "- 'Customer was not interested' unless customer explicitly said so.\n\n"

        "REQUIRED OUTPUT:\n"
        "Return exactly these sections:\n\n"

        "1. Overall verdict:\n"
        "overall score /10;\n"
        "overall priority score /10;\n"
        "customer-perspective score /10;\n"
        "lead-handling score /10;\n"
        "post-booking risk Low/Medium/High or not visible;\n"
        "overall risk Low/Medium/High;\n"
        "one-line reason.\n\n"

        "2. Lead journey by phase:\n"
        "- pre-booking\n"
        "- conversion evidence\n"
        "- handoff/onboarding\n"
        "- post-booking\n\n"

        "3. Customer perspective:\n"
        "response speed, clarity, reassurance, friction, sentiment/risk.\n\n"

        "4. Stakeholder scorecard:\n"
        "Markdown table with columns:\n"
        "Stakeholder/team | Score /10 | Priority score /10 | Phase judged | What they handled | Gaps | Evidence\n\n"

        "5. Response/follow-up:\n"
        "separate:\n"
        "- customer inbound missed calls\n"
        "- outbound missed attempts\n"
        "- connected calls with missing transcript\n"
        "- human follow-up\n"
        "- activity/progress events\n"
        "- cadence and next step\n\n"

        "6. Team performance:\n"
        "role/admin-wise connected vs missed attempts; useful vs incomplete; separate pre-booking, handoff/onboarding, and post-booking.\n\n"

        "7. Ownership/handoff gaps.\n\n"

        "8. Customer risk/sentiment:\n"
        "evidence-backed only.\n\n"

        "9. Data limits.\n\n"

        "10. What worked well.\n\n"

        "11. Process gaps.\n\n"

        "12. Immediate next best actions:\n"
        "Markdown table with columns:\n"
        "Priority score /10 | Owner/team | Action | Evidence\n\n"

        "13. Customer follow-up:\n"
        "short message, or 'No customer follow-up needed based on provided evidence.'\n\n"

        "14. Individual actor/entity scores:\n"
        "Markdown table with columns:\n"
        "Actor/entity | Role/team | Score /10 | Priority score /10 | Action | Evidence\n\n"

        "FINAL MACHINE-PARSABLE LINES:\n"
        "At the very end of the response, include these exact two lines and nothing after them:\n\n"
        "PRIORITY: X/10 — Low|Medium|High|Critical — one sentence reason\n"
        "RATING: X/10 — one sentence reason\n\n"

        "LEAD COMMUNICATION DATA:\n"
        + json.dumps(lead_data, default=str, ensure_ascii=False, indent=2)
    )

def render_llm(payload: Dict[str, Any], max_rows: int) -> str:
    brief = {
        "context": compact_dict(payload["input"]),
        "lead": clean_lead_for_mode(payload.get("lead") or {}, mode="llm"),
        "summary": payload.get("summary"),
        "counts": clean_counts_for_mode(payload.get("counts") or {}, mode="llm"),
        "events": clean_events_for_mode(payload.get("events") or [], mode="llm", limit=max_rows),
    }
    return build_lead_handling_prompt(brief)


def render_table(payload: Dict[str, Any], max_rows: int) -> str:
    lines: List[str] = []
    inp = payload["input"]
    lead = payload["lead"]
    counts = payload["counts"]
    events = payload["events"][:max_rows]
    lines.append("=" * 120)
    lines.append("LEAD COMMUNICATION REVIEW")
    lines.append("=" * 120)
    lines.append(
        f"lead_id={inp['lead_id']} | window={inp['window']} | events={counts.get('events', 0)} | "
        f"calls={counts.get('calls', 0)} | whatsapp={counts.get('whatsapp', 0)} | emails={counts.get('emails', 0)} | "
        f"bookings={counts.get('bookings', 0)} | site_visits={counts.get('site_visits', 0)} | "
        f"travel_cart={counts.get('travel_cart', 0)}"
    )
    lines.append(
        f"contacts phones={', '.join(lead.get('phones') or []) or '-'} "
        f"emails={', '.join(lead.get('emails') or []) or '-'} "
        f"booking_ids={', '.join(lead.get('booking_ids') or []) or '-'} "
        f"owner={lead.get('owner') or '-'} status={lead.get('status') or '-'} source={lead.get('source') or '-'}"
    )
    lines.append("-" * 120)
    if not events:
        lines.append("No matching lead communication found.")
        return "\n".join(lines)
    for row in events:
        actor = row.get("actor") or row.get("actor_name") or "-"
        role = row.get("actor_role") or "-"
        lines.append(
            f"[{fmt_dt(row.get('time'))}] {str(row.get('channel')).upper()} {row.get('flow')} "
            f"status={row.get('status') or '-'} role={role} actor={actor}"
        )
        if row.get("customer_number") or row.get("customer_email"):
            lines.append(f"  customer={row.get('customer_number') or row.get('customer_email') or '-'}")
        text_value = clean_text(row.get("text"), 600)
        if text_value:
            lines.append(f"  {text_value}")
        lines.append("")
    if len(payload["events"]) > max_rows:
        lines.append(f"... truncated: showing {max_rows} of {len(payload['events'])} events")
    return "\n".join(lines).rstrip()


