from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional, Sequence

from .staff_activity_collectors import StaffActivityCollectorService
from .staff_activity_common import *
from .staff_activity_common import _date_window


class StaffActivityReviewService(StaffActivityCollectorService):

    @staticmethod
    def _rating_stats(rows: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
        rating_sum = 0.0
        rating_count = 0

        for row in rows or []:
            value = row.get(key)
            if value in (None, ""):
                continue

            try:
                rating = float(value)
            except Exception:
                continue

            # 0 means not rated in this business context.
            if rating <= 0:
                continue

            rating_sum += rating
            rating_count += 1

        return {
            "sum": round(rating_sum, 2),
            "count": rating_count,
            "avg": round(rating_sum / rating_count, 2) if rating_count else None,
        }
    @staticmethod
    def _staff_role_display(staff: dict[str, Any]) -> str:
        """User-facing role label built only from DB fields: is_admin + team."""
        account_type = account_type_display((staff or {}).get("is_admin"))
        team = clean_text((staff or {}).get("team"), 80)
        if team and lower_ref(team) != lower_ref(account_type):
            return f"{account_type} ({team})"
        return account_type

    def _public_staff(self, staff: dict[str, Any]) -> dict[str, Any]:
        """Safe staff profile for evidence/LLM views. Keep source ids only in raw mode."""
        assignment = staff.get("line_assignment") if isinstance(staff.get("line_assignment"), dict) else {}
        return compact_dict(
            {
                "username": staff.get("username"),
                "email": staff.get("email"),
                "phone": staff.get("normalized_phone") or show_phone(staff.get("phone_number")),
                "role": self._staff_role_display(staff),
                "team": staff.get("team"),
                "account_type": account_type_display(staff.get("is_admin")),
                "active": staff.get("active"),
                "resolved_from": staff.get("resolved_from"),
                "input_phone_is_pooled_line": staff.get("input_phone_is_pooled_line"),
                "pooled_line": compact_dict(
                    {
                        "phone": assignment.get("normalized_phone") or assignment.get("phone"),
                        "username": assignment.get("username"),
                        "team": assignment.get("team"),
                        "tag_to": assignment.get("tag_to"),
                    }
                )
                if assignment
                else None,
            }
        )

    @staticmethod
    def _canonical_role_from_staff_team(staff_team: Any) -> str:
        """Resolve the review scope from the DB staff team when possible."""
        team_text = lower_ref(staff_team)
        if not team_text:
            return "generic"

        direct_scope = normalize_role_scope("auto", team_text)
        if direct_scope != "generic":
            return direct_scope

        # Handles values like "Sales Team", "User (Sales)", "Ops - Bangalore", etc.
        for alias, canonical in ROLE_ALIASES.items():
            if alias in {"auto", "all", "generic", "staff", "admin"}:
                continue
            if alias and alias in team_text:
                return canonical

        return "generic"

    def _resolve_review_role_scope(
        self,
        *,
        staff: dict[str, Any],
        requested_role: Any = "auto",
    ) -> tuple[str, str, str]:
        """
        Resolve the effective review role.

        Rule:
        - If staging_user_account.team maps to a known role, use that role.
        - Otherwise fall back to the requested role.

        This prevents legacy routes such as /staff/caretaker-activity from
        forcing Caretaker for a staff member whose DB team is Sales/Finance/etc.
        """
        requested_role_scope = normalize_role_scope(requested_role, staff.get("team"))
        staff_team_scope = self._canonical_role_from_staff_team(staff.get("team"))

        if staff_team_scope != "generic":
            return staff_team_scope, requested_role_scope, "staging_user_account.team"

        return requested_role_scope, requested_role_scope, "request_role_fallback"

    def _timeline(
        self,
        *,
        calls: Sequence[dict[str, Any]],
        whatsapp: Sequence[dict[str, Any]],
        tickets: Sequence[dict[str, Any]],
        checkins: Sequence[dict[str, Any]],
        checkouts: Sequence[dict[str, Any]],
        site_visits: Sequence[dict[str, Any]],
        leads: Sequence[dict[str, Any]] = (),
        bookings: Sequence[dict[str, Any]] = (),
        travel_cart: Sequence[dict[str, Any]] = (),
        finance_rows: Sequence[dict[str, Any]] = (),
        property_marks_staff: Sequence[dict[str, Any]] = (),
        max_text: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        rows.extend(calls)
        rows.extend(whatsapp)

        for row in tickets:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("created_at") or row.get("close_date"),
                        "channel": "ticket",
                        "flow": "staff_activity",
                        "status": row.get("status"),
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Ticket {row.get('source_id') or ''} | {row.get('category') or ''} | {row.get('description') or ''} | feedback={row.get('ticket_feedback') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in checkins:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("checkin_date") or row.get("added_on"),
                        "channel": "checkin_feedback",
                        "flow": "customer_feedback",
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Check-in booking={row.get('booking_id') or ''} stay_rating={row.get('stay_rating') or ''} cleaning={row.get('cleaning_rating') or ''} comment={row.get('stay_comment') or row.get('suggestions') or row.get('other_comment') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in checkouts:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("checkout_date") or row.get("added_time"),
                        "channel": "checkout_feedback",
                        "flow": "customer_feedback",
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Checkout booking={row.get('booking_id') or ''} rms_rating={row.get('rms_rating') or ''} building_rating={row.get('building_rating') or ''} comment={row.get('stay_comment') or row.get('rms_comment') or row.get('suggestions') or row.get('other_comment') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in site_visits:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("site_visit_date") or row.get("added_on"),
                        "channel": "site_visit",
                        "flow": "field_activity",
                        "status": self._site_visit_status_label(row.get("schedule_status")),
                        "status_code": row.get("schedule_status"),
                        "activity_type": self._site_visit_activity_type(row),
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Site visit lead={row.get('lead_id') or ''} type={row.get('visit_type') or ''} activity={self._site_visit_activity_type(row)} status={self._site_visit_status_label(row.get('schedule_status'))}",
                            max_text,
                        ),
                    }
                )
            )

        for row in leads:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("created_at") or row.get("closed_at"),
                        "channel": "lead",
                        "flow": "staff_pipeline_activity",
                        "status": row.get("raw_status"),
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Lead {row.get('source_id') or ''} user={row.get('user_id') or ''} booking={row.get('booking_id') or ''} origin={row.get('origin') or ''} match={','.join(row.get('match_reasons') or [])}",
                            max_text,
                        ),
                    }
                )
            )

        for row in bookings:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("booking_datetime") or row.get("travel_from_date"),
                        "channel": "booking",
                        "flow": "staff_booking_activity",
                        "status": row.get("booking_status"),
                        "source_id": row.get("source_id") or row.get("booking_id"),
                        "text": clean_text(
                            f"Booking {row.get('booking_id') or row.get('source_id') or ''} lead={row.get('lead_id') or ''} status={row.get('booking_status') or ''} amount={row.get('total_amount') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in travel_cart:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("added_on"),
                        "channel": "travel_cart",
                        "flow": "booking_attempt",
                        "status": row.get("bkc_status"),
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Travel cart user={row.get('user_id') or ''} dates={row.get('travel_from_date') or ''}->{row.get('travel_to_date') or ''} amount={row.get('total_amount') or ''} pending={row.get('pending_amount') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in finance_rows:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("utr_added_on") or row.get("created_on") or row.get("send_time"),
                        "channel": "finance",
                        "flow": "staff_finance_activity",
                        "status": row.get("status") or row.get("amount_status"),
                        "source_id": row.get("source_id"),
                        "text": clean_text(
                            f"Invoice/payment booking={row.get('booking_id') or ''} payment={row.get('payment_id') or ''} status={row.get('status') or row.get('amount_status') or ''} amount={row.get('amount') or row.get('total_amount') or ''} pending={row.get('pending_balance') or ''}",
                            max_text,
                        ),
                    }
                )
            )

        for row in property_marks_staff:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("last_updated_on") or row.get("asset_verified_on") or row.get("flat_verified_on"),
                        "channel": "property_mark",
                        "flow": "staff_property_update",
                        "source_id": row.get("source_id") or row.get("prop_id"),
                        "text": clean_text(
                            f"Property mark match={','.join(row.get('match_reasons') or [])}",
                            max_text,
                        ),
                    }
                )
            )

        rows.sort(key=lambda row: coerce_datetime(row.get("time")) or datetime.min, reverse=True)
        return rows

    def _compact_timeline_rows(
        self,
        timeline: Sequence[dict[str, Any]],
        limit: int,
        max_text: int = 160,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in list(timeline)[:limit]:
            line = self._timeline_line(row)
            rows.append(
                compact_dict(
                    {
                        "time": row.get("time"),
                        "channel": row.get("channel"),
                        "flow": row.get("flow"),
                        "call_type": row.get("call_type"),
                        "status": row.get("status"),
                        "duration": fmt_duration(row.get("duration_sec")) if row.get("channel") == "call" else None,
                        "duration_sec": row.get("duration_sec") if row.get("channel") == "call" else None,
                        "source_id": row.get("source_id"),
                        "line": clean_text(line, max_text * 2),
                        "summary": clean_text(row.get("summary") or row.get("text"), max_text),
                    }
                )
            )
        return rows

    def _timeline_text(self, timeline: Sequence[dict[str, Any]], max_rows: int = 500) -> str:
        lines = []
        for row in timeline[:max_rows]:
            lines.append(self._timeline_line(row))
        return "\n".join(line for line in lines if line)

    def _activity_text(self, payload: dict[str, Any]) -> str:
        lines = ["Staff Activity Review"]
        staff = payload.get("staff") or {}
        counts = payload.get("counts") or {}
        lines.append(
            f"Staff: {staff.get('username') or 'unknown'} | role={self._staff_role_display(staff)} | team={staff.get('team')} | "
            f"role_scope={payload.get('role_display') or payload.get('role_scope')} | phone={staff.get('normalized_phone')}"
        )
        lines.append(f"Window: {(payload.get('window') or {}).get('label')}")

        for key in (
            "calls",
            "calls_connected",
            "calls_missed_or_zero_duration",
            "missed_calls_requiring_followup",
            "missed_calls_followed_up",
            "missed_calls_without_followup",
            "followup_rate_pct",
            "avg_followup_hours",
            "calls_external",
            "whatsapp_direct",
            "whatsapp_groups",
            "assigned_buildings",
            "vacant_properties",
            "own_tickets_closed_in_window",
            "tickets_rated",
            "tickets_unrated",
            "leads",
            "bookings",
            "success_bookings",
            "travel_cart_attempts",
            "finance_rows",
            "checkin_feedback",
            "checkin_feedback_total",
            "checkout_feedback",
            "checkout_feedback_total",
            "site_visits",
            "property_marks_by_staff",
        ):
            if key in counts and counts.get(key) not in (None, "", 0):
                lines.append(f"{key}: {counts.get(key, 0)}")

        timeline_text = ((payload.get("copy_blocks") or {}).get("timeline_text") or "").strip()
        if timeline_text:
            lines.append("")
            lines.append("Timeline")
            lines.append(timeline_text)

        return "\n".join(lines)

    def _site_visit_not_done_reason_counts(self, site_visits: Sequence[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "not_done_due_to_booking_full": 0,
            "not_done_with_connected_followup": 0,
            "not_done_missed_call_no_connected_followup": 0,
            "not_done_no_booking_no_call_activity": 0,
            "not_done_unknown_reason": 0,
        }

        for row in site_visits or []:
            try:
                status = int(row.get("schedule_status"))
            except Exception:
                status = None

            if status != 1:
                continue

            reason = str(row.get("not_done_reason_candidate") or "").strip()

            if reason == "NOT_DONE_PROPERTY_ALREADY_BOOKED_OR_FULL":
                counts["not_done_due_to_booking_full"] += 1
            elif reason == "NOT_DONE_BUT_CONNECTED_FOLLOWUP_FOUND":
                counts["not_done_with_connected_followup"] += 1
            elif reason == "NOT_DONE_MISSED_CALL_NO_CONNECTED_FOLLOWUP":
                counts["not_done_missed_call_no_connected_followup"] += 1
            elif reason == "NOT_DONE_NO_BOOKING_NO_CALL_ACTIVITY":
                counts["not_done_no_booking_no_call_activity"] += 1
            else:
                counts["not_done_unknown_reason"] += 1

        return counts

    @staticmethod
    def _site_visit_missed_call_source_counts(site_visits: Sequence[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "missed_call_no_followup_from_caretaker": 0,
            "missed_call_no_followup_from_customer": 0,
            "missed_call_no_followup_both_sides": 0,
            "missed_call_no_followup_customer_only": 0,
            "missed_call_no_followup_unknown_direction": 0,
        }

        for row in site_visits or []:
            try:
                status = int(row.get("schedule_status"))
            except Exception:
                status = None

            if status != 1:
                continue

            reason = str(row.get("not_done_reason_candidate") or "").strip()
            if reason != "NOT_DONE_MISSED_CALL_NO_CONNECTED_FOLLOWUP":
                continue

            caretaker_attempted = int(row.get("missed_calls_from_caretaker_near_visit") or 0) > 0
            customer_attempted = int(row.get("missed_calls_from_customer_near_visit") or 0) > 0
            unknown_attempted = int(row.get("missed_calls_unknown_direction_near_visit") or 0) > 0

            if caretaker_attempted:
                counts["missed_call_no_followup_from_caretaker"] += 1
            if customer_attempted:
                counts["missed_call_no_followup_from_customer"] += 1
            if caretaker_attempted and customer_attempted:
                counts["missed_call_no_followup_both_sides"] += 1
            if customer_attempted and not caretaker_attempted:
                counts["missed_call_no_followup_customer_only"] += 1
            if unknown_attempted and not caretaker_attempted and not customer_attempted:
                counts["missed_call_no_followup_unknown_direction"] += 1

        return counts

    @staticmethod
    def _pre_visit_call_stats(site_visits: Sequence[dict[str, Any]]) -> dict[str, Any]:
        done_site_visits = 0
        done_site_visits_with_pre_call = 0
        minutes_before_values: list[float] = []

        for row in site_visits or []:
            try:
                status = int(row.get("schedule_status"))
            except Exception:
                status = None

            if status != 0:
                continue

            done_site_visits += 1

            try:
                connected_pre_calls = int(row.get("pre_visit_connected_calls_same_day") or 0)
            except Exception:
                connected_pre_calls = 0

            if connected_pre_calls <= 0:
                continue

            done_site_visits_with_pre_call += 1

            try:
                minutes_before = float(row.get("pre_visit_call_minutes_before"))
            except Exception:
                minutes_before = None

            if minutes_before is not None and minutes_before >= 0:
                minutes_before_values.append(minutes_before)

        pre_call_coverage_pct = (
            round(done_site_visits_with_pre_call * 100.0 / done_site_visits, 2)
            if done_site_visits
            else None
        )
        avg_pre_call_minutes = (
            round(sum(minutes_before_values) / len(minutes_before_values), 2)
            if minutes_before_values
            else None
        )

        return {
            "done_site_visits": done_site_visits,
            "done_site_visits_with_pre_call": done_site_visits_with_pre_call,
            "done_site_visits_without_pre_call": max(0, done_site_visits - done_site_visits_with_pre_call),
            "pre_call_coverage_pct": pre_call_coverage_pct,
            "avg_pre_call_minutes_before_visit": avg_pre_call_minutes,
        }

    @staticmethod
    def _call_counterparty_key(row: dict[str, Any]) -> str:
        for key in (
            "counterparty_phone",
            "customer_phone",
            "customer_phone_number",
            "cx_number",
            "peer_pn",
            "phone",
            "mobile",
            "lead_id",
        ):
            value = row.get(key)
            if value in (None, ""):
                continue

            digits = "".join(ch for ch in str(value) if ch.isdigit())
            if len(digits) >= 10:
                return digits[-10:]

            text = str(value).strip().lower()
            if text:
                return text

        return ""

    @staticmethod
    def _call_time(row: dict[str, Any]) -> datetime | None:
        for key in ("activity_time", "call_datetime", "created_at", "time", "date"):
            parsed = coerce_datetime(row.get(key))
            if parsed:
                return parsed
        return None

    def _missed_call_followup_stats(self, calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """
        Pair every missed/zero-duration call with the next connected call to the
        same counterparty. This keeps the metric customer-specific instead of
        letting an unrelated connected call hide a missed follow-up.
        """
        grouped: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}

        for row in calls or []:
            if not isinstance(row, dict):
                continue

            when = self._call_time(row)
            counterparty = self._call_counterparty_key(row)
            if not when or not counterparty:
                continue

            grouped.setdefault(counterparty, []).append((when, row))

        missed_required = 0
        followed_up = 0
        missing_followup = 0
        delays_minutes: list[float] = []

        for rows in grouped.values():
            rows.sort(key=lambda item: item[0])
            connected_after: list[datetime] = [
                when
                for when, row in rows
                if int(row.get("duration_sec") or 0) > 0
            ]

            for missed_time, row in rows:
                if int(row.get("duration_sec") or 0) > 0:
                    continue

                missed_required += 1
                next_connected = next((when for when in connected_after if when > missed_time), None)

                if next_connected:
                    followed_up += 1
                    delays_minutes.append((next_connected - missed_time).total_seconds() / 60.0)
                else:
                    missing_followup += 1

        avg_minutes = round(sum(delays_minutes) / len(delays_minutes), 2) if delays_minutes else None
        sorted_delays = sorted(delays_minutes)
        midpoint = len(sorted_delays) // 2
        if not sorted_delays:
            median_minutes = None
        elif len(sorted_delays) % 2:
            median_minutes = round(sorted_delays[midpoint], 2)
        else:
            median_minutes = round((sorted_delays[midpoint - 1] + sorted_delays[midpoint]) / 2.0, 2)

        return {
            "missed_calls_requiring_followup": missed_required,
            "missed_calls_followed_up": followed_up,
            "missed_calls_without_followup": missing_followup,
            "followup_rate_pct": round(followed_up * 100.0 / missed_required, 2) if missed_required else None,
            "avg_followup_minutes": avg_minutes,
            "avg_followup_hours": round(avg_minutes / 60.0, 2) if avg_minutes is not None else None,
            "median_followup_minutes": median_minutes,
        }

    @staticmethod
    def _has_valid_ticket_rating(row: dict[str, Any], key: str = "ticket_rating") -> bool:
        value = row.get(key)
        if value in (None, ""):
            return False

        try:
            rating = float(value)
        except Exception:
            return False

        # Ticket ratings should be real customer ratings.
        # Treat 0 / negative / invalid values as not rated.
        return rating > 0

    def _count_ticket_ratings(self, rows: Sequence[dict[str, Any]], key: str = "ticket_rating") -> int:
        return sum(1 for row in rows or [] if self._has_valid_ticket_rating(row, key))


    @staticmethod
    def _parse_ticket_dt(value: Any) -> datetime | None:
        if value in (None, ""):
            return None

        if isinstance(value, datetime):
            return value

        parsed = coerce_datetime(value)
        if parsed:
            return parsed

        text = str(value).strip().replace("T", " ")

        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:26] if "." in text else text[:19], fmt)
            except Exception:
                continue

        return None

    @staticmethod
    def _median_number(values: Sequence[float]) -> float | None:
        cleaned = sorted(float(value) for value in values if value is not None)
        if not cleaned:
            return None

        n = len(cleaned)
        mid = n // 2

        if n % 2:
            return round(cleaned[mid], 2)

        return round((cleaned[mid - 1] + cleaned[mid]) / 2, 2)

    def _ticket_resolution_stats(self, tickets: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """
        Calculates ticket resolution speed from staging_user_ticket timestamps.

        Business meaning:
        - created_at = ticket opening time
        - close_date = ticket closing/resolution time
        - Only rows with close_date >= created_at are included in resolution speed.
        - Open tickets are counted separately as age, not resolution time.
        """
        resolution_hours: list[float] = []
        open_age_hours: list[float] = []

        now = datetime.utcnow()

        for row in tickets or []:
            if not isinstance(row, dict):
                continue

            opened_at = self._parse_ticket_dt(
                row.get("created_at")
                or row.get("ticket_opening_time")
                or row.get("opened_at")
                or row.get("added_on")
            )

            closed_at = self._parse_ticket_dt(
                row.get("close_date")
                or row.get("ticket_closing_time")
                or row.get("closed_at")
                or row.get("resolved_at")
            )

            if opened_at and closed_at and closed_at >= opened_at:
                resolution_hours.append((closed_at - opened_at).total_seconds() / 3600)

            elif opened_at and not closed_at:
                age_hours = (now - opened_at).total_seconds() / 3600
                if age_hours >= 0:
                    open_age_hours.append(age_hours)

        resolved_count = len(resolution_hours)
        open_age_count = len(open_age_hours)

        return {
            "ticket_resolution_count": resolved_count,
            "avg_ticket_resolution_hours": round(sum(resolution_hours) / resolved_count, 2)
            if resolved_count
            else None,
            "median_ticket_resolution_hours": self._median_number(resolution_hours),
            "min_ticket_resolution_hours": round(min(resolution_hours), 2) if resolution_hours else None,
            "max_ticket_resolution_hours": round(max(resolution_hours), 2) if resolution_hours else None,

            "tickets_resolved_within_2h": sum(1 for hours in resolution_hours if hours <= 2),
            "tickets_resolved_2_to_24h": sum(1 for hours in resolution_hours if 2 < hours <= 24),
            "tickets_resolved_1_to_2d": sum(1 for hours in resolution_hours if 24 < hours <= 48),
            "tickets_resolved_after_2d": sum(1 for hours in resolution_hours if hours > 48),

            "open_ticket_age_count": open_age_count,
            "avg_open_ticket_age_hours": round(sum(open_age_hours) / open_age_count, 2)
            if open_age_count
            else None,
        }

    @staticmethod
    def _distinct_booking_count(rows: Sequence[dict[str, Any]]) -> int:
        booking_ids: set[str] = set()
        fallback_count = 0

        for row in rows or []:
            booking_id = row.get("booking_id")
            if booking_id not in (None, ""):
                booking_ids.add(str(booking_id).strip())
            else:
                fallback_count += 1

        return len(booking_ids) + fallback_count

    def _feedback_due_booking_counts(
        self,
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict[str, int]:
        """
        Returns total eligible check-ins/check-outs for assigned caretaker properties.

        Business meaning:
        - checkin_feedback_total = successful bookings for assigned properties
          whose travel_from_date falls inside the review window.
        - checkout_feedback_total = successful bookings for assigned properties
          whose travel_to_date falls inside the review window.
        """
        table_name = "staging_booking_confirm"
        if not self.table_exists(table_name):
            return {"checkin_total": 0, "checkout_total": 0}

        columns = self.table_columns(table_name)

        prop_column = next(
            (
                col
                for col in ("prop_id", "booking_prop_id", "property_id", "propid")
                if col in columns
            ),
            None,
        )
        if not prop_column:
            return {"checkin_total": 0, "checkout_total": 0}

        prop_ids = {
            str(row.get("prop_id") or row.get("source_id") or row.get("property_id") or row.get("propid"))
            for row in properties or []
            if row.get("prop_id") or row.get("source_id") or row.get("property_id") or row.get("propid")
        }
        if not prop_ids:
            return {"checkin_total": 0, "checkout_total": 0}

        if "booking_id" in columns and "source_id" in columns:
            booking_key_expr = "COALESCE(NULLIF(bc.booking_id::text, ''), bc.source_id::text)"
        elif "booking_id" in columns:
            booking_key_expr = "bc.booking_id::text"
        elif "source_id" in columns:
            booking_key_expr = "bc.source_id::text"
        else:
            booking_key_expr = (
                f"bc.{prop_column}::text || ':' || "
                "COALESCE(bc.travel_from_date::text, '') || ':' || "
                "COALESCE(bc.travel_to_date::text, '')"
            )

        checkin_case = (
            f"""
            CASE
                WHEN bc.travel_from_date >= :start_dt
                 AND bc.travel_from_date < :end_dt
                THEN {booking_key_expr}
            END
            """
            if "travel_from_date" in columns
            else "NULL"
        )

        checkout_case = (
            f"""
            CASE
                WHEN bc.travel_to_date >= :start_dt
                 AND bc.travel_to_date < :end_dt
                THEN {booking_key_expr}
            END
            """
            if "travel_to_date" in columns
            else "NULL"
        )

        date_filters: list[str] = []
        if "travel_from_date" in columns:
            date_filters.append("(bc.travel_from_date >= :start_dt AND bc.travel_from_date < :end_dt)")
        if "travel_to_date" in columns:
            date_filters.append("(bc.travel_to_date >= :start_dt AND bc.travel_to_date < :end_dt)")
        if not date_filters:
            return {"checkin_total": 0, "checkout_total": 0}

        status_filter = ""
        if "booking_status" in columns:
            status_filter = "AND LOWER(TRIM(COALESCE(bc.booking_status::text, ''))) = 'success'"
        elif "status" in columns:
            status_filter = "AND LOWER(TRIM(COALESCE(bc.status::text, ''))) = 'success'"

        in_sql, in_params = build_in_params(sorted(prop_ids), "fbprop")
        params: dict[str, Any] = {
            "start_dt": start_dt,
            "end_dt": end_dt,
            **in_params,
        }

        rows = self.rows(
            f"""
            SELECT
                COUNT(DISTINCT {checkin_case}) AS checkin_total,
                COUNT(DISTINCT {checkout_case}) AS checkout_total
            FROM {table_ref(self.schema, table_name)} bc
            WHERE bc.{prop_column}::text IN {in_sql}
              {status_filter}
              AND ({" OR ".join(date_filters)})
            """,
            params,
        )

        first = rows[0] if rows else {}
        return {
            "checkin_total": int(first.get("checkin_total") or 0),
            "checkout_total": int(first.get("checkout_total") or 0),
        }

    def _counts(
        self,
        *,
        buildings: Sequence[dict[str, Any]],
        properties: Sequence[dict[str, Any]],
        start_dt: datetime,
        end_dt: datetime,
        tickets: Sequence[dict[str, Any]],
        checkins: Sequence[dict[str, Any]],
        checkouts: Sequence[dict[str, Any]],
        site_visits: Sequence[dict[str, Any]],
        calls: Sequence[dict[str, Any]],
        whatsapp: Sequence[dict[str, Any]],
        office_numbers: Sequence[dict[str, Any]],
        leads: Sequence[dict[str, Any]] = (),
        bookings: Sequence[dict[str, Any]] = (),
        travel_cart: Sequence[dict[str, Any]] = (),
        finance_rows: Sequence[dict[str, Any]] = (),
        property_marks_staff: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        occupied = sum(1 for row in properties if row.get("occupancy_status") == "occupied")
        vacant = len(self._vacant_properties(properties))
        availability_inventory = self._availability_inventory_properties(properties)
        availability_quality_followup = self._availability_quality_followup_properties(properties)
        non_occupied_not_available = len(availability_inventory)
        upcoming = sum(1 for row in properties if row.get("occupancy_status") == "upcoming_booking")

        internal_calls = sum(1 for row in calls if row.get("call_type") == "internal")
        external_calls = sum(1 for row in calls if row.get("call_type") != "internal")
        call_followup_stats = self._missed_call_followup_stats(calls)

        site_visits_actual, site_visits_system = self._site_visit_counts(site_visits)
        site_visit_reason_counts = self._site_visit_not_done_reason_counts(site_visits)
        missed_call_source_counts = self._site_visit_missed_call_source_counts(site_visits)
        pre_visit_call_stats = self._pre_visit_call_stats(site_visits)

        ticket_rating_stats = self._rating_stats(tickets, "ticket_rating")
        ticket_resolution_stats = self._ticket_resolution_stats(tickets)

        tickets_rated = int(ticket_rating_stats["count"])
        tickets_unrated = max(0, len(tickets) - tickets_rated)
        ticket_rating_coverage_pct = round(tickets_rated * 100.0 / len(tickets), 2) if tickets else None

        feedback_due_counts = self._feedback_due_booking_counts(properties, start_dt, end_dt)

        checkin_feedback_received = self._distinct_booking_count(checkins)
        checkout_feedback_received = self._distinct_booking_count(checkouts)

        checkin_feedback_total = int(feedback_due_counts.get("checkin_total") or 0)
        checkout_feedback_total = int(feedback_due_counts.get("checkout_total") or 0)

        checkin_feedback_missing = max(0, checkin_feedback_total - checkin_feedback_received)
        checkout_feedback_missing = max(0, checkout_feedback_total - checkout_feedback_received)

        checkin_feedback_coverage_pct = (
            round(checkin_feedback_received * 100.0 / checkin_feedback_total, 2)
            if checkin_feedback_total
            else None
        )
        checkout_feedback_coverage_pct = (
            round(checkout_feedback_received * 100.0 / checkout_feedback_total, 2)
            if checkout_feedback_total
            else None
        )

        total_events = (
            len(calls)
            + len(whatsapp)
            + len(tickets)
            + len(checkins)
            + len(checkouts)
            + len(site_visits)
            + len(leads)
            + len(bookings)
            + len(travel_cart)
            + len(finance_rows)
            + len(property_marks_staff)
        )
        return {
            "events": total_events,
            "assigned_buildings": len(buildings),
            "assigned_properties": len(properties),
            "occupied_properties": occupied,
            "vacant_properties": vacant,
            "upcoming_bookings": upcoming,
            "non_occupied_not_currently_available": non_occupied_not_available,
            "availability_date_current_occupancy_inventory": len(availability_inventory),
            "availability_quality_followup_candidates": len(availability_quality_followup),

            "tickets_closed_in_window": len(tickets),
            "own_tickets_closed_in_window": len(tickets),
            "tickets": len(tickets),
            "tickets_total": len(tickets),
            "closed_tickets": len(tickets),
            "tickets_closed": len(tickets),
            "open_tickets": 0,
            "tickets_open": 0,
            "tickets_reopened": sum(
                1
                for row in tickets
                if str(row.get("reopen_flag") or "").strip() not in {"", "0", "false", "False"}
            ),

            "tickets_rated": tickets_rated,
            "tickets_unrated": tickets_unrated,
            "ticket_rating_sum": ticket_rating_stats["sum"],
            "ticket_rating_count": ticket_rating_stats["count"],
            "ticket_rating_coverage_pct": ticket_rating_coverage_pct,
            "avg_ticket_rating": ticket_rating_stats["avg"],

            # Ticket resolution speed
            "ticket_resolution_count": ticket_resolution_stats["ticket_resolution_count"],
            "avg_ticket_resolution_hours": ticket_resolution_stats["avg_ticket_resolution_hours"],
            "median_ticket_resolution_hours": ticket_resolution_stats["median_ticket_resolution_hours"],
            "min_ticket_resolution_hours": ticket_resolution_stats["min_ticket_resolution_hours"],
            "max_ticket_resolution_hours": ticket_resolution_stats["max_ticket_resolution_hours"],
            "tickets_resolved_within_2h": ticket_resolution_stats["tickets_resolved_within_2h"],
            "tickets_resolved_2_to_24h": ticket_resolution_stats["tickets_resolved_2_to_24h"],
            "tickets_resolved_1_to_2d": ticket_resolution_stats["tickets_resolved_1_to_2d"],
            "tickets_resolved_after_2d": ticket_resolution_stats["tickets_resolved_after_2d"],
            "open_ticket_age_count": ticket_resolution_stats["open_ticket_age_count"],
            "avg_open_ticket_age_hours": ticket_resolution_stats["avg_open_ticket_age_hours"],

            "checkin_feedback": checkin_feedback_received,
            "checkout_feedback": checkout_feedback_received,

            "checkin_feedback_received": checkin_feedback_received,
            "checkin_feedback_total": checkin_feedback_total,
            "checkin_feedback_missing": checkin_feedback_missing,
            "checkin_feedback_coverage_pct": checkin_feedback_coverage_pct,

            "checkout_feedback_received": checkout_feedback_received,
            "checkout_feedback_total": checkout_feedback_total,
            "checkout_feedback_missing": checkout_feedback_missing,
            "checkout_feedback_coverage_pct": checkout_feedback_coverage_pct,

            "site_visits": len(site_visits),
            "site_visits_actual": site_visits_actual,
            "site_visits_system": site_visits_system,
            "done_site_visits_with_pre_call": pre_visit_call_stats["done_site_visits_with_pre_call"],
            "done_site_visits_without_pre_call": pre_visit_call_stats["done_site_visits_without_pre_call"],
            "pre_visit_call_coverage_pct": pre_visit_call_stats["pre_call_coverage_pct"],
            "avg_pre_visit_call_minutes_before_visit": pre_visit_call_stats["avg_pre_call_minutes_before_visit"],

            # Neutral / explainable
            "site_visits_not_done_due_to_booking_full": site_visit_reason_counts["not_done_due_to_booking_full"],
            "site_visits_not_done_with_connected_followup": site_visit_reason_counts["not_done_with_connected_followup"],

            # Negative / rating-impacting
            "site_visits_not_done_missed_call_no_connected_followup": site_visit_reason_counts["not_done_missed_call_no_connected_followup"],
            "site_visits_missed_call_no_followup_from_caretaker": missed_call_source_counts["missed_call_no_followup_from_caretaker"],
            "site_visits_missed_call_no_followup_from_customer": missed_call_source_counts["missed_call_no_followup_from_customer"],
            "site_visits_missed_call_no_followup_both_sides": missed_call_source_counts["missed_call_no_followup_both_sides"],
            "site_visits_missed_call_no_followup_customer_only": missed_call_source_counts["missed_call_no_followup_customer_only"],
            "site_visits_missed_call_no_followup_unknown_direction": missed_call_source_counts["missed_call_no_followup_unknown_direction"],
            "site_visits_not_done_no_booking_no_call_activity": site_visit_reason_counts["not_done_no_booking_no_call_activity"],

            # Data-quality fallback only
            "site_visits_not_done_unknown_reason": site_visit_reason_counts["not_done_unknown_reason"],

            "calls": len(calls),
            "calls_internal": internal_calls,
            "calls_external": external_calls,
            "calls_connected": sum(1 for row in calls if int(row.get("duration_sec") or 0) > 0),
            "calls_missed_or_zero_duration": sum(1 for row in calls if int(row.get("duration_sec") or 0) <= 0),
            "missed_calls_requiring_followup": call_followup_stats["missed_calls_requiring_followup"],
            "missed_calls_followed_up": call_followup_stats["missed_calls_followed_up"],
            "missed_calls_without_followup": call_followup_stats["missed_calls_without_followup"],
            "followup_rate_pct": call_followup_stats["followup_rate_pct"],
            "avg_followup_minutes": call_followup_stats["avg_followup_minutes"],
            "avg_followup_hours": call_followup_stats["avg_followup_hours"],
            "median_followup_minutes": call_followup_stats["median_followup_minutes"],
            "calls_unknown_direction": sum(
                1
                for row in calls
                if row.get("direction_known") is False or "direction_unknown" in str(row.get("flow") or "")
            ),
            "call_talk_time_sec": sum(int(row.get("duration_sec") or 0) for row in calls),

            "whatsapp": len(whatsapp),
            "whatsapp_direct": sum(1 for row in whatsapp if row.get("conversation_kind") == "direct"),
            "whatsapp_groups": sum(1 for row in whatsapp if row.get("conversation_kind") == "group"),
            "office_numbers_used": len(office_numbers),

            "leads": len(leads),
            "leads_open_or_active": sum(
                1
                for row in leads
                if lower_ref(row.get("raw_status")) not in {"closed", "cancelled", "canceled", "lost", "converted"}
            ),
            "bookings": len(bookings),
            "success_bookings": sum(1 for row in bookings if lower_ref(row.get("booking_status")) == "success"),
            "travel_cart_attempts": len(travel_cart),

            "finance_rows": len(finance_rows),
            "finance_pending_rows": sum(
                1
                for row in finance_rows
                if str(row.get("pending_balance") or "").strip() not in {"", "0", "0.0", "0.00"}
            ),

            "property_marks_by_staff": len(property_marks_staff),

            "avg_checkin_stay_rating": avg_numeric(checkins, "stay_rating"),
            "avg_checkin_cleaning_rating": avg_numeric(checkins, "cleaning_rating"),
            "avg_checkout_rms_rating": avg_numeric(checkouts, "rms_rating"),
            "avg_checkout_building_rating": avg_numeric(checkouts, "building_rating"),
        }

    @staticmethod
    def _public_counts(counts: dict[str, Any]) -> dict[str, Any]:
        """Public count payload: no detailed property/occupancy inventory noise."""
        hidden_keys = {
            "assigned_properties",
            "occupied_properties",
            "upcoming_bookings",
            "non_occupied_not_currently_available",
            "availability_date_current_occupancy_inventory",
            "availability_quality_followup_candidates",
        }
        return {key: value for key, value in (counts or {}).items() if key not in hidden_keys}

    def _summary_cards(self, counts: dict[str, Any], role_scope: str = "generic") -> list[dict[str, Any]]:
        cards = [
            {"label": "Review scope", "value": role_display_name(role_scope)},
            {
                "label": "Calls",
                "value": (
                    f"{counts.get('calls', 0)} total, "
                    f"{counts.get('calls_connected', 0)} connected, "
                    f"{counts.get('calls_missed_or_zero_duration', 0)} missed, "
                    f"{counts.get('calls_internal', 0)} internal, "
                    f"{counts.get('calls_external', 0)} external, "
                    f"{fmt_duration(counts.get('call_talk_time_sec', 0))} talk time"
                ),
            },
            {
                "label": "WhatsApp",
                "value": f"{counts.get('whatsapp_direct', 0)} direct, {counts.get('whatsapp_groups', 0)} group",
            },
        ]

        if counts.get("own_tickets_closed_in_window"):
            rated = counts.get("tickets_rated", 0)
            total = counts.get("tickets_total", 0)
            avg = counts.get("avg_ticket_rating")
            suffix = f", rated {rated}/{total}"
            if avg not in (None, ""):
                suffix += f", avg rating {avg}"
            cards.append(
                {
                    "label": "Own closed tickets",
                    "value": f"{counts.get('own_tickets_closed_in_window', counts.get('tickets_closed_in_window', 0))}{suffix}",
                }
            )

        if counts.get("missed_calls_requiring_followup"):
            cards.append(
                {
                    "label": "Missed-call follow-up",
                    "value": (
                        f"{counts.get('missed_calls_followed_up', 0)}"
                        f"/{counts.get('missed_calls_requiring_followup', 0)} recovered, "
                        f"avg {counts.get('avg_followup_hours') or '-'}h, "
                        f"{counts.get('followup_rate_pct') or 0}%"
                    ),
                }
            )

        if counts.get("assigned_buildings") or counts.get("vacant_properties"):
            cards.extend(
                [
                    {"label": "Assigned buildings", "value": counts.get("assigned_buildings", 0)},
                    {"label": "Vacant properties", "value": counts.get("vacant_properties", 0)},
                ]
            )

        if counts.get("leads") or counts.get("bookings") or counts.get("travel_cart_attempts"):
            cards.extend(
                [
                    {
                        "label": "Leads",
                        "value": f"{counts.get('leads', 0)} total, {counts.get('leads_open_or_active', 0)} open/active",
                    },
                    {
                        "label": "Bookings",
                        "value": f"{counts.get('success_bookings', 0)} success of {counts.get('bookings', 0)}",
                    },
                    {"label": "Travel cart", "value": counts.get("travel_cart_attempts", 0)},
                ]
            )

        if counts.get("finance_rows"):
            cards.append(
                {
                    "label": "Finance rows",
                    "value": f"{counts.get('finance_rows', 0)} rows, {counts.get('finance_pending_rows', 0)} pending rows",
                }
            )

        if counts.get("checkin_feedback_total") or counts.get("checkout_feedback_total") or counts.get("checkin_feedback") or counts.get("checkout_feedback"):
            cards.append(
                {
                    "label": "Check-in / checkout feedback",
                    "value": (
                        f"Check-in {counts.get('checkin_feedback_received', counts.get('checkin_feedback', 0))}"
                        f"/{counts.get('checkin_feedback_total', counts.get('checkin_feedback', 0))}; "
                        f"Checkout {counts.get('checkout_feedback_received', counts.get('checkout_feedback', 0))}"
                        f"/{counts.get('checkout_feedback_total', counts.get('checkout_feedback', 0))}"
                    ),
                }
            )

        if counts.get("site_visits"):
            cards.append(
                {
                    "label": "Site visits",
                    "value": f"{counts.get('site_visits_actual', 0)} done, {counts.get('site_visits_system', 0)} scheduled/not done",
                }
            )

        if counts.get("property_marks_by_staff"):
            cards.append({"label": "Property marks by staff", "value": counts.get("property_marks_by_staff", 0)})

        return [compact_dict(card) for card in cards]

    def _compact_lead_rows(self, rows_in: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("created_at") or row.get("activity_time"),
                        "lead_id": row.get("source_id"),
                        "user_id": row.get("user_id"),
                        "booking_id": row.get("booking_id"),
                        "status": row.get("raw_status"),
                        "origin": row.get("origin"),
                        "priority": row.get("priority"),
                        "assigned_to": row.get("assigned_to") or row.get("executive_id"),
                        "generated_by": row.get("generated_by"),
                        "match": row.get("match_reasons"),
                        "phone": row.get("customer_phone"),
                    }
                )
            )
        return rows

    def _compact_booking_rows(self, rows_in: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("booking_datetime") or row.get("activity_time"),
                        "booking_id": row.get("booking_id") or row.get("source_id"),
                        "lead_id": row.get("lead_id"),
                        "user_id": row.get("user_id"),
                        "status": row.get("booking_status"),
                        "type": row.get("booking_type"),
                        "source": row.get("booking_source") or row.get("txn_source"),
                        "stay": f"{row.get('travel_from_date') or ''} -> {row.get('travel_to_date') or ''}".strip(" ->"),
                        "amount": row.get("total_amount"),
                        "created_by": row.get("created_by"),
                    }
                )
            )
        return rows

    def _compact_travel_cart_rows(self, rows_in: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("added_on") or row.get("activity_time"),
                        "travel_id": row.get("source_id"),
                        "user_id": row.get("user_id"),
                        "stay": f"{row.get('travel_from_date') or ''} -> {row.get('travel_to_date') or ''}".strip(" ->"),
                        "nights": row.get("nights"),
                        "type": row.get("booking_type"),
                        "total": row.get("total_amount"),
                        "advance": row.get("advance_amount"),
                        "pending": row.get("pending_amount"),
                        "source": row.get("source"),
                        "bkc_status": row.get("bkc_status"),
                    }
                )
            )
        return rows

    def _compact_finance_rows(
        self,
        rows_in: Sequence[dict[str, Any]],
        limit: int,
        max_text: int = 160,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("utr_added_on") or row.get("created_on") or row.get("send_time") or row.get("activity_time"),
                        "invoice_id": row.get("source_id"),
                        "booking_id": row.get("booking_id"),
                        "payment_id": row.get("payment_id"),
                        "status": row.get("status") or row.get("amount_status"),
                        "payment_mode": row.get("payment_mode"),
                        "amount": row.get("amount") or row.get("total_amount"),
                        "pending": row.get("pending_balance"),
                        "utr_no": row.get("utr_no"),
                        "utr_added_by": row.get("utr_added_by"),
                        "comment": clean_text(row.get("comment"), max_text),
                    }
                )
            )
        return rows

    def _compact_property_mark_staff_rows(self, rows_in: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        rows = []
        for row in list(rows_in)[:limit]:
            rows.append(
                compact_dict(
                    {
                        "time": row.get("activity_time") or row.get("last_updated_on") or row.get("asset_verified_on") or row.get("flat_verified_on"),
                        "checkout_marked": boolish(row.get("mark_check_out")),
                        "electricity_bill_marked": boolish(row.get("mark_electricity_bill")),
                        "rent_paid_marked": boolish(row.get("mark_rent_paid")),
                        "asset_verified": row.get("asset_verified"),
                        "flat_verified": row.get("flat_verified"),
                        "match": row.get("match_reasons"),
                    }
                )
            )
        return rows

    def _sections(
        self,
        *,
        staff: dict[str, Any],
        role_scope: str,
        role_display: str,
        role_focus: Sequence[str],
        buildings: Sequence[dict[str, Any]],
        properties: Sequence[dict[str, Any]],
        tickets: Sequence[dict[str, Any]],
        checkins: Sequence[dict[str, Any]],
        checkouts: Sequence[dict[str, Any]],
        site_visits: Sequence[dict[str, Any]],
        calls: Sequence[dict[str, Any]],
        whatsapp: Sequence[dict[str, Any]],
        office_numbers: Sequence[dict[str, Any]],
        leads: Sequence[dict[str, Any]] = (),
        bookings: Sequence[dict[str, Any]] = (),
        travel_cart: Sequence[dict[str, Any]] = (),
        finance_rows: Sequence[dict[str, Any]] = (),
        property_marks_staff: Sequence[dict[str, Any]] = (),
        print_limit: int,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, int(print_limit or 50))
        call_summary_rows = self._call_summary_by_counterparty(calls, min(safe_limit, 20))
        communication_rows = self._compact_communication_rows(calls, whatsapp, safe_limit)

        sections = [
            {
                "title": "Staff profile",
                "items": [
                    {"label": "Name", "value": staff.get("username")},
                    {"label": "Email", "value": staff.get("email")},
                    {"label": "Phone", "value": staff.get("normalized_phone")},
                    {"label": "Role", "value": self._staff_role_display(staff)},
                    {"label": "Team", "value": staff.get("team")},
                    {"label": "Account type", "value": account_type_display(staff.get("is_admin"))},
                    {"label": "Review role scope", "value": role_display},
                    {"label": "Active", "value": staff.get("active")},
                    {"label": "Last login", "value": staff.get("last_login_time")},
                ],
            },
            {"title": "Role-specific review focus", "rows": [{"line": item} for item in role_focus]},
            {"title": "Office numbers used", "rows": list(office_numbers)},
            {"title": "Call summary by counterparty", "rows": call_summary_rows},
            {"title": "Communication", "rows": communication_rows},
        ]

        if buildings:
            sections.append({"title": "Assigned buildings", "rows": self._compact_building_rows(buildings, safe_limit)})

        if properties:
            vacant_rows = self._compact_property_rows(properties, safe_limit)
            if vacant_rows:
                sections.append({"title": "Vacant properties", "rows": vacant_rows})

        if tickets:
            sections.append({"title": "Own closed ticket updates", "rows": self._compact_ticket_rows(tickets, safe_limit)})

        if leads:
            title = "Sales / lead activity" if role_scope == "sales" else "Lead activity"
            if role_scope == "marketing":
                title = "Marketing lead generation / handoff"
            sections.append({"title": title, "rows": self._compact_lead_rows(leads, safe_limit)})

        if bookings:
            sections.append({"title": "Booking activity", "rows": self._compact_booking_rows(bookings, safe_limit)})

        if travel_cart:
            sections.append({"title": "Travel cart / booking attempts", "rows": self._compact_travel_cart_rows(travel_cart, safe_limit)})

        if finance_rows:
            sections.append({"title": "Finance rows touched by staff", "rows": self._compact_finance_rows(finance_rows, safe_limit)})

        if checkins:
            sections.append({"title": "Check-in feedback", "rows": self._compact_checkin_rows(checkins, safe_limit)})

        if checkouts:
            sections.append({"title": "Checkout feedback", "rows": self._compact_checkout_rows(checkouts, safe_limit)})

        if site_visits:
            sections.append({"title": "Site visits / scheduled visits", "rows": self._compact_site_visit_rows(site_visits, safe_limit)})

        if property_marks_staff:
            sections.append(
                {
                    "title": "Property / asset marks by this staff member",
                    "rows": self._compact_property_mark_staff_rows(property_marks_staff, safe_limit),
                }
            )

        compact_sections = []
        for section in sections:
            if section.get("items") or section.get("rows"):
                compact_sections.append(compact_dict(section))

        return compact_sections

    def compact_for_llm(self, payload: dict[str, Any], *, print_limit: int = 50) -> dict[str, Any]:
        data = payload.get("data") or {}
        safe_limit = max(1, int(print_limit or 50))
        role_scope = str(payload.get("role_scope") or "generic")

        return compact_dict(
            {
                "context_version": "staff_activity_llm:v4_role_scoped",
                "input": payload.get("input"),
                "window": payload.get("window"),
                "role_scope": role_scope,
                "role_display": payload.get("role_display") or role_display_name(role_scope),
                "role_focus": payload.get("role_focus"),
                "staff": self._public_staff(payload.get("staff") or {}),
                "counts": payload.get("counts"),
                "office_numbers_used": data.get("office_numbers_used"),
                "own_closed_tickets_in_window": self._compact_ticket_rows(data.get("tickets") or [], safe_limit),
                "call_summary_by_counterparty": self._call_summary_by_counterparty(data.get("calls") or [], min(safe_limit, 20)),
                "communication": self._compact_communication_rows(data.get("calls") or [], data.get("whatsapp") or [], safe_limit),
                "assigned_buildings": data.get("assigned_buildings") or self._compact_building_rows(data.get("buildings") or [], safe_limit),
                "vacant_properties": data.get("vacant_properties") or self._compact_property_rows(data.get("properties") or [], safe_limit),
                "leads": self._compact_lead_rows(data.get("leads") or [], safe_limit),
                "bookings": self._compact_booking_rows(data.get("bookings") or [], safe_limit),
                "travel_cart": self._compact_travel_cart_rows(data.get("travel_cart") or [], safe_limit),
                "finance_rows": self._compact_finance_rows(data.get("finance_rows") or [], safe_limit),
                "checkin_feedback": self._compact_checkin_rows(data.get("checkins") or [], safe_limit),
                "checkout_feedback": self._compact_checkout_rows(data.get("checkouts") or [], safe_limit),
                "site_visits": self._compact_site_visit_rows(data.get("site_visits") or [], safe_limit),
                "property_marks_by_staff": self._compact_property_mark_staff_rows(data.get("property_marks_by_staff") or [], safe_limit),
                "timeline": self._compact_timeline_rows(payload.get("timeline") or [], safe_limit),
            }
        )

    def build_analysis_prompt(self, payload: dict[str, Any], *, print_limit: int = 50) -> str:
        llm_context = self.compact_for_llm(payload, print_limit=print_limit)
        data_text = json.dumps(llm_context, ensure_ascii=False, indent=2, default=str)
        staff = payload.get("staff") or {}
        role_display = payload.get("role_display") or role_display_name(str(payload.get("role_scope") or "generic"), staff.get("team"))
        focus_lines = "\n".join(f"- {item}" for item in (payload.get("role_focus") or []))

        return f"""Below is the staff activity evidence from AnalyticsEngine for {role_display}: {staff.get('username') or 'unknown'}.

Please analyse this as an individual staff/admin activity review for role scope: {role_display}.

Important guardrails:
- Use only the evidence below; do not invent facts.
- Common evidence for every role is only calls and WhatsApp from the staff/user number or assigned pooled line.
- For calls, prefer call-log executive_id/executive_name as the actor. sales_phone is only the line used; MobileTagging/staging_staff_phone_assignment is line metadata and fallback only when actor is missing.
- Do not judge this staff member on role-specific sections that are missing or not applicable.
- If a source/table appears missing or empty, explicitly say that the evidence is unavailable rather than assuming no work happened.
- Building/property context is intentionally compact: assigned buildings show only BuildName and BuildingId; vacant properties show only property_name and vacant_since.
- Do not infer missing work from a missing role-specific section. Treat absent sections as not visible in provided data.

Role-specific focus:
{focus_lines or '- Generic staff activity quality and communication.'}

Review checklist:
1. Overall work coverage for the selected role scope.
2. Communication: who called whom, internal vs external calls, office numbers used, WhatsApp direct/group messages, missed/zero-duration calls, and communication gaps.
3. Role-specific evidence only: tickets/support for support-like roles; leads/bookings/travel cart for Sales/Marketing/Onboarding; invoice/payment rows for Finance; field feedback/site visits/property marks for Caretaker/Ops/Technical where present.
4. Assigned building table and vacant property table are context only; do not expand them into unrelated property-quality claims.
5. What was handled correctly, what needs immediate improvement, and what should be followed up.
6. Give a practical rating out of 10 with clear evidence-based reasons.
7. Suggest exact next actions for the manager.

COMPACT STAFF ACTIVITY DATA:
{data_text}
""".strip()

    def build_staff_activity(
        self,
        *,
        username: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        role: str = "auto",
        days: int = 3,
        limit: int = 10000,
        print_limit: int = 50,
        max_text: int = 160,
        llm: bool = True,
        display_mode: str = "evidence",
    ) -> dict[str, Any]:
        start_dt, end_dt, window_label = _date_window(days)
        staff = self.resolve_staff(username=username, email=email, phone=phone)

        role_scope, requested_role_scope, role_resolution_source = self._resolve_review_role_scope(
            staff=staff,
            requested_role=role,
        )

        config = ROLE_CONFIG.get(role_scope) or ROLE_CONFIG["generic"]
        role_display = role_display_name(role_scope, staff.get("team"))
        role_focus = list(config.get("focus") or [])
        metric_packs = set(config.get("metric_packs") or [])

        buildings = self.collect_assigned_buildings(staff, role_scope)
        properties = self.collect_assigned_properties(buildings) if buildings else []

        # Common evidence for every role: only calls and WhatsApp from the staff
        # actor or assigned pooled line. Tickets and all other evidence are
        # role-specific below.
        calls = self.collect_calls(staff, start_dt, end_dt, limit, max_text)
        whatsapp = self.collect_whatsapp(staff, start_dt, end_dt, limit, max_text)

        ticket_role_scopes = {"caretaker", "ops_team", "technical", "finance", "support", "onboarding"}
        tickets = (
            self.collect_tickets(staff, [], [], start_dt, end_dt, limit, include_scope=False)
            if ("tickets" in metric_packs or role_scope in ticket_role_scopes)
            else []
        )

        # Role-specific packs. Each collector is narrow and evidence-based.
        leads = (
            self.collect_leads(staff, role_scope, start_dt, end_dt, limit)
            if metric_packs.intersection({"sales_pipeline", "marketing_leads", "onboarding"})
            else []
        )
        bookings = (
            self.collect_bookings(staff, leads, role_scope, start_dt, end_dt, limit)
            if metric_packs.intersection({"bookings", "sales_pipeline", "marketing_leads", "onboarding"})
            else []
        )
        travel_cart = (
            self.collect_travel_cart_for_leads(leads, start_dt, end_dt, limit)
            if metric_packs.intersection({"travel_cart", "sales_pipeline", "marketing_leads"})
            else []
        )
        finance_rows = self.collect_finance_rows(staff, start_dt, end_dt, limit) if "finance_actions" in metric_packs else []

        checkins = self.collect_checkin_feedback(staff, properties, start_dt, end_dt, limit) if "field_feedback" in metric_packs else []
        checkouts = self.collect_checkout_feedback(staff, properties, start_dt, end_dt, limit) if "field_feedback" in metric_packs else []

        include_site_scope = role_scope in {"caretaker", "ops_team"}
        site_visits = (
            self.collect_site_visits(
                staff,
                buildings,
                properties,
                start_dt,
                end_dt,
                limit,
                include_scope=include_site_scope,
            )
            if metric_packs.intersection({"site_visits", "sales_pipeline", "onboarding"})
            else []
        )

        property_marks_staff = self.collect_property_marks_by_staff(staff, start_dt, end_dt, limit) if "property_marks" in metric_packs else []

        # Standardize property/unit display for all user-facing evidence.
        # Keep prop_id/source_id in raw rows for joins/debug, but compact views use
        # staging_property_unit.unit_name through `property` / `unit_name`.
        self.enrich_rows_with_unit_names(tickets, prop_keys=("prop_id",))
        self.enrich_rows_with_unit_names(checkins, prop_keys=("prop_id",), booking_keys=("booking_id",))
        self.enrich_rows_with_unit_names(checkouts, prop_keys=("booking_prop_id",), booking_keys=("booking_id",))
        self.enrich_rows_with_unit_names(site_visits, prop_keys=("prop_id",))
        self.enrich_rows_with_unit_names(bookings, prop_keys=("prop_id",), booking_keys=("booking_id", "source_id"))
        self.enrich_rows_with_unit_names(travel_cart, prop_keys=("prop_id",))
        self.enrich_rows_with_unit_names(finance_rows, booking_keys=("booking_id",))
        self.enrich_rows_with_unit_names(property_marks_staff, prop_keys=("prop_id", "source_id"))

        office_numbers = self._office_numbers(calls, whatsapp)

        timeline = self._timeline(
            calls=calls,
            whatsapp=whatsapp,
            tickets=tickets,
            checkins=checkins,
            checkouts=checkouts,
            site_visits=site_visits,
            leads=leads,
            bookings=bookings,
            travel_cart=travel_cart,
            finance_rows=finance_rows,
            property_marks_staff=property_marks_staff,
            max_text=max_text,
        )

        counts = self._counts(
            buildings=buildings,
            properties=properties,
            start_dt=start_dt,
            end_dt=end_dt,
            tickets=tickets,
            checkins=checkins,
            checkouts=checkouts,
            site_visits=site_visits,
            calls=calls,
            whatsapp=whatsapp,
            office_numbers=office_numbers,
            leads=leads,
            bookings=bookings,
            travel_cart=travel_cart,
            finance_rows=finance_rows,
            property_marks_staff=property_marks_staff,
        )

        public_counts = self._public_counts(counts)
        window = {"start": start_dt, "end": end_dt, "label": window_label}

        full_payload = compact_dict(
            {
                "view": "raw",
                "context_version": "staff_activity:v4",
                "input": {
                    "username": username,
                    "email": email,
                    "phone": phone or staff.get("normalized_phone") or staff.get("phone_number"),
                    "role": role_display,
                    "requested_role": role,
                    "requested_role_scope": requested_role_scope,
                    "resolved_role_scope": role_scope,
                    "role_resolution_source": role_resolution_source,
                    "staff_team": staff.get("team"),
                    "days": days,
                    "schema": self.schema,
                },
                "source_scope": "staff_profile_plus_common_calls_whatsapp_plus_role_specific_evidence",
                "role_scope": role_scope,
                "role_display": role_display,
                "role_focus": role_focus,
                "metric_packs": sorted(metric_packs),
                "window": window,
                "staff": self._public_staff(staff),
                "counts": public_counts,
                "summary_cards": self._summary_cards(public_counts, role_scope),
                "timeline": self._compact_timeline_rows(timeline, print_limit or 50),
                "data": {
                    "office_numbers_used": office_numbers,
                    "assigned_buildings": self._compact_building_rows(buildings, print_limit or 50),
                    "vacant_properties": self._compact_property_rows(properties, print_limit or 50),
                    "tickets": tickets,
                    "leads": leads,
                    "bookings": bookings,
                    "travel_cart": travel_cart,
                    "finance_rows": finance_rows,
                    "checkins": checkins,
                    "checkouts": checkouts,
                    "site_visits": site_visits,
                    "property_marks_by_staff": property_marks_staff,
                    "calls": calls,
                    "whatsapp": whatsapp,
                },
            }
        )

        timeline_text = self._timeline_text(timeline[:print_limit] if print_limit else timeline)
        full_payload["copy_blocks"] = {"timeline_text": timeline_text}
        full_payload["copy_blocks"]["staff_activity_text"] = self._activity_text(full_payload)

        llm_context = self.compact_for_llm(full_payload, print_limit=print_limit or 50)
        llm_prompt = self.build_analysis_prompt(full_payload, print_limit=print_limit or 50) if llm or display_mode == "llm" else None

        normalized_mode = str(display_mode or "evidence").strip().lower()

        if normalized_mode == "raw":
            if llm_prompt:
                full_payload["llm_context"] = llm_context
                full_payload["llm_prompt"] = llm_prompt
            return full_payload

        if normalized_mode == "llm":
            return compact_dict(
                {
                    "view": "llm",
                    "context_version": "staff_activity_llm:v4",
                    "role_scope": role_scope,
                    "role_display": role_display,
                    "staff": self._public_staff(staff),
                    "window": window,
                    "counts": public_counts,
                    "llm_context": llm_context,
                    "llm_prompt": llm_prompt or self.build_analysis_prompt(full_payload, print_limit=print_limit or 50),
                }
            )

        evidence = compact_dict(
            {
                "view": "evidence",
                "title": f"Staff Activity Review - {role_display}",
                "context_version": "staff_activity_evidence:v4",
                "username": staff.get("username"),
                "role_scope": role_scope,
                "role_display": role_display,
                "staff": self._public_staff(staff),
                "window": window,
                "source_scope": full_payload.get("source_scope"),
                "counts": public_counts,
                "summary_cards": self._summary_cards(public_counts, role_scope),
                "sections": self._sections(
                    staff=staff,
                    role_scope=role_scope,
                    role_display=role_display,
                    role_focus=role_focus,
                    buildings=buildings,
                    properties=properties,
                    tickets=tickets,
                    checkins=checkins,
                    checkouts=checkouts,
                    site_visits=site_visits,
                    calls=calls,
                    whatsapp=whatsapp,
                    office_numbers=office_numbers,
                    leads=leads,
                    bookings=bookings,
                    travel_cart=travel_cart,
                    finance_rows=finance_rows,
                    property_marks_staff=property_marks_staff,
                    print_limit=print_limit or 50,
                    start_dt=start_dt,
                    end_dt=end_dt,
                ),
                "timeline": self._compact_timeline_rows(timeline, print_limit or 50),
                "row_count": len(timeline),
                "copy_blocks": full_payload.get("copy_blocks"),
            }
        )

        if llm_prompt:
            evidence["llm_context"] = llm_context
            evidence["llm_prompt"] = llm_prompt

        return evidence

    def build_caretaker_activity(
        self,
        *,
        username: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        days: int = 3,
        limit: int = 10000,
        print_limit: int = 50,
        max_text: int = 160,
        llm: bool = True,
        display_mode: str = "evidence",
    ) -> dict[str, Any]:
        return self.build_staff_activity(
            username=username,
            email=email,
            phone=phone,
            role="auto",
            days=days,
            limit=limit,
            print_limit=print_limit,
            max_text=max_text,
            llm=llm,
            display_mode=display_mode,
        )


__all__ = ["StaffActivityReviewService"]
