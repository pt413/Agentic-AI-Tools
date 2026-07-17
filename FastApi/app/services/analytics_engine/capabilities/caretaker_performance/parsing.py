from __future__ import annotations

import re
from typing import Any


def _strip_markdown(value: Any) -> str:
    text = str(value or "")
    # Remove markdown emphasis/backticks but preserve underscores because
    # evidence metric keys use names like:
    # not_done_missed_call_no_connected_followup
    text = re.sub(r"[*`]+", "", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _score_number(value: Any) -> int | float | None:
    text = _strip_markdown(value)

    match = re.search(
        r"(?<!\d)(10(?:\.0)?|[0-9](?:\.\d+)?)(?:\s*/\s*10)?(?!\d)",
        text,
        flags=re.I,
    )
    if not match:
        return None

    try:
        number = float(match.group(1))
        return int(number) if number.is_integer() else number
    except Exception:
        return None


def _line_after_label(text: str, label: str) -> str | None:
    clean_label = re.escape(label.strip())

    for raw_line in str(text or "").splitlines():
        line = _strip_markdown(raw_line)
        if not line:
            continue

        match = re.search(
            rf"^\s*(?:[-•]\s*)?{clean_label}\s*(?:[:：=\-–—])\s*(.+?)\s*$",
            line,
            flags=re.I,
        )
        if match:
            return match.group(1).strip()

    return None


def _score_after_label(text: str, label: str) -> int | float | None:
    value = _line_after_label(text, label)
    if value is not None:
        return _score_number(value)

    # Fallback for inline text where the label is not at the start of the line.
    clean_text = _strip_markdown(text)
    match = re.search(
        rf"\b{re.escape(label)}\s*(?:[:：=\-–—])\s*(10(?:\.0)?|[0-9](?:\.\d+)?)(?:\s*/\s*10)?\b",
        clean_text,
        flags=re.I,
    )
    if not match:
        return None

    return _score_number(match.group(1))


def _risk_after_label(text: str, label: str = "Risk") -> str | None:
    value = _line_after_label(text, label)

    if value is not None:
        match = re.search(r"\b(Low|Medium|High|No\s*Data|Needs\s*Review)\b", value, flags=re.I)
        if match:
            raw = re.sub(r"\s+", " ", match.group(1).strip())
            return raw.title()

    clean_text = _strip_markdown(text)
    match = re.search(
        rf"\b{re.escape(label)}\s*(?:[:：=\-–—])\s*(Low|Medium|High|No\s*Data|Needs\s*Review)\b",
        clean_text,
        flags=re.I,
    )
    if not match:
        return None

    raw = re.sub(r"\s+", " ", match.group(1).strip())
    return raw.title()


def _main_reason(text: str) -> str | None:
    value = _line_after_label(text, "Main reason")
    if value:
        return _strip_markdown(value)

    clean_text = _strip_markdown(text)
    match = re.search(r"\bMain reason\s*(?:[:：=\-–—])\s*(.+)", clean_text, flags=re.I)
    if match:
        return _strip_markdown(match.group(1))

    return None


def _clean_cell(value: str) -> str:
    return _strip_markdown(value).strip("| ")


def _header_key(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", _strip_markdown(value).lower())).strip("_")


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def parse_action_rows(review_text: str) -> list[dict[str, Any]]:
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]

    for idx, line in enumerate(lines):
        lowered = _strip_markdown(line).lower()
        if "priority" not in lowered or "owner" not in lowered or "action" not in lowered:
            continue

        headers = [_header_key(cell) for cell in line.strip().strip("|").split("|")]
        rows: list[dict[str, Any]] = []

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
            if len(cells) < 3:
                continue
            if _is_separator(cells):
                continue

            data = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}

            priority_key = next((k for k in data if "priority" in k), "")
            owner_key = next((k for k in data if "owner" in k or "team" in k), "")
            action_key = next((k for k in data if "action" in k), "")
            evidence_key = next((k for k in data if "evidence" in k), "")

            rows.append(
                {
                    "priority_score": _score_number(data.get(priority_key)),
                    "owner_team": data.get(owner_key),
                    "action": data.get(action_key),
                    "evidence": data.get(evidence_key),
                }
            )

        if rows:
            return rows

    return []


def parse_caretaker_performance_review(review_text: str) -> dict[str, Any]:
    text = str(review_text or "")

    overall_score = (
        _score_after_label(text, "Overall score")
        or _score_after_label(text, "Score")
        or _score_after_label(text, "Overall")
    )

    priority_score = (
        _score_after_label(text, "Priority score")
        or _score_after_label(text, "Priority")
    )

    action_rows = parse_action_rows(text)

    if priority_score is None:
        priorities = [
            row.get("priority_score")
            for row in action_rows
            if isinstance(row.get("priority_score"), (int, float))
        ]
        if priorities:
            priority_score = max(priorities)

    return {
        "overall_score": overall_score,
        "priority_score": priority_score,
        "communication_score": _score_after_label(text, "Communication score"),
        "site_visit_score": _score_after_label(text, "Site visit score"),
        "ticket_score": _score_after_label(text, "Ticket score"),
        "management_score": _score_after_label(text, "Management score"),
        "overall_risk": _risk_after_label(text, "Overall risk") or _risk_after_label(text, "Risk"),
        "main_reason": _main_reason(text),
        "action_rows": action_rows,
    }