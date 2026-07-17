from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from .cache import get_booking_review_cache, store_booking_review_cache
from .common import (
    BOOKING_REVIEW_CONTEXT_VERSION,
    BOOKING_REVIEW_SOURCE_SCOPE,
    DEFAULT_MODEL,
    DEFAULT_SCHEMA,
    compact_dict,
    now_ist_naive,
    q1,
    score_number,
    table_columns,
    table_exists,
    table_ref,
)
from .llm_client import run_openai_prompt, stable_json_hash
from .parsing import parse_booking_review_text
from .prompt import build_booking_handling_prompt
from .team_policy import (
    enrich_support_with_team_policy,
    team_policy_context,
)


_TEAM_KEYS = ("Ops", "Sales", "Finance", "Caretaker")


def _build_missed_call_evidence_by_team(calls: dict[str, Any]) -> dict[str, Any]:
    """Produce a per-team missed-call evidence block for the LLM.

    Teams that had zero missed calls are omitted so the LLM doesn't score them
    negatively for something that never happened.

    Sources:
      calls.missed_by_team            – total missed inbound calls per team
      calls.missed_sla_breached_by_team – missed + SLA breached (no/late recovery)
      calls.missed_recovered_by_team  – recovered on time
    """
    missed_by: dict[str, int] = {str(k): int(v or 0) for k, v in (calls.get("missed_by_team") or {}).items()}
    breached_by: dict[str, int] = {str(k): int(v or 0) for k, v in (calls.get("missed_sla_breached_by_team") or {}).items()}
    recovered_by: dict[str, int] = {str(k): int(v or 0) for k, v in (calls.get("missed_recovered_by_team") or {}).items()}
    # pending = total missed - breached - recovered (remainder still within deadline)
    all_teams = set(missed_by) | set(breached_by) | set(recovered_by)
    out: dict[str, Any] = {}
    for team in all_teams:
        total = missed_by.get(team, 0)
        breached = breached_by.get(team, 0)
        recovered = recovered_by.get(team, 0)
        pending = max(0, total - breached - recovered)
        if total == 0 and breached == 0:
            continue  # nothing to report
        out[team] = compact_dict(
            {
                "missed": total,
                "sla_breached": breached,
                "recovered_on_time": recovered,
                "sla_pending": pending,
                "sla_note": (
                    "all_recovered_on_time" if total > 0 and breached == 0 and pending == 0
                    else "breach_present" if breached > 0
                    else "within_deadline_pending" if pending > 0
                    else None
                ),
            }
        )
    return out

def _cap_missed_call_only_scores(
    stakeholder_scores: list[dict[str, Any]],
    llm_context: dict[str, Any],
) -> list[dict[str, Any]]:
    missed_by_team = llm_context.get("missed_call_evidence_by_team") or {}
    support = llm_context.get("support") or {}
    ticket_groups = support.get("ticket_groups") or {}

    def open_ticket_count_for_team(team: str) -> int:
        team_key = {
            "Ops": "Operations",
            "Finance": "Finance",
            "Caretaker": "Caretaker",
            "Sales": "Sales",
        }.get(team, team)
        group = ticket_groups.get(team_key) or {}
        if not isinstance(group, dict):
            return 0
        total = 0
        for value in group.values():
            if isinstance(value, dict):
                total += int(value.get("open_count") or 0)
                total += len(value.get("open_tickets") or [])
        return total

    def min_score_for_breach_count(count: int) -> float:
        if count <= 0:
            return 10.0
        if count == 1:
            return 9.5
        if count == 2:
            return 9.0
        return 8.5

    for row in stakeholder_scores:
        if not isinstance(row, dict):
            continue
        team = str(row.get("stakeholder_team") or "").strip()
        if not team:
            continue
        team_missed = missed_by_team.get(team) or {}
        if not isinstance(team_missed, dict):
            team_missed = {}
        sla_breached = int(team_missed.get("sla_breached") or 0)
        
        if sla_breached <= 0:
            team_open_tickets = open_ticket_count_for_team(team)
            if team_open_tickets == 0:
                row["score"] = 10
                row["gaps"] = "None"
                row["priority_score"] = min(int(row.get("priority_score") or 1), 3)
                row["evidence"] = "No own missed-call SLA breach; no open tickets"
            else:
                text = f"{row.get('gaps') or ''} {row.get('evidence') or ''}".lower()
                if "missed" in text or "call" in text or "sla" in text:
                    row["evidence"] = (
                        "No own missed-call SLA breach. "
                        "Score/priority should be based only on this team's ticket work."
                    )
                    gaps_text = str(row.get("gaps") or "").strip().lower()
                    if "missed" in gaps_text or "call" in gaps_text or "sla" in gaps_text:
                        row["gaps"] = "Ticket-related gap only"
            continue
        
        if open_ticket_count_for_team(team) > 0:
            continue
        min_score = min_score_for_breach_count(sla_breached)
        try:
            current_score = float(row.get("score") or 10)
        except Exception:
            continue
        if current_score < min_score:
            row["score"] = min_score
            row["gaps"] = f"{sla_breached} missed-call SLA breach{'es' if sla_breached != 1 else ''}"
            row["evidence"] = (
                f"{team_missed.get('missed', sla_breached)} missed calls total; "
                f"{sla_breached} SLA breached; "
                f"missed-call-only deduction capped by business rule"
            )
    return stakeholder_scores

