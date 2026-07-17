from __future__ import annotations

import re
from typing import Any

from .common import compact_dict


ACTIVE_TICKET_STATUSES_FOR_POLICY = {
    "open",
    "in progress",
    "in_progress",
    "reopen",
    "reopened",
    "pending",
    "assigned",
    "new",
}

TICKET_POLICY: dict[str, Any] = {
    "category_rules": [
        # ----------------------------------------------------------------
        # Finance
        # ----------------------------------------------------------------
        {
            "owner_team": "Finance",
            "subteam": "Finance",
            "keywords": [
                "e-bill payment",
                "ebill payment",
                "finance related",
                "deposit refund",
                "rent collection",
                "vendor payment",
                "fuel expense",
                "refund",
                "invoice",
                "overdue",
                "over due",
                "pending payment",
            ],
        },
        # ----------------------------------------------------------------
        # Operations / Desk
        # ----------------------------------------------------------------
        {
            "owner_team": "Operations",
            "subteam": "Desk",
            "keywords": [
                "agreement id",
                "proof signature",
                "agreement",
                "aggrement",
                "agrrement",
                "rental agreement",
                "kyc",
                "e-kyc",
                "ekyc",
                "vacate",
                "vacate extend",
                "vacate/extend",
                "extend",
                "extension",
                "extension request",
                "duplicate key",
                "coupon code discount",
                "coupon code",
                "coupon",
                "move in feedback",
                "move-in feedback",
                "movein feedback",
                "move out feedback",
                "move-out feedback",
                "moveout feedback",
                "checkout notice",
                "notice feedback",
                "stay feedback",
                "customer feedback",
                "feedback",
            ],
        },
        # ----------------------------------------------------------------
        # Operations / Asset
        # ----------------------------------------------------------------
        {
            "owner_team": "Operations",
            "subteam": "Asset",
            "keywords": [
                "asset tracking",
                "furnish flat",
                "furniture movement",
                "furniture issue",
                "furniture",
            ],
        },
        # ----------------------------------------------------------------
        # Operations / Field
        # ----------------------------------------------------------------
        {
            "owner_team": "Operations",
            "subteam": "Field",
            "keywords": [
                "cable issue",
                "cable recharge",
                "check-out damages",
                "checkout damages",
                "ct utilization",
                "flat inspection",
                "gas cylinder refill",
                "gas cylinder",
                "install item",
                "uninstall item",
                "lift/power backup",
                "lift/powerbackup",
                "new flat setup",
                "new setbox connection",
                "setbox",
                "office work",
                "other issues",
                "others issue",
                "common area cleaning",
                "cleaning",
                "plumbing issue",
                "pest control",
                "electrical issue",
                "electricity",
                "lift",
                "powerbackup",
                "power backup",
                "water issue",
                "wifi issue",
                "wi-fi",
                "internet",
                "electronics issue",
                "washingmachine issue",
                "washing machine",
                "fridge issue",
                "house keeping",
                "housekeeping",
                "paid housekeeping service",
                "fridge",
                "geyser",
                "ac",
                "fan",
                "tv",
                "carpenter issue",
                "property readiness",
                "maintenance",
                "repair",
                "unit issue",
                "painting issue",
            ],
        },
        # ----------------------------------------------------------------
        # Caretaker 
        # ----------------------------------------------------------------
        {
            "owner_team": "Caretaker",
            "subteam": "Caretaker",
            "keywords": [
                "carpenter issue",
                "electrical issue",
                "electronics issue",
                "house keeping",
                "housekeeping",
                "paid housekeeping service",
                "painting issue",
                "pest control",
                "plumbing issue",
                "water issue",
                "washing machine issue",
                "washingmachine issue",
                "wifi issue",
                "wi-fi issue",
                "painting",
            ],
        },
    ],
    "groups": {
        "Operations": ["Desk", "Field", "Asset"],
        "Finance": ["Finance"],
        "Caretaker": ["Caretaker"],
        "Unclassified": ["Unclassified"],
    },
}

def policy_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _ticket_id(ticket: dict[str, Any]) -> Any:
    return ticket.get("ticket_id") or ticket.get("source_id") or ticket.get("id")


