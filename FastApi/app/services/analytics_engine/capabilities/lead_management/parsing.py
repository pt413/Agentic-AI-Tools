from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .common import compact_dict

def _score_number(value: Any) -> Optional[int | float]:
    match = re.search(r"\b(10|[0-9](?:\.\d+)?)\s*/\s*10\b|\b(10|[0-9](?:\.\d+)?)\b", str(value or ""))
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        score = float(raw)
        return int(score) if score.is_integer() else score
    except Exception:
        return None


def parse_overall_rating(review_text: str) -> Dict[str, Any]:
    text_value = str(review_text or "")
    out: Dict[str, Any] = {}

    patterns = {
        "overall_score": r"overall(?:\s+verdict)?(?:\s+score)?\s*(?:[:\-;]|\s)\s*(10|[0-9](?:\.\d+)?)\s*/\s*10",
        "overall_priority_score": r"overall\s+priority(?:\s+score)?\s*(?:[:\-;]|\s)\s*(10|[0-9](?:\.\d+)?)\s*/\s*10",
        "customer_perspective_score": r"customer[-\s]*perspective\s+score\s*(?:[:\-;]|\s)\s*(10|[0-9](?:\.\d+)?)\s*/\s*10",
        "lead_handling_score": r"lead[-\s]*handling\s+score\s*(?:[:\-;]|\s)\s*(10|[0-9](?:\.\d+)?)\s*/\s*10",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text_value, flags=re.I)
        if match:
            out[key] = _score_number(match.group(1))

    if "overall_score" not in out:
        match = re.search(r"1\.\s*Overall verdict.*?(10|[0-9](?:\.\d+)?)\s*/\s*10", text_value, flags=re.I | re.S)
        if match:
            out["overall_score"] = _score_number(match.group(1))

    risk_patterns = {
        "post_booking_risk": r"post[-\s]*booking\s+risk\s*(?:[:\-;]|\s)\s*(Low|Medium|High)",
        "overall_risk": r"overall\s+risk\s*(?:[:\-;]|\s)\s*(Low|Medium|High)",
    }
    for key, pattern in risk_patterns.items():
        match = re.search(pattern, text_value, flags=re.I)
        if match:
            out[key] = match.group(1).title()

    if "overall_risk" not in out:
        # Fallback for simpler variants like "Risk: Medium". Avoid matching the
        # "post-booking risk" phrase by anchoring to a line/section boundary.
        match = re.search(r"(?:^|\n|;|\.)\s*risk\s*(?:[:\-;]|\s)\s*(Low|Medium|High)", text_value, flags=re.I)
        if match:
            out["overall_risk"] = match.group(1).title()

    reason_match = re.search(r"(?:one[-\s]*line reason|main reason|reason)\s*[:\-]\s*(.+)", text_value, flags=re.I)
    if reason_match:
        out["main_reason"] = reason_match.group(1).strip().strip("*")[:500]

    # Backward-compatible aliases for existing API callers.
    if out.get("overall_score") is not None:
        out["score"] = out.get("overall_score")
    if out.get("overall_risk"):
        out["risk"] = out.get("overall_risk")

    return compact_dict(out)