def _support_group_open_count(support: dict[str, Any], owner_team: str) -> int:
    group = (support.get("ticket_groups") or {}).get(owner_team) or {}
    total = 0
    for sub in group.values():
        if isinstance(sub, dict):
            total += int(sub.get("open_count") or 0)
    return total

NO_ACTION_RE = re.compile(
    r"\b(no immediate action|no action needed|monitoring only|informational only|no current issue|nothing pending|no unresolved issue)\b",
    flags=re.I,
)

ISSUE_THEME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wifi", re.compile(r"\b(wifi|wi-fi|internet|network|speed)\b", re.I)),
    ("cleaning", re.compile(r"\b(clean|cleaning|dirty|housekeeping)\b", re.I)),
    ("maintenance", re.compile(r"\b(maintenance|repair|broken|not working|leak|water|electric|power|fan|tv|ac)\b", re.I)),
    ("check_in", re.compile(r"\b(check[- ]?in|move[- ]?in|onboarding|ready|readiness)\b", re.I)),
    ("check_out", re.compile(r"\b(check[- ]?out|move[- ]?out|early checkout|checkout)\b", re.I)),
    ("payment", re.compile(r"\b(payment|paid|upi|invoice|rent|refund|deposit|amount|balance)\b", re.I)),
    ("missed_call", re.compile(r"\b(missed call|missed inbound|call gap|not picked)\b", re.I)),
    ("handoff", re.compile(r"\b(handoff|hand[- ]?off|ownership|unclear owner|wrong team)\b", re.I)),
    ("ticket", re.compile(r"\b(ticket|open issue|unresolved|resolved|closed)\b", re.I)),
    ("amenities", re.compile(r"\b(amenit|furniture|geyser|appliance|kitchen|bed|mattress)\b", re.I)),
)


# -----------------------------------------------------------------------------
# Small helpers for derived work/scope fields
# -----------------------------------------------------------------------------

def _safe_sql_ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def _text_col(alias: str, column_name: str) -> str:
    return f"NULLIF(TRIM({alias}.{_safe_sql_ident(column_name)}::text), '')"


def _existing_text_expr(alias: str, columns: set[str], candidates: Sequence[str], *, default: str = "NULL") -> str:
    parts = [_text_col(alias, column) for column in candidates if column in columns]
    if not parts:
        return default
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _existing_raw_expr(alias: str, columns: set[str], candidates: Sequence[str], *, default: str = "NULL") -> str:
    parts = [f"{alias}.{_safe_sql_ident(column)}" for column in candidates if column in columns]
    if not parts:
        return default
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _first_available_table(db: Session, schema: str, names: Sequence[str]) -> str | None:
    for name in names:
        if table_exists(db, schema, name):
            return name
    return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        text_value = str(value or "").strip()
        return int(text_value) if re.fullmatch(r"\d+", text_value) else None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        parsed = score_number(value)
        try:
            return float(parsed) if parsed is not None else None
        except Exception:
            return None


def _priority_score(value: Any, default: int | None = None) -> int | None:
    score = _to_float(value)
    if score is None:
        return default
    return max(1, min(10, int(round(score))))


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text.lower() in {"none", "not visible", "not applicable"}:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _is_no_action_row(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ("action", "evidence", "owner_team", "owner"))
    return bool(NO_ACTION_RE.search(text))


def _extract_issue_themes(*texts: Any) -> list[str]:
    joined = " ".join(str(text or "") for text in texts)
    themes = [theme for theme, pattern in ISSUE_THEME_PATTERNS if pattern.search(joined)]
    return _dedupe_strings(themes)


def _parse_stay_dates(value: Any) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    parts = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], None
    return None, None

