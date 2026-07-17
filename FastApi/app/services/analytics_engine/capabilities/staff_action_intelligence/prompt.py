from __future__ import annotations

import json
import re
from typing import Any


JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", flags=re.S | re.I)


def build_staff_action_intelligence_prompt(
    *,
    activity: dict[str, Any],
    signals: dict[str, Any],
    heuristic_review: dict[str, Any],
) -> str:
    staff = activity.get("staff") if isinstance(activity.get("staff"), dict) else {}
    role_scope = str(activity.get("role_scope") or "generic")
    role_display = str(activity.get("role_display") or role_scope)
    window = activity.get("window") if isinstance(activity.get("window"), dict) else {}
    llm_context = activity.get("llm_context") if isinstance(activity.get("llm_context"), dict) else {}

    payload = {
        "staff": {
            "username": staff.get("username"),
            "email": staff.get("email"),
            "phone": staff.get("phone"),
            "team": staff.get("team"),
            "role_scope": role_scope,
            "role_display": role_display,
        },
        "window": window,
        "deterministic_signals": signals,
        "heuristic_review": {
            "rating": heuristic_review.get("rating"),
            "business_impact": heuristic_review.get("business_impact"),
            "key_findings": heuristic_review.get("key_findings"),
            "recommended_actions": heuristic_review.get("recommended_actions"),
            "coaching_points": heuristic_review.get("coaching_points"),
            "data_gaps": heuristic_review.get("data_gaps"),
        },
        "activity_evidence": llm_context or {
            "staff": activity.get("staff"),
            "counts": activity.get("counts"),
            "timeline": activity.get("timeline"),
            "data": activity.get("data"),
        },
    }
    evidence_json = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    return f"""You are reviewing one staff member for Staff Action Intelligence.

Return valid JSON only. Do not write markdown. Do not wrap in code fences. Do not add commentary before or after JSON.

Use only the provided evidence.
Do not invent customers, leads, bookings, tickets, revenue, or staff actions.
Separate quality score from priority score:
- overall_score = staff handling quality
- priority_score = urgency for manager action
A good staff can still have high priority if one urgent customer or revenue issue exists.
If evidence is missing, say data unavailable. Do not assume no work happened.
Always produce at least one recommended action if risk is High or Medium.
Every recommended action must include owner, due_by, and evidence.
If no issue is found, action can be: "No immediate action; monitor next review window."

Output schema:
{{
  "staff": {{
    "username": "",
    "team": "",
    "role_scope": ""
  }},
  "rating": {{
    "overall_score": 0,
    "priority_score": 0,
    "risk": "High | Medium | Low | No Data",
    "reason": ""
  }},
  "business_impact": {{
    "customer_risk": "",
    "revenue_risk": "",
    "operation_risk": "",
    "trust_risk": ""
  }},
  "key_findings": [
    ""
  ],
  "recommended_actions": [
    {{
      "owner": "",
      "action": "",
      "priority": "High | Medium | Low",
      "due_by": "",
      "evidence": "",
      "status": "open"
    }}
  ],
  "coaching_points": [
    ""
  ],
  "data_gaps": [
    ""
  ]
}}

Evidence for this single staff review:
{evidence_json}
""".strip()


def build_staff_action_assistant_prompt(
    *,
    question: str,
    context_payload: dict[str, Any],
) -> str:
    context_json = json.dumps(context_payload, ensure_ascii=False, indent=2, default=str)
    return f"""You are a Staff Action Intelligence assistant.

You are answering one manager question about one specific staff review.

Rules:
- Use only the provided evidence.
- Do not invent calls, tickets, leads, payments, site visits, or customer outcomes.
- If the selected evidence does not support an answer, say that clearly.
- Prefer direct, operational answers: what caused the rating, what is the main problem, what evidence supports it, and what data is missing.
- If the question is about calls, focus on call evidence.
- If the question is about WhatsApp, focus on WhatsApp evidence.
- If the question is about tickets, finance, leads, bookings, or site visits, stay within that section.
- When the question is general, connect the answer back to rating, risk, key findings, and recommended actions.

Return valid JSON only with this exact shape:
{{
  "answer": "",
  "evidence": [
    {{
      "section": "",
      "detail": ""
    }}
  ],
  "used_sections": [
    ""
  ],
  "data_gaps": [
    ""
  ]
}}

Question:
{question}

Scoped staff review context:
{context_json}
""".strip()


def parse_staff_action_llm_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        raise ValueError("LLM response was empty.")

    text_value = str(value).strip()
    if not text_value:
        raise ValueError("LLM response was empty.")

    match = JSON_BLOCK_RE.search(text_value)
    if match:
        text_value = match.group(1).strip()
    elif text_value.startswith("```"):
        text_value = text_value.strip("`").strip()
        if text_value.lower().startswith("json"):
            text_value = text_value[4:].strip()

    try:
        parsed = json.loads(text_value)
    except Exception:
        start = text_value.find("{")
        end = text_value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM response did not contain a valid JSON object.")
        try:
            parsed = json.loads(text_value[start : end + 1])
        except Exception as exc:
            raise ValueError("LLM response did not contain parseable JSON.") from exc

    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object.")
    return parsed


def parse_staff_action_assistant_json(value: Any) -> dict[str, Any]:
    parsed = parse_staff_action_llm_json(value)
    out = {
        "answer": str(parsed.get("answer") or "").strip(),
        "evidence": parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else [],
        "used_sections": parsed.get("used_sections") if isinstance(parsed.get("used_sections"), list) else [],
        "data_gaps": parsed.get("data_gaps") if isinstance(parsed.get("data_gaps"), list) else [],
    }
    if not out["answer"]:
        raise ValueError("Assistant response JSON was missing 'answer'.")
    return out