def _clean_table_cell(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", str(value or ""), flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip("` ")


def _is_markdown_separator_cell(value: str) -> bool:
    return bool(re.fullmatch(r":?-{3,}:?", str(value or "").replace(" ", "")))


def _header_key_map(headers: List[str]) -> Dict[str, int]:
    return {str(header or "").strip().lower(): idx for idx, header in enumerate(headers)}


def _cell_by_header(cells: List[str], header_map: Dict[str, int], *candidates: str) -> Optional[str]:
    normalized_candidates = [candidate.strip().lower() for candidate in candidates]
    for candidate in normalized_candidates:
        if candidate in header_map and header_map[candidate] < len(cells):
            return cells[header_map[candidate]]
    for header, idx in header_map.items():
        if idx >= len(cells):
            continue
        if any(candidate in header for candidate in normalized_candidates):
            return cells[idx]
    return None


def parse_markdown_action_table(review_text: str) -> List[Dict[str, Any]]:
    """Extract Priority score /10 | Owner/team | Action | Evidence rows."""
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]
    rows: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        normalized = line.lower()
        if "priority" not in normalized or "owner/team" not in normalized or "action" not in normalized:
            continue

        headers = [_clean_table_cell(cell) for cell in line.strip().strip("|").split("|")]
        header_map = _header_key_map(headers)
        for raw in lines[idx + 1:]:
            if not raw.strip():
                if rows:
                    break
                continue
            if not raw.lstrip().startswith("|"):
                if rows:
                    break
                continue
            cells = [_clean_table_cell(cell) for cell in raw.strip().strip("|").split("|")]
            if len(cells) < 4:
                continue
            if cells and _is_markdown_separator_cell(cells[0]):
                continue
            if "priority" in cells[0].lower():
                continue
            rows.append(compact_dict({
                "priority_score": _score_number(_cell_by_header(cells, header_map, "priority score /10", "priority score", "priority") or cells[0]),
                "owner_team": _cell_by_header(cells, header_map, "owner/team", "owner", "team") or (cells[1] if len(cells) > 1 else None),
                "action": _cell_by_header(cells, header_map, "action") or (cells[2] if len(cells) > 2 else None),
                "evidence": _cell_by_header(cells, header_map, "evidence") or (cells[3] if len(cells) > 3 else None),
            }))
        if rows:
            break

    return rows


def parse_stakeholder_scores(review_text: str) -> List[Dict[str, Any]]:
    """Extract stakeholder/team score and priority rows from markdown tables."""
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]
    rows: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        normalized = line.lower()
        if "stakeholder" not in normalized or "score" not in normalized or "evidence" not in normalized:
            continue

        headers = [_clean_table_cell(cell) for cell in line.strip().strip("|").split("|")]
        header_map = _header_key_map(headers)
        for raw in lines[idx + 1:]:
            if not raw.strip():
                if rows:
                    break
                continue
            if not raw.lstrip().startswith("|"):
                if rows:
                    break
                continue
            cells = [_clean_table_cell(cell) for cell in raw.strip().strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            if cells and _is_markdown_separator_cell(cells[0]):
                continue

            stakeholder = _cell_by_header(cells, header_map, "stakeholder/team", "stakeholder", "team")
            score = _cell_by_header(cells, header_map, "score /10", "score")
            priority = _cell_by_header(cells, header_map, "priority score /10", "priority score", "priority")
            rows.append(compact_dict({
                "stakeholder_team": stakeholder,
                "score": _score_number(score),
                "priority_score": _score_number(priority),
                "phase": _cell_by_header(cells, header_map, "phase judged", "phase"),
                "handled": _cell_by_header(cells, header_map, "what they handled", "handled"),
                "gaps": _cell_by_header(cells, header_map, "gaps", "gap"),
                "evidence": _cell_by_header(cells, header_map, "evidence"),
            }))
        if rows:
            break

    return rows


def parse_actor_scores(review_text: str) -> List[Dict[str, Any]]:
    """Extract individual actor/entity score and priority rows from markdown tables."""
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]
    rows: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        normalized = line.lower()
        if not (("actor" in normalized or "entity" in normalized) and "score" in normalized and "evidence" in normalized):
            continue

        headers = [_clean_table_cell(cell) for cell in line.strip().strip("|").split("|")]
        header_map = _header_key_map(headers)
        for raw in lines[idx + 1:]:
            if not raw.strip():
                if rows:
                    break
                continue
            if not raw.lstrip().startswith("|"):
                if rows:
                    break
                continue
            cells = [_clean_table_cell(cell) for cell in raw.strip().strip("|").split("|")]
            if len(cells) < min(4, len(headers)):
                continue
            if cells and _is_markdown_separator_cell(cells[0]):
                continue

            actor = _cell_by_header(cells, header_map, "actor/entity", "actor", "entity")
            role_team = _cell_by_header(cells, header_map, "role/team", "role", "team")
            score = _cell_by_header(cells, header_map, "score /10", "score")
            priority = _cell_by_header(cells, header_map, "priority score /10", "priority score", "priority")
            action = _cell_by_header(cells, header_map, "action")
            evidence = _cell_by_header(cells, header_map, "evidence")

            # Tolerate the old 4-column format: Actor | Role | Score | Evidence.
            if evidence is None and len(cells) >= 4:
                evidence = cells[-1]
            rows.append(compact_dict({
                "actor_entity": actor or (cells[0] if cells else None),
                "role_team": role_team or (cells[1] if len(cells) > 1 else None),
                "score": _score_number(score or (cells[2] if len(cells) > 2 else None)),
                "priority_score": _score_number(priority),
                "action": action,
                "evidence": evidence,
            }))
        if rows:
            break

    return rows


def _max_priority_score(*groups: Any) -> Optional[int | float]:
    values: List[float] = []
    for group in groups:
        if isinstance(group, dict):
            group = [group]
        for row in group or []:
            if not isinstance(row, dict):
                continue
            score = _score_number(row.get("priority_score") or row.get("priority"))
            if score is None:
                continue
            try:
                values.append(float(score))
            except Exception:
                pass
    if not values:
        return None
    max_value = max(values)
    return int(max_value) if max_value.is_integer() else max_value


def _fallback_priority_score(*, overall_risk: Any = None, overall_score: Any = None) -> Optional[int]:
    risk_text = str(overall_risk or "").strip().lower()
    if risk_text == "high":
        return 8
    if risk_text == "medium":
        return 6
    if risk_text == "low":
        return 3
    score = _score_number(overall_score)
    if score is None:
        return None
    try:
        numeric = float(score)
    except Exception:
        return None
    if numeric <= 4:
        return 8
    if numeric <= 6:
        return 6
    if numeric <= 8:
        return 4
    return 2


def extract_customer_followup(review_text: str) -> Optional[str]:
    match = re.search(r"13\.\s*Customer follow-up\s*:?\s*(.+?)(?:\n\s*14\.|\Z)", str(review_text or ""), flags=re.I | re.S)
    if not match:
        return None
    text_value = re.sub(r"\s+", " ", match.group(1)).strip()
    return text_value[:1000] if text_value else None