def _derive_overall_score_from_stakeholders(
    stakeholder_scores: list[dict[str, Any]],
    fallback_score: Any = None,
    open_ticket_count: int = 0,
) -> int | None:
    def score_value(value: Any) -> float | None:
        score = _to_float(value)
        if score is None:
            return None
        if score < 0 or score > 10:
            return None
        return score

    def is_neutral_row(row: dict[str, Any]) -> bool:
        score = score_value(row.get("score"))
        priority = score_value(row.get("priority_score"))
        handled = str(row.get("handled") or "").strip().lower()
        gaps = str(row.get("gaps") or "").strip().lower()
        evidence = str(row.get("evidence") or "").strip().lower()
        no_gap = gaps in {
            "",
            "none",
            "no",
            "nil",
            "na",
            "n/a",
            "not visible",
        }
        no_work = (
            "not visible" in handled
            or "no tickets" in handled
            or "no tickets" in evidence
            or "no visible complaints" in evidence
            or "no own missed-call sla breach" in evidence
        )
        return (
            score is not None
            and score >= 10
            and no_gap
            and no_work
            and (priority is None or priority <= 3)
        )

    active_scores: list[float] = []
    for row in stakeholder_scores or []:
        if not isinstance(row, dict):
            continue
        score = score_value(row.get("score"))
        if score is None:
            continue
        if not is_neutral_row(row):
            active_scores.append(score)
    if not active_scores:
        for row in stakeholder_scores or []:
            if isinstance(row, dict):
                score = score_value(row.get("score"))
                if score is not None:
                    active_scores.append(score)

    if active_scores:
        avg_score = sum(active_scores) / len(active_scores)
        final_score = max(1, min(10, int(avg_score)))
        
        if open_ticket_count >= 8:
            final_score = min(final_score, 5)
        elif open_ticket_count >= 5:
            final_score = min(final_score, 6)
        elif open_ticket_count >= 2:
            final_score = min(final_score, 7)
        elif open_ticket_count == 1:
            final_score = min(final_score, 8)
        return final_score
    
    fallback = score_value(fallback_score)
    if fallback is not None:
        return max(1, min(10, int(fallback)))
    return None

def derive_booking_review_rollup(
    *,
    parsed: dict[str, Any],
    action_rows: list[dict[str, Any]],
    stakeholder_scores: list[dict[str, Any]],
    booking_summary: dict[str, Any],
    llm_context: dict[str, Any],
    customer_days: int,
) -> dict[str, Any]:
    """Derive deterministic fields used by manager/caretaker/building queues."""
    booking = llm_context.get("booking") if isinstance(llm_context.get("booking"), dict) else {}
    summary = booking_summary if isinstance(booking_summary, dict) else {}

    normalized_actions: list[dict[str, Any]] = [row for row in (action_rows or []) if isinstance(row, dict)]
    actionable = [row for row in normalized_actions if not _is_no_action_row(row)]
    candidates = actionable or normalized_actions

    def priority_value(row: dict[str, Any]) -> int:
        return _priority_score(row.get("priority_score"), 0) or 0

    top_action = max(candidates, key=priority_value, default={}) if candidates else {}
    action_owner_teams = _dedupe_strings(row.get("owner_team") or row.get("owner") for row in normalized_actions)
    is_no_action = bool(normalized_actions) and not actionable

    if is_no_action:
        action_priorities = [_priority_score(row.get("priority_score"), 2) or 2 for row in normalized_actions]
        work_priority_score = max(1, min(3, min(action_priorities or [2])))
    elif top_action:
        work_priority_score = _priority_score(top_action.get("priority_score"), None)
    else:
        work_priority_score = _priority_score(parsed.get("overall_priority_score"), None)
    
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    open_ticket_count = (
        _to_int(summary.get("open_tickets"))
        or _to_int(counts.get("open_tickets"))
        or 0
    )
    missed_call_breached_count = (
        _to_int(summary.get("missed_call_sla_breached"))
        or 0
    )
    breached_by_team = summary.get("missed_call_sla_breached_by_team")
    if isinstance(breached_by_team, dict) and breached_by_team:
        missed_call_breached_count = sum(
            int(value or 0)
            for value in breached_by_team.values()
        )

    if open_ticket_count >= 2 or missed_call_breached_count >= 4:
        work_priority_score = 9
        owner_team = top_action.get("owner_team") or top_action.get("owner") or "Ops"

        reasons = []
        if open_ticket_count >= 2:
            reasons.append(f"{open_ticket_count} open tickets")
        if missed_call_breached_count >= 4:
            reasons.append(f"{missed_call_breached_count} missed-call SLA breaches")
        top_action = {
            "priority_score": work_priority_score,
            "owner_team": owner_team,
            "action": "Immediate action required",
            "evidence": "; ".join(reasons),
        }
        is_no_action = False
    elif open_ticket_count == 1 or 2 <= missed_call_breached_count <= 3:
        work_priority_score = 7
        owner_team = top_action.get("owner_team") or top_action.get("owner") or "Ops"
        reasons = []
        if open_ticket_count == 1:
            reasons.append("1 open ticket")
        if 2 <= missed_call_breached_count <= 3:
            reasons.append(f"{missed_call_breached_count} missed-call SLA breaches")
        top_action = {
            "priority_score": work_priority_score,
            "owner_team": owner_team,
            "action": "Needs attention",
            "evidence": "; ".join(reasons),
        }
        is_no_action = False
    elif open_ticket_count == 0 and missed_call_breached_count == 0:
        work_priority_score = 1
        top_action = {
            "priority_score": work_priority_score,
            "owner_team": "None",
            "action": "No immediate action",
            "evidence": "0 open tickets; 0 missed-call SLA breaches",
        }
        is_no_action = True
    else:
        work_priority_score = max(work_priority_score or 1, 4)
        top_action = {
            "priority_score": work_priority_score,
            "owner_team": top_action.get("owner_team") or top_action.get("owner") or "Ops",
            "action": "Monitor missed-call follow-up",
            "evidence": f"{missed_call_breached_count} missed-call SLA breach",
        }
        is_no_action = False

    stay_from, stay_to = _parse_stay_dates(booking.get("stay") or summary.get("stay_dates"))
    issue_themes = _extract_issue_themes(
        parsed.get("main_reason"),
        top_action.get("action"),
        top_action.get("evidence"),
        " ".join(str(row.get("gaps") or "") for row in stakeholder_scores or [] if isinstance(row, dict)),
    )

    return compact_dict(
        {
            "review_window_days": int(customer_days),
            "review_generated_at": llm_context.get("generated_at"),
            "booking_status": booking.get("status"),
            "stay_state": booking.get("state") or summary.get("state"),
            "booking_type": booking.get("type") or summary.get("booking_type"),
            "property_id": _to_int(booking.get("property_id") or summary.get("property_id")),
            "travel_from_date": stay_from,
            "travel_to_date": stay_to,
            "work_priority_score": work_priority_score,
            "work_owner_team": top_action.get("owner_team") or top_action.get("owner"),
            "work_action": top_action.get("action"),
            "work_evidence": top_action.get("evidence"),
            "is_no_action": is_no_action,
            "action_owner_teams": action_owner_teams,
            "issue_themes": issue_themes,
        }
    )