def _ticket_policy_text(ticket: dict[str, Any]) -> str:
    parts = [
        ticket.get("category"),
        ticket.get("sub_category"),
        ticket.get("issue_type"),
        ticket.get("type"),
        ticket.get("subject"),
        ticket.get("title"),
        ticket.get("description"),
        ticket.get("text"),
    ]
    return " ".join(str(part or "") for part in parts if part not in (None, ""))


def _is_open_ticket(ticket: dict[str, Any]) -> bool:
    status = str(ticket.get("status") or "").strip().lower()
    return status in ACTIVE_TICKET_STATUSES_FOR_POLICY


def _matches_keyword(text: Any, keywords: list[str]) -> bool:
    text_key = policy_key(text)
    if not text_key:
        return False

    for keyword in keywords or []:
        keyword_key = policy_key(keyword)
        if keyword_key and keyword_key in text_key:
            return True

    return False

def _category_rule_match(policy_text: str) -> tuple[str, str] | None:
    for rule in TICKET_POLICY.get("category_rules") or []:
        if _matches_keyword(policy_text, rule.get("keywords") or []):
            return (
                str(rule.get("owner_team") or "Unclassified"),
                str(rule.get("subteam") or "Unclassified"),
            )
    return None

def resolve_ticket_policy(ticket: dict[str, Any]) -> dict[str, Any]:
    """Classify ticket from category/text only.

    Source of truth:
    - staging_user_ticket.source_id = ticket id
    - staging_user_ticket.category/text = owner team + subteam
    - staging_user_ticket.team = creator/team metadata only, not ownership
    """
    policy_text = _ticket_policy_text(ticket)
    is_open = _is_open_ticket(ticket)

    matched = _category_rule_match(policy_text)
    if matched:
        owner_team, subteam = matched
        source = "category_policy"
    else:
        owner_team, subteam = "Unclassified", "Unclassified"
        source = "category_policy_unclassified"

    result: dict[str, Any] = {
        "ticket_id": _ticket_id(ticket),
        "ticket_owner_team": owner_team,
        "ticket_subteam": subteam,
        "ticket_creator_team": ticket.get("team"),
        "team_policy_source": source,
    }

    if owner_team == "Operations":
        result["ops_subteam"] = subteam
        result["desk_pending"] = bool(is_open and subteam == "Desk")
        result["field_pending"] = bool(is_open and subteam == "Field")
        result["asset_pending"] = bool(is_open and subteam == "Asset")

    if owner_team == "Finance":
        result["finance_pending"] = bool(is_open)

    if owner_team == "Caretaker":
        result["caretaker_pending"] = bool(is_open)

    if owner_team == "Unclassified" or subteam == "Unclassified":
        result["policy_confidence"] = "low"
        result["needs_policy_review"] = True

    return compact_dict(result)

def _empty_group() -> dict[str, Any]:
    return {
        "open_tickets": [],
        "closed_tickets": [],
        "open_count": 0,
        "closed_count": 0,
    }


def _initial_ticket_groups() -> dict[str, Any]:
    groups: dict[str, Any] = {}

    for owner_team, subteams in (TICKET_POLICY.get("groups") or {}).items():
        groups[owner_team] = {}
        for subteam in subteams or ["Unclassified"]:
            groups[owner_team][subteam] = _empty_group()

    groups.setdefault("Unclassified", {})
    groups["Unclassified"].setdefault("Unclassified", _empty_group())

    return groups


def _ticket_group_item(ticket: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "ticket_id": _ticket_id(ticket),
            "category": ticket.get("category"),
            "status": ticket.get("status"),
            "priority": ticket.get("priority"),
            "created_at": ticket.get("created_at"),
            "close_date": ticket.get("close_date"),
            "active_days": ticket.get("active_days") or ticket.get("age_days"),
            "description": ticket.get("description") or ticket.get("text") or ticket.get("evidence"),
            "assigned_to": ticket.get("assigned_to"),
            "resolved_by": ticket.get("resolved_by"),
            "closed_by": ticket.get("closed_by"),
            "ticket_owner_team": ticket.get("ticket_owner_team"),
            "ticket_subteam": ticket.get("ticket_subteam"),
            "ops_subteam": ticket.get("ops_subteam"),
            "needs_policy_review": ticket.get("needs_policy_review"),
        }
    )


