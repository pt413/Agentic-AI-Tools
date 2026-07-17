from __future__ import annotations

import re
from typing import Any, Sequence

from .common import compact_dict, score_number


def _clean_cell(value: Any) -> str:
    text = re.sub(r"<br\s*/?>", " ", str(value or ""), flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("` ")


def _priority_number(value: Any) -> int | None:
    parsed = score_number(value)
    if parsed is None:
        return None
    try:
        score = int(round(float(parsed)))
    except Exception:
        return None
    return max(1, min(score, 10))


def _table_header_key(cell: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(cell or "").strip().lower())).strip("_")


def _is_markdown_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", str(cell or "").replace(" ", "")) for cell in cells)


def parse_markdown_table_by_header(review_text: str, required_terms: Sequence[str]) -> list[dict[str, str]]:
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]
    required = [term.lower() for term in required_terms]
    for idx, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        lowered = line.lower()
        if not all(term in lowered for term in required):
            continue
        headers = [_table_header_key(cell) for cell in line.strip().strip("|").split("|")]
        rows: list[dict[str, str]] = []
        for raw in lines[idx + 1:]:
            if not raw.strip():
                if rows:
                    break
                continue
            if not raw.lstrip().startswith("|"):
                if rows:
                    break
                continue
            cells = [_clean_cell(cell) for cell in raw.strip().strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            if _is_markdown_separator(cells):
                continue
            rows.append({headers[i]: cells[i] for i in range(min(len(headers), len(cells)))})
        return rows
    return []


def parse_stakeholder_scores(review_text: str) -> list[dict[str, Any]]:
    rows = parse_markdown_table_by_header(review_text, ("stakeholder", "score", "evidence"))
    out: list[dict[str, Any]] = []
    for row in rows:
        stakeholder_key = next((k for k in row if "stakeholder" in k or "team" in k), "")
        score_key = next((k for k in row if k == "score_10" or k.startswith("score") or "score" in k), "")
        priority_key = next((k for k in row if "priority" in k), "")
        handled_key = next((k for k in row if "handled" in k or "what_they" in k), "")
        gaps_key = next((k for k in row if "gap" in k), "")
        evidence_key = next((k for k in row if "evidence" in k), "")
        out.append(
            compact_dict(
                {
                    "stakeholder_team": row.get(stakeholder_key),
                    "score": score_number(row.get(score_key)),
                    "priority_score": _priority_number(row.get(priority_key)),
                    "handled": row.get(handled_key),
                    "gaps": row.get(gaps_key),
                    "evidence": row.get(evidence_key),
                }
            )
        )
    return out

def parse_ops_subteam_scores(review_text: str) -> list[dict[str, Any]]:
    rows = parse_markdown_table_by_header(review_text, ("ops subteam", "score", "evidence"))
    out: list[dict[str, Any]] = []

    for row in rows:
        subteam_key = next((k for k in row if "ops_subteam" in k or "subteam" in k), "")
        score_key = next((k for k in row if k == "score_10" or k.startswith("score") or "score" in k), "")
        priority_key = next((k for k in row if "priority" in k), "")
        handled_key = next((k for k in row if "handled" in k or "what_they" in k), "")
        gaps_key = next((k for k in row if "gap" in k), "")
        evidence_key = next((k for k in row if "evidence" in k), "")

        out.append(
            compact_dict(
                {
                    "subteam": row.get(subteam_key),
                    "score": score_number(row.get(score_key)),
                    "priority_score": _priority_number(row.get(priority_key)),
                    "handled": row.get(handled_key),
                    "gaps": row.get(gaps_key),
                    "evidence": row.get(evidence_key),
                }
            )
        )

    return out

def parse_markdown_action_table(review_text: str) -> list[dict[str, Any]]:
    rows = parse_markdown_table_by_header(review_text, ("priority", "owner", "action"))
    out: list[dict[str, Any]] = []
    for row in rows:
        priority_key = next((k for k in row if "priority" in k), "")
        owner_key = next((k for k in row if "owner" in k or "team" in k), "")
        action_key = next((k for k in row if "action" in k), "")
        evidence_key = next((k for k in row if "evidence" in k), "")
        # Skip accidental stakeholder rows.
        if "stakeholder" in owner_key and "owner" not in owner_key:
            continue
        out.append(
            compact_dict(
                {
                    "priority_score": _priority_number(row.get(priority_key)),
                    "owner_team": row.get(owner_key),
                    "action": row.get(action_key),
                    "evidence": row.get(evidence_key),
                }
            )
        )
    return out


def _score_after_label(text: str, label: str) -> int | float | None:
    pattern = rf"\b{re.escape(label)}\s*:\s*(10|[0-9](?:\.\d+)?)\s*/\s*10"
    match = re.search(pattern, text, flags=re.I)
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def _risk_after_label(text: str, label: str) -> str | None:
    pattern = rf"\b{re.escape(label)}\s*:\s*(Low|Medium|High|Not visible)\b"
    match = re.search(pattern, text, flags=re.I)
    if not match:
        return None
    raw = match.group(1)
    return "Not visible" if raw.lower() == "not visible" else raw.title()


def fallback_priority_score(*, risk: Any = None, overall_score: Any = None, action_rows: list[dict[str, Any]] | None = None) -> int | None:
    values = []
    for row in action_rows or []:
        score = row.get("priority_score") if isinstance(row, dict) else None
        try:
            if score is not None:
                values.append(int(round(float(score))))
        except Exception:
            pass
    if values:
        return max(1, min(10, max(values)))
    risk_text = str(risk or "").lower()
    if risk_text == "high":
        return 8
    if risk_text == "medium":
        return 6
    if risk_text == "low":
        return 3
    try:
        score = float(overall_score)
        if score <= 4:
            return 8
        if score <= 6:
            return 6
        if score <= 8:
            return 4
        return 2
    except Exception:
        return None


def parse_overall(review_text: str, *, action_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    text = str(review_text or "")
    score = _score_after_label(text, "Score")
    priority_score = _score_after_label(text, "Priority score")
    customer_score = _score_after_label(text, "Customer perspective score")
    operations_score = _score_after_label(text, "Operations score")
    support_score = _score_after_label(text, "Support score")
    risk = _risk_after_label(text, "Risk")
    onboarding_risk = _risk_after_label(text, "Onboarding risk")
    support_risk = _risk_after_label(text, "Support risk")
    reason_match = re.search(r"\bMain reason\s*:\s*(.+)", text, flags=re.I)
    main_reason = reason_match.group(1).strip() if reason_match else None
    if priority_score is None:
        priority_score = fallback_priority_score(risk=risk, overall_score=score, action_rows=action_rows)
    return compact_dict(
        {
            "overall_score": score,
            "overall_priority_score": priority_score,
            "customer_perspective_score": customer_score,
            "operations_score": operations_score,
            "support_score": support_score,
            "overall_risk": risk,
            "onboarding_risk": onboarding_risk,
            "support_risk": support_risk,
            "main_reason": main_reason,
        }
    )


def extract_customer_followup(review_text: str) -> str | None:
    text = str(review_text or "")
    match = re.search(r"12\.\s*Customer follow-up\s*:\s*(.+)$", text, flags=re.I | re.S)
    if not match:
        match = re.search(r"Customer follow-up\s*:\s*(.+)$", text, flags=re.I | re.S)
    if not match:
        return None
    value = match.group(1).strip()
    value = re.split(r"\n\s*(?:13\.|#|\*\*)\s+", value, maxsplit=1)[0].strip()
    return value or None


def parse_booking_review_text(review_text: str) -> dict[str, Any]:
    actions = parse_markdown_action_table(review_text)
    stakeholder_scores = parse_stakeholder_scores(review_text)
    ops_subteam_scores = parse_ops_subteam_scores(review_text)
    overall = parse_overall(review_text, action_rows=actions)
    if ops_subteam_scores:
        ops_found = False
        for row in stakeholder_scores:
            team = str(row.get("stakeholder_team") or row.get("team") or "").strip().lower()
            if team == "ops":
                row["subteam_scores"] = ops_subteam_scores
                ops_found = True
                break

        if not ops_found:
            stakeholder_scores.append(
                compact_dict(
                    {
                        "stakeholder_team": "Ops",
                        "score": overall.get("operations_score"),
                        "priority_score": overall.get("overall_priority_score"),
                        "handled": "Overall operations ownership",
                        "subteam_scores": ops_subteam_scores,
                    }
                )
            )
    if review_text and not actions:
        actions = [
            {
                "priority_score": overall.get("overall_priority_score"),
                "owner_team": "Monitoring team",
                "action": "Review LLM output manually; action table could not be parsed.",
                "evidence": "LLM returned text but not the expected markdown action table.",
            }
        ]
    return compact_dict(
        {
            **overall,
            "action_rows": actions,
            "stakeholder_scores": stakeholder_scores,
            "customer_followup": extract_customer_followup(review_text),
        }
    )