@dataclass
class BookingCommunicationReviewRunner:
    db: Session
    schema: str = DEFAULT_SCHEMA

    def resolve_cache_booking_id(self, booking_id: int) -> int:
        """Resolve to staging_booking_confirm.source_id when available.

        This is a cheap DB lookup, not a prompt/context build. It keeps cache rows
        stable when some callers pass booking_id and others pass source_id.
        """
        if not table_exists(self.db, self.schema, "staging_booking_confirm"):
            return int(booking_id)
        row = q1(
            self.db,
            f"""
            SELECT source_id, booking_id
            FROM {table_ref(self.schema, 'staging_booking_confirm')}
            WHERE source_id::text = :booking_id_text
               OR booking_id::text = :booking_id_text
            ORDER BY source_id DESC NULLS LAST
            LIMIT 1
            """,
            {"booking_id_text": str(int(booking_id))},
        )
        if row and row.get("source_id") not in (None, ""):
            try:
                return int(row["source_id"])
            except Exception:
                return int(booking_id)
        return int(booking_id)

    def hydrate_booking_review_scope(self, booking_id: int) -> dict[str, Any]:
        """Fetch current booking/property/building/owner scope for one booking.

        This gives the cache row enough fields for manager/caretaker/building
        queues and also fixes fast-dashboard blank status/property columns.
        """
        if not table_exists(self.db, self.schema, "staging_booking_confirm"):
            return {}

        b_cols = table_columns(self.db, self.schema, "staging_booking_confirm")
        if "source_id" not in b_cols and "booking_id" not in b_cols:
            return {}

        id_expr = "b.source_id" if "source_id" in b_cols else "b.booking_id"
        alt_id_expr = "b.booking_id" if "booking_id" in b_cols else "b.source_id" if "source_id" in b_cols else id_expr
        id_exprs: list[str] = []
        for expr in (id_expr, alt_id_expr):
            if expr not in id_exprs:
                id_exprs.append(expr)

        status_expr = _existing_text_expr("b", b_cols, ["booking_status", "status"])
        booking_type_expr = _existing_text_expr("b", b_cols, ["booking_type", "period", "type"])
        prop_id_expr = _existing_raw_expr("b", b_cols, ["prop_id"])
        travel_from_expr = _existing_raw_expr("b", b_cols, ["travel_from_date"])
        travel_to_expr = _existing_raw_expr("b", b_cols, ["travel_to_date"])
        booking_sales_expr = _existing_text_expr("b", b_cols, ["created_by", "salesperson", "sales", "added_by"])

        joins: list[str] = []
        property_name_expr = _existing_text_expr("b", b_cols, ["property_name", "prop_name", "unit_name"], default="NULL")
        property_building_expr = _existing_raw_expr("b", b_cols, ["building_id"], default="NULL")
        property_id_select = f"{prop_id_expr}" if prop_id_expr != "NULL" else "NULL"

        if prop_id_expr != "NULL" and table_exists(self.db, self.schema, "staging_property_unit"):
            pu_cols = table_columns(self.db, self.schema, "staging_property_unit")
            if "prop_id" in pu_cols or "source_id" in pu_cols:
                prop_key = "prop_id" if "prop_id" in pu_cols else "source_id"
                order_col = "last_updated_on" if "last_updated_on" in pu_cols else "synced_at" if "synced_at" in pu_cols else prop_key
                select_parts = [f"pu_raw.{prop_key}::text AS prop_key"]
                for column in ("prop_id", "unit_name", "display_property_name", "listing_title", "building_id", "rms_prop"):
                    select_parts.append(f"pu_raw.{column}" if column in pu_cols else f"NULL AS {column}")
                joins.append(
                    f"""
                    LEFT JOIN (
                        SELECT *
                        FROM (
                            SELECT
                                {', '.join(select_parts)},
                                ROW_NUMBER() OVER (PARTITION BY pu_raw.{prop_key}::text ORDER BY pu_raw.{order_col} DESC NULLS LAST) AS _rn
                            FROM {table_ref(self.schema, 'staging_property_unit')} pu_raw
                        ) pu_ranked
                        WHERE _rn = 1
                    ) pu ON pu.prop_key = b.prop_id::text
                    """
                )
                property_name_expr = "COALESCE(NULLIF(TRIM(pu.unit_name::text), ''), NULLIF(TRIM(pu.display_property_name::text), ''), NULLIF(TRIM(pu.listing_title::text), ''))"
                property_building_expr = "pu.building_id"
                property_id_select = "COALESCE(NULLIF(TRIM(pu.prop_id::text), ''), NULLIF(TRIM(b.prop_id::text), ''))"

        building_name_expr = "NULL"
        sales_owner_expr = booking_sales_expr
        caretaker_expr = "NULL"
        ops_owner_expr = "NULL"
        ops_manager_expr = "NULL"
        finance_owner_expr = "NULL"
        finance_manager_expr = "NULL"

        building_table_name = _first_available_table(
            self.db,
            self.schema,
            ["staging_buildings", "staging_building_details", "staging_building_detail"],
        )
        if building_table_name and property_building_expr != "NULL":
            bd_cols = table_columns(self.db, self.schema, building_table_name)
            key = "building_id" if "building_id" in bd_cols else "source_id" if "source_id" in bd_cols else None
            if key:
                order_col = "updated_on" if "updated_on" in bd_cols else "last_updated_on" if "last_updated_on" in bd_cols else "synced_at" if "synced_at" in bd_cols else key
                select_parts = [f"bd_raw.{key}::text AS building_key"]
                for column in (
                    "building_id", "building_name", "bname", "name", "caretaker", "supervisor", "superviser",
                    "ops_manager", "finance_supervisor", "finance_superviser", "finance_manager", "sales", "marketing",
                ):
                    select_parts.append(f"bd_raw.{column}" if column in bd_cols else f"NULL AS {column}")
                joins.append(
                    f"""
                    LEFT JOIN (
                        SELECT *
                        FROM (
                            SELECT
                                {', '.join(select_parts)},
                                ROW_NUMBER() OVER (PARTITION BY bd_raw.{key}::text ORDER BY bd_raw.{order_col} DESC NULLS LAST) AS _rn
                            FROM {table_ref(self.schema, building_table_name)} bd_raw
                        ) bd_ranked
                        WHERE _rn = 1
                    ) bd ON bd.building_key = {property_building_expr}::text
                    """
                )
                building_name_expr = "COALESCE(NULLIF(TRIM(bd.building_name::text), ''), NULLIF(TRIM(bd.bname::text), ''), NULLIF(TRIM(bd.name::text), ''))"
                sales_owner_expr = f"COALESCE({booking_sales_expr}, NULLIF(TRIM(bd.sales::text), ''))"
                caretaker_expr = "NULLIF(TRIM(bd.caretaker::text), '')"
                ops_manager_expr = "COALESCE(NULLIF(TRIM(bd.ops_manager::text), ''), NULLIF(TRIM(bd.supervisor::text), ''), NULLIF(TRIM(bd.superviser::text), ''))"
                ops_owner_expr = "NULL"
                finance_manager_expr = "COALESCE(NULLIF(TRIM(bd.finance_manager::text), ''), NULLIF(TRIM(bd.finance_supervisor::text), ''), NULLIF(TRIM(bd.finance_superviser::text), ''))"
                finance_owner_expr = finance_manager_expr

        if travel_from_expr != "NULL" and travel_to_expr != "NULL":
            stay_state_expr = f"""
            CASE
                WHEN LOWER(TRIM(COALESCE({status_expr}, ''))) NOT IN ('success', 'booked') THEN 'inactive_non_success'
                WHEN {travel_to_expr}::date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'stay_completed'
                WHEN {travel_to_expr}::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'checkout_due'
                WHEN {travel_from_expr}::date <= (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                 AND {travel_to_expr}::date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'active_stay'
                WHEN {travel_from_expr}::date > (NOW() AT TIME ZONE 'Asia/Kolkata')::date THEN 'upcoming_active_booking'
                ELSE 'active_booking_open'
            END
            """
        else:
            stay_state_expr = "'unknown'"

        where_sql = " OR ".join(f"{expr}::text = :booking_id_text" for expr in id_exprs)
        row = q1(
            self.db,
            f"""
            SELECT
                {id_expr} AS canonical_booking_id,
                {status_expr} AS booking_status,
                ({stay_state_expr}) AS stay_state,
                {booking_type_expr} AS booking_type,
                {property_id_select} AS property_id,
                {property_name_expr} AS property_name,
                {property_building_expr} AS building_id,
                {building_name_expr} AS building_name,
                CASE WHEN {travel_from_expr} IS NULL THEN NULL ELSE {travel_from_expr}::date END AS travel_from_date,
                CASE WHEN {travel_to_expr} IS NULL THEN NULL ELSE {travel_to_expr}::date END AS travel_to_date,
                {sales_owner_expr} AS sales_owner,
                {ops_owner_expr} AS ops_owner,
                {ops_manager_expr} AS ops_manager,
                {caretaker_expr} AS caretaker,
                {finance_owner_expr} AS finance_owner,
                {finance_manager_expr} AS finance_manager
            FROM {table_ref(self.schema, 'staging_booking_confirm')} b
            {' '.join(joins)}
            WHERE {where_sql}
            ORDER BY {id_expr} DESC NULLS LAST
            LIMIT 1
            """,
            {"booking_id_text": str(int(booking_id))},
        )
        if not row:
            return {}
        return compact_dict(
            {
                "booking_status": row.get("booking_status"),
                "stay_state": row.get("stay_state"),
                "booking_type": row.get("booking_type"),
                "property_id": _to_int(row.get("property_id")),
                "property_name": row.get("property_name"),
                "building_id": _to_int(row.get("building_id")),
                "building_name": row.get("building_name"),
                "travel_from_date": row.get("travel_from_date"),
                "travel_to_date": row.get("travel_to_date"),
                "sales_owner": row.get("sales_owner"),
                "ops_owner": row.get("ops_owner"),
                "ops_manager": row.get("ops_manager"),
                "caretaker": row.get("caretaker"),
                "finance_owner": row.get("finance_owner"),
                "finance_manager": row.get("finance_manager"),
            }
        )

    def build_prompt_for_booking(
        self,
        *,
        booking_id: int,
        customer_days: int = 30,
        max_llm_messages: int = 12,
        max_llm_text_chars: int = 220,
    ) -> dict[str, Any]:
        from app.services.analytics_engine.capabilities.customer_brief_service import CustomerBriefService

        canonical_booking_id = self.resolve_cache_booking_id(int(booking_id))
        service = CustomerBriefService(db=self.db, schema=self.schema)
        full_payload = service.build(booking_id=canonical_booking_id, conversation_days=int(customer_days))
        compact_brief = service.compact_for_llm(
            full_payload,
            max_messages=int(max_llm_messages),
            max_text_chars=int(max_llm_text_chars),
        )
        scope = self.hydrate_booking_review_scope(canonical_booking_id)
        booking = compact_brief.get("booking") or {}
        support = enrich_support_with_team_policy(compact_brief.get("support") or {})
        conversation = compact_brief.get("conversation") or {}
        team_policy = team_policy_context(scope)
        conversation_stats = conversation.get("stats") if isinstance(conversation.get("stats"), dict) else {}
        by_channel = conversation_stats.get("by_channel") if isinstance(conversation_stats.get("by_channel"), dict) else {}
        calls = conversation_stats.get("calls") if isinstance(conversation_stats.get("calls"), dict) else {}
        booking_summary = compact_dict(
            {
                "booking_id": canonical_booking_id,
                "requested_booking_id": int(booking_id) if int(booking_id) != canonical_booking_id else None,
                "state": booking.get("state") or booking.get("status"),
                "stay_dates": booking.get("stay"),
                "property_id": booking.get("property_id"),
                "booking_type": booking.get("type"),
                "open_tickets": support.get("open_ticket_count"),
                "ops_open_tickets": _support_group_open_count(support, "Operations"),
                "closed_tickets": support.get("closed_ticket_count"),
                "total_tickets": support.get("total_ticket_count"),
                "call_count": calls.get("connected"),
                "missed_calls": calls.get("missed"),
                "missed_calls_recovered_on_time": calls.get("missed_recovered_on_time"),
                "missed_call_sla_breached": calls.get("missed_sla_breached"),
                "missed_call_sla_pending": calls.get("missed_sla_pending"),
                "missed_calls_by_team": calls.get("missed_by_team"),
                "missed_recovered_by_team": calls.get("missed_recovered_by_team"),
                "missed_call_sla_breached_by_team": calls.get("missed_sla_breached_by_team"),
                "counts": compact_dict(
                    {
                        "events": conversation.get("count") or conversation.get("deduped_count"),
                        "messages": conversation.get("deduped_count"),
                        "calls": by_channel.get("call"),
                        "whatsapp": by_channel.get("whatsapp"),
                        "emails": by_channel.get("email"),
                        "tickets_total": support.get("total_ticket_count"),
                        "open_tickets": support.get("open_ticket_count"),
                        "ops_open_tickets": _support_group_open_count(support, "Operations"),
                        "closed_tickets": support.get("closed_ticket_count"),
                    }
                ),
                "quality_flags": full_payload.get("quality_flags") or compact_brief.get("quality_flags"),
            }
        )
        missed_call_evidence_by_team = _build_missed_call_evidence_by_team(calls)
        llm_context = compact_dict(
            {
                "context_version": BOOKING_REVIEW_CONTEXT_VERSION,
                "source_scope": BOOKING_REVIEW_SOURCE_SCOPE,
                "generated_at": now_ist_naive().isoformat(sep=" "),
                "booking_id": canonical_booking_id,
                "requested_booking_id": int(booking_id) if int(booking_id) != canonical_booking_id else None,
                "customer_days": int(customer_days),
                "team_policy": team_policy,
                "booking_scope": scope,
                "summary": booking_summary,
                "customer": compact_brief.get("customer"),
                "booking": compact_brief.get("booking"),
                "support": support,
                "missed_call_evidence_by_team": missed_call_evidence_by_team or None,
                "conversation": conversation,
                "facts": full_payload.get("facts"),
                "quality_flags": full_payload.get("quality_flags"),
            }
        )
        prompt = build_booking_handling_prompt(llm_context)
        return {
            "booking_id": canonical_booking_id,
            "requested_booking_id": int(booking_id),
            "llm_context": llm_context,
            "llm_prompt": prompt,
            "context_hash": stable_json_hash(llm_context),
            "booking_summary": booking_summary,
        }

    def review_one(
        self,
        *,
        booking_id: int,
        customer_days: int = 30,
        run_llm: bool = True,
        model: str = DEFAULT_MODEL,
        max_llm_messages: int = 12,
        max_llm_text_chars: int = 220,
        timeout_seconds: int = 120,
        use_cache: bool = True,
        force_refresh: bool = False,
        include_context: bool = False,
        include_prompt: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        canonical_booking_id = self.resolve_cache_booking_id(int(booking_id))
        timings: list[dict[str, Any]] = [
            {"stage": "resolve_cache_booking_id", "elapsed_ms": round((time.perf_counter() - started) * 1000, 2), "booking_id": canonical_booking_id}
        ]

        cached_any = get_booking_review_cache(self.db, self.schema, booking_id=canonical_booking_id, ok_only=False) if use_cache else None
        if use_cache and not force_refresh:
            if cached_any and str(cached_any.get("status") or "").lower() == "ok" and not cached_any.get("stale_at"):
                out = dict(cached_any)
                out.update(
                    {
                        "cached": True,
                        "cache_status": "hit",
                        "booking_id": canonical_booking_id,
                        "requested_booking_id": int(booking_id) if int(booking_id) != canonical_booking_id else None,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                        "timings": timings,
                    }
                )

                # IMPORTANT FAST-PATH RULE:
                # Cache hits must return immediately. Do not rebuild the customer
                # brief/prompt just because include_prompt/include_context was
                # passed by the UI. If a fresh prompt/context is needed for
                # debugging, call with force_refresh=true (or recompute=true).
                if include_context or include_prompt:
                    out["prompt_cache_note"] = (
                        "Skipped prompt/context rebuild on cache hit. "
                        "Use force_refresh=true&include_prompt=true only for an explicit debug/recompute call."
                    )
                return compact_dict(out)

        if not run_llm:
            out = dict(cached_any or {})
            out.update(
                {
                    "view": "booking_llm_rating",
                    "booking_id": canonical_booking_id,
                    "requested_booking_id": int(booking_id) if int(booking_id) != canonical_booking_id else None,
                    "status": cached_any.get("status") if cached_any else "not_rated",
                    "cached": bool(cached_any),
                    "cache_status": "stale" if cached_any and cached_any.get("stale_at") else "hit_non_ok" if cached_any else "miss",
                    "run_llm": False,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "timings": timings,
                }
            )
            return compact_dict(out)

        build_started = time.perf_counter()
        built = self.build_prompt_for_booking(
            booking_id=canonical_booking_id,
            customer_days=int(customer_days),
            max_llm_messages=int(max_llm_messages),
            max_llm_text_chars=int(max_llm_text_chars),
        )
        timings.append({"stage": "build_prompt", "elapsed_ms": round((time.perf_counter() - build_started) * 1000, 2)})
        prompt = built["llm_prompt"]
        context_hash = built["context_hash"]
        llm_started = time.perf_counter()
        review_text = None
        error = None
        try:
            review_text = run_openai_prompt(prompt, payload=built.get("llm_context"), model=model, timeout_seconds=timeout_seconds)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
        timings.append({"stage": "llm_call", "elapsed_ms": round((time.perf_counter() - llm_started) * 1000, 2), "status": "error" if error else "ok"})

        parsed = parse_booking_review_text(review_text or "") if review_text else {}
        action_rows: list[dict[str, Any]] = []
        for row in parsed.get("action_rows") or []:
            action_rows.append(
                compact_dict(
                    {
                        "booking_id": canonical_booking_id,
                        "priority_score": row.get("priority_score"),
                        "owner_team": row.get("owner_team"),
                        "action": row.get("action"),
                        "evidence": row.get("evidence"),
                        "score": parsed.get("overall_score"),
                    }
                )
            )
        stakeholder_scores: list[dict[str, Any]] = []
        for row in parsed.get("stakeholder_scores") or []:
            stakeholder_scores.append(compact_dict({"booking_id": canonical_booking_id, **row}))
        stakeholder_scores = _cap_missed_call_only_scores(
            stakeholder_scores,
            built.get("llm_context") or {},
        )
        summary_for_score = built.get("booking_summary") or {}
        counts_for_score = summary_for_score.get("counts") if isinstance(summary_for_score.get("counts"), dict) else {}
        open_ticket_count_for_score = (
            _to_int(summary_for_score.get("open_tickets"))
            or _to_int(counts_for_score.get("open_tickets"))
            or 0
        )
        derived_overall_score = _derive_overall_score_from_stakeholders(
            stakeholder_scores,
            fallback_score=parsed.get("overall_score"),
            open_ticket_count=open_ticket_count_for_score,
        )
        if derived_overall_score is not None:
            parsed["overall_score"] = derived_overall_score

        rollup = derive_booking_review_rollup(
            parsed=parsed,
            action_rows=action_rows,
            stakeholder_scores=stakeholder_scores,
            booking_summary=built.get("booking_summary") or {},
            llm_context=built.get("llm_context") or {},
            customer_days=int(customer_days),
        )
        scope = self.hydrate_booking_review_scope(canonical_booking_id)
        summary_state = (built.get("booking_summary") or {}).get("state")
        if summary_state in {"checkout_completed_early", "checkout_completed", "stay_completed"}:
            scope.pop("stay_state", None)
        rollup.update(scope)
        if summary_state in {"checkout_completed_early", "checkout_completed", "stay_completed"}:
            rollup["stay_state"] = summary_state
        if scope:
            timings.append({"stage": "hydrate_scope", "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})

        payload = compact_dict(
            {
                "view": "booking_llm_rating",
                "booking_id": canonical_booking_id,
                "requested_booking_id": int(booking_id) if int(booking_id) != canonical_booking_id else None,
                "status": "error" if error else "ok",
                "error": error,
                "model": model,
                "context_version": BOOKING_REVIEW_CONTEXT_VERSION,
                "context_hash": context_hash,
                "cached": False,
                "cache_status": "stored" if not error else "error_stored_or_preserved_existing_ok",
                "overall_score": parsed.get("overall_score"),
                "overall_priority_score": parsed.get("overall_priority_score"),
                "work_priority_score": rollup.get("work_priority_score"),
                "customer_perspective_score": parsed.get("customer_perspective_score"),
                "operations_score": parsed.get("operations_score"),
                "support_score": parsed.get("support_score"),
                "main_reason": parsed.get("main_reason"),
                "booking_summary": built.get("booking_summary"),
                "summary": built.get("booking_summary"),
                "action_rows": action_rows,
                "stakeholder_scores": stakeholder_scores,
                "actor_scores": [],
                "customer_followup": parsed.get("customer_followup"),
                **rollup,
                "review_text": review_text,
                "llm_prompt": prompt,
                "llm_context": built.get("llm_context"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "timings": timings,
            }
        )
        store_booking_review_cache(self.db, self.schema, payload=payload)
        if not include_context:
            payload.pop("llm_context", None)
        if not include_prompt:
            payload.pop("llm_prompt", None)
        return payload

    def review_many(
        self,
        *,
        booking_ids: Sequence[int],
        customer_days: int = 30,
        run_llm: bool = True,
        model: str = DEFAULT_MODEL,
        max_bookings: int = 25,
        max_llm_messages: int = 12,
        max_llm_text_chars: int = 220,
        timeout_seconds: int = 120,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        seen: set[int] = set()
        capped_ids: list[int] = []
        for raw in booking_ids or []:
            booking_id = int(raw)
            if booking_id in seen:
                continue
            seen.add(booking_id)
            capped_ids.append(booking_id)
            if len(capped_ids) >= max(1, int(max_bookings or 1)):
                break
        reviews = [
            self.review_one(
                booking_id=booking_id,
                customer_days=customer_days,
                run_llm=run_llm,
                model=model,
                max_llm_messages=max_llm_messages,
                max_llm_text_chars=max_llm_text_chars,
                timeout_seconds=timeout_seconds,
                use_cache=use_cache,
                force_refresh=force_refresh,
            )
            for booking_id in capped_ids
        ]
        action_rows: list[dict[str, Any]] = []
        for review in reviews:
            action_rows.extend(review.get("action_rows") or [])
        action_rows.sort(key=lambda row: (-(row.get("priority_score") or 0), str(row.get("owner_team") or ""), int(row.get("booking_id") or 0)))
        return {
            "view": "booking_rating_batch",
            "context_version": BOOKING_REVIEW_CONTEXT_VERSION,
            "booking_ids": capped_ids,
            "reviewed_booking_count": len(reviews),
            "action_rows": action_rows,
            "reviews": reviews,
        }