def build_ticket_groups(support: dict[str, Any]) -> dict[str, Any]:
    groups = _initial_ticket_groups()
    seen: set[str] = set()

    for source_key in ("open_tickets", "closed_tickets", "tickets"):
        for ticket in support.get(source_key) or []:
            if not isinstance(ticket, dict):
                continue

            ticket_id = _ticket_id(ticket)
            dedupe_key = str(ticket_id) if ticket_id not in (None, "") else "|".join(
                str(ticket.get(key) or "")
                for key in ("team", "category", "status", "created_at", "description", "text")
            )

            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            owner_team = str(ticket.get("ticket_owner_team") or "Unclassified").strip() or "Unclassified"
            subteam = str(ticket.get("ticket_subteam") or ticket.get("ops_subteam") or owner_team).strip() or "Unclassified"

            if owner_team not in groups:
                groups[owner_team] = {}
            if subteam not in groups[owner_team]:
                groups[owner_team][subteam] = _empty_group()

            bucket = "open_tickets" if _is_open_ticket(ticket) else "closed_tickets"
            groups[owner_team][subteam][bucket].append(_ticket_group_item(ticket))

    for owner_group in groups.values():
        if not isinstance(owner_group, dict):
            continue

        for group in owner_group.values():
            if not isinstance(group, dict):
                continue

            group["open_count"] = len(group.get("open_tickets") or [])
            group["closed_count"] = len(group.get("closed_tickets") or [])

    return compact_dict(groups)


def enrich_support_with_team_policy(support: dict[str, Any]) -> dict[str, Any]:
    out = dict(support or {})

    for source_key in ("open_tickets", "closed_tickets", "tickets"):
        enriched_rows = []

        for ticket in out.get(source_key) or []:
            if not isinstance(ticket, dict):
                continue

            row = dict(ticket)
            row.update(resolve_ticket_policy(row))
            enriched_rows.append(compact_dict(row))

        if enriched_rows:
            out[source_key] = enriched_rows

    ticket_groups = build_ticket_groups(out)
    out["ticket_groups"] = ticket_groups
    out["ops_subteam_ticket_groups"] = ticket_groups.get("Operations") or {}

    return compact_dict(out)


def team_policy_context(scope: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "policy_source": "config_driven_category_ticket_policy",
            "ticket_id_source": "staging_user_ticket.source_id",
            "owner_source": "staging_user_ticket.category/text",
            "note": "staging_user_ticket.team is creator/team metadata only, not ownership.",
            "rule": (
                "Ticket owner team and subteam are normalized from ticket category/text before LLM scoring. "
                "Do not use staging_user_ticket.team to decide ownership because that column represents who created the ticket. "
                "Use support.ticket_groups and support.ops_subteam_ticket_groups as source of truth. "
                "Operations has three subteams: Desk, Field, and Asset. "
                "Desk handles agreements, vacate/extend, feedback, coupon codes. "
                "Field handles maintenance, installation, cable, gas, lift, and on-site work. "
                "Asset handles asset tracking, furnish flat, furniture movement, and furniture issues. "
                "Sales does not resolve tickets; do not score Sales on ticket quality. "
                "Do not reassign ticket owner/subteam inside the prompt. "
                "Calls, WhatsApp and email are communication evidence for the overall booking review, "
                "not the base for Desk/Field/Asset subteam scoring."
            ),
            "ticket_policy": TICKET_POLICY,
            "booking_scope": {
                "ops_owner": scope.get("ops_owner"),
                "ops_manager": scope.get("ops_manager"),
                "caretaker": scope.get("caretaker"),
                "sales_owner": scope.get("sales_owner"),
                "finance_owner": scope.get("finance_owner"),
                "finance_manager": scope.get("finance_manager"),
            },
        }
    )

def enrich_conversation_with_team_policy(
    conversation: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Legacy compatibility only.

    Conversation should not decide ticket ownership/subteam scoring.
    """
    return compact_dict(dict(conversation or {}))