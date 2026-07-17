import json
import logging
import re
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.utils.whatsapp_utils import normalize_number

import os
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError

from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp-payment-dashboard"])


def _only_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _parse_dt(value, end_of_day=False):
    if not value:
        return None

    value = value.strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        if end_of_day:
            return datetime.fromisoformat(value + "T23:59:59")
        return datetime.fromisoformat(value + "T00:00:00")

    return datetime.fromisoformat(value.replace("Z", "+00:00"))




RMS_BOOKING_API_URL = os.getenv(
    "RMS_BOOKING_API_URL",
    "http://www.rentmystay.com/T/search_booking_by_contact"
)

RMS_BOOKING_API_TOKEN = os.getenv("RMS_BOOKING_API_TOKEN")



@router.get("/payment-metadata/dashboard")
async def get_payment_metadata_dashboard(
    admin_number: Optional[str] = Query(None),
    phone: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    utr: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    only_incomplete: bool = Query(False),
    include_text: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):

    try:
        offset = (page - 1) * limit

        normalized_admin = normalize_number(admin_number) if admin_number else None
        admin_digits = _only_digits(normalized_admin) if normalized_admin else None
        admin_last10 = admin_digits[-10:] if admin_digits and len(admin_digits) >= 10 else admin_digits

        normalized_phone = normalize_number(phone) if phone else None
        phone_digits = _only_digits(normalized_phone) if normalized_phone else None
        phone_last10 = phone_digits[-10:] if phone_digits and len(phone_digits) >= 10 else phone_digits

        start_dt = _parse_dt(from_date) if from_date else None
        end_dt = _parse_dt(to_date, end_of_day=True) if to_date else None

        sql = """
            WITH payment_rows AS (
                SELECT
                    id,
                    message_id,
                    admin_number,
                    cx_number,
                    peer_pn,
                    participant,
                    remote_jid,
                    r2_media_url,
                    ocr_status,
                    extracted_text,
                    image_type,
                    COALESCE(payment_metadata, '{}'::jsonb) AS payment_metadata,
                    timestamp,

                    regexp_replace(coalesce(peer_pn, ''), '[^0-9]', '', 'g') AS peer_digits,
                    regexp_replace(coalesce(cx_number, ''), '[^0-9]', '', 'g') AS cx_digits,
                    regexp_replace(coalesce(admin_number, ''), '[^0-9]', '', 'g') AS admin_digits,

                    CASE
                        WHEN COALESCE(payment_metadata, '{}'::jsonb)->>'amount' ~ '^[0-9]+(\\.[0-9]+)?$'
                        THEN (COALESCE(payment_metadata, '{}'::jsonb)->>'amount')::numeric
                        ELSE 0
                    END AS amount_value
                FROM public.messages
                WHERE image_type = 'payment_receipt'
            )
            SELECT
                id,
                message_id,
                admin_number,
                cx_number,
                peer_pn,
                participant,
                remote_jid,
                r2_media_url,
                ocr_status,
                extracted_text,
                image_type,
                payment_metadata,
                timestamp,
                amount_value,

                CASE
                    -- Use peer_pn only when it is a valid phone number
                    -- and it is NOT same as the WhatsApp account/admin number.
                    WHEN peer_digits <> ''
                     AND length(peer_digits) BETWEEN 10 AND 13
                     AND right(peer_digits, 10) <> right(admin_digits, 10)
                    THEN peer_pn

                    -- If peer_pn is missing/self/admin, use cx_number only when cx_number
                    -- looks like a real customer phone number, not group/chat id.
                    WHEN cx_digits <> ''
                     AND length(cx_digits) BETWEEN 10 AND 13
                     AND right(cx_digits, 10) <> right(admin_digits, 10)
                     AND coalesce(cx_number, '') NOT ILIKE '%@g.us%'
                     AND coalesce(cx_number, '') NOT ILIKE '%g.us%'
                     AND coalesce(cx_number, '') NOT LIKE '%-%'
                    THEN cx_number

                    ELSE NULL
                END AS resolved_customer_number,

                CASE
                    WHEN peer_digits <> ''
                     AND length(peer_digits) BETWEEN 10 AND 13
                     AND right(peer_digits, 10) <> right(admin_digits, 10)
                    THEN 'peer_pn'

                    WHEN cx_digits <> ''
                     AND length(cx_digits) BETWEEN 10 AND 13
                     AND right(cx_digits, 10) <> right(admin_digits, 10)
                     AND coalesce(cx_number, '') NOT ILIKE '%@g.us%'
                     AND coalesce(cx_number, '') NOT ILIKE '%g.us%'
                     AND coalesce(cx_number, '') NOT LIKE '%-%'
                    THEN 'cx_number'

                    WHEN peer_digits <> ''
                     AND right(peer_digits, 10) = right(admin_digits, 10)
                    THEN 'peer_pn_self_admin'

                    WHEN cx_digits <> ''
                     AND right(cx_digits, 10) = right(admin_digits, 10)
                    THEN 'cx_self_admin'

                    ELSE 'unknown'
                END AS matched_by,

                COUNT(*) OVER() AS total_count,
                SUM(amount_value) OVER() AS total_amount
            FROM payment_rows
            WHERE 1 = 1
        """

        params = {
            "limit": limit,
            "offset": offset,
        }

        if admin_digits:
            sql += """
                AND (
                    admin_digits = :admin_digits
                    OR right(admin_digits, 10) = :admin_last10
                )
            """
            params["admin_digits"] = admin_digits
            params["admin_last10"] = admin_last10

        if phone_digits:
            sql += """
                AND (
                    (
                        peer_digits <> ''
                        AND length(peer_digits) BETWEEN 10 AND 13
                        AND right(peer_digits, 10) <> right(admin_digits, 10)
                        AND (
                            peer_digits = :phone_digits
                            OR right(peer_digits, 10) = :phone_last10
                        )
                    )

                    OR

                    (
                        cx_digits <> ''
                        AND length(cx_digits) BETWEEN 10 AND 13
                        AND right(cx_digits, 10) <> right(admin_digits, 10)
                        AND (
                            cx_digits = :phone_digits
                            OR right(cx_digits, 10) = :phone_last10
                        )
                        AND coalesce(cx_number, '') NOT ILIKE '%@g.us%'
                        AND coalesce(cx_number, '') NOT ILIKE '%g.us%'
                        AND coalesce(cx_number, '') NOT LIKE '%-%'
                    )
                )
            """
            params["phone_digits"] = phone_digits
            params["phone_last10"] = phone_last10


        if utr:
            sql += """
                AND (
                    payment_metadata->>'utr' ILIKE :utr_like
                    OR payment_metadata->>'rrn' ILIKE :utr_like
                    OR payment_metadata->>'transaction_id' ILIKE :utr_like
                    OR payment_metadata->>'txn_id' ILIKE :utr_like
                    OR payment_metadata->>'transaction_reference' ILIKE :utr_like
                    OR COALESCE(extracted_text, '') ILIKE :utr_like
                )
            """
            params["utr_like"] = f"%{utr.strip()}%"


        if email:
            sql += """
                AND (
                    lower(COALESCE(payment_metadata->>'booking_contact_email', '')) LIKE :email_like
                    OR lower(COALESCE(payment_metadata->>'contact_email', '')) LIKE :email_like
                )
            """
            params["email_like"] = f"%{email.strip().lower()}%"

        


        if start_dt:
            sql += " AND timestamp >= :start_dt"
            params["start_dt"] = start_dt

        if end_dt:
            sql += " AND timestamp <= :end_dt"
            params["end_dt"] = end_dt

        if status:
            sql += " AND lower(payment_metadata->>'status') = :status"
            params["status"] = status.lower().strip()

        if only_incomplete:
            sql += """
                AND (
                    payment_metadata = '{}'::jsonb
                    OR payment_metadata->>'amount' IS NULL
                    OR payment_metadata->>'amount' = ''
                    OR payment_metadata->>'status' IS NULL
                    OR payment_metadata->>'status' = ''
                    OR payment_metadata->>'transaction_datetime' IS NULL
                    OR payment_metadata->>'transaction_datetime' = ''
                )
            """

        sql += """
            ORDER BY timestamp DESC NULLS LAST, id DESC
            LIMIT :limit
            OFFSET :offset
        """    

        rows = db.execute(text(sql), params).fetchall()

        data = []
        total_count = 0
        total_amount = 0.0
        amount_count = 0
        incomplete_count = 0

        for row in rows:
            metadata = row.payment_metadata or {}

            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}

            amount = metadata.get("amount")
            transaction_datetime = metadata.get("transaction_datetime")
            payment_status = metadata.get("status")

            is_incomplete = (
                amount in [None, ""]
                or payment_status in [None, ""]
                or transaction_datetime in [None, ""]
            )

            if amount not in [None, ""]:
                amount_count += 1

            if is_incomplete:
                incomplete_count += 1

            total_count = int(row.total_count or 0)
            total_amount = float(row.total_amount or 0)

            item = {
                "id": row.id,
                "message_id": row.message_id,
                "admin_number": row.admin_number,
                "customer_number": row.resolved_customer_number,
                "matched_by": row.matched_by,
                "cx_number": row.cx_number,
                "peer_pn": row.peer_pn,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "r2_media_url": row.r2_media_url,
                "ocr_status": row.ocr_status,
                "image_type": row.image_type,
                "payment_metadata": metadata,
                "is_incomplete": is_incomplete,

                "bookings": (
                    [{
                        "booking_id": metadata.get("booking_id"),
                        "contact_email": metadata.get("booking_contact_email") or metadata.get("contact_email"),
                        "contact_type": metadata.get("booking_contact_type"),
                    }]
                    if (metadata.get("booking_id") or metadata.get("booking_contact_email") or metadata.get("contact_email"))
                    else []
                ),    
            }

            


            if include_text:
                item["extracted_text"] = row.extracted_text

            data.append(item)

        return {
            "success": True,
            "page": page,
            "limit": limit,
            "count": len(data),
            "total_count": total_count,
            "total_amount": total_amount,
            "amount_count": amount_count,
            "incomplete_count_on_page": incomplete_count,
            "filters": {
                "admin_number": normalized_admin,
                "phone": normalized_phone,
                "from_date": from_date,
                "to_date": to_date,
                "status": status,
                "only_incomplete": only_incomplete,
            },
            "data": data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching payment metadata dashboard: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch payment metadata dashboard")




RMS_UTR_DETAILS_API_URL = os.getenv(
    "RMS_UTR_DETAILS_API_URL",
    "https://www.rentmystay.com/T/utr_details"
)

RMS_UTR_API_TOKEN = os.getenv("RMS_UTR_API_TOKEN", RMS_BOOKING_API_TOKEN or "")

try:
    RMS_UTR_TIMEOUT_SECONDS = max(
        2.0,
        float(os.getenv("RMS_UTR_TIMEOUT_SECONDS", "5"))
    )
except Exception:
    RMS_UTR_TIMEOUT_SECONDS = 5.0



def _parse_bool(value):
    if isinstance(value, bool):
        return value

    text_value = str(value or "").strip().lower()

    if text_value in {"true", "1", "yes", "y"}:
        return True

    if text_value in {"false", "0", "no", "n"}:
        return False

    return None


def _extract_utr_candidate(payment_metadata, extracted_text=""):
    metadata = payment_metadata or {}

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    keys = [
        "utr",
        "rrn",
        "transaction_reference",
        "transaction_id",
        "txn_id",
    ]

    for key in keys:
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue

        digits = _only_digits(value)

        if len(digits) >= 8:
            return digits

        return value

    text_value = str(extracted_text or "")

    patterns = [
        r"utr\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
        r"rrn\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
        r"reference\s*number\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
        r"reference\s*no\.?\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
        r"upi\s*ref\s*no\.?\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text_value, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            digits = _only_digits(value)
            return digits if len(digits) >= 8 else value

    return ""


def _call_rms_utr_details_api(utr_no: str):
    utr_value = _only_digits(utr_no) or str(utr_no or "").strip()

    if not utr_value:
        return {
            "ok": False,
            "utr_no": "",
            "is_present": None,
            "message": "Missing UTR",
        }

    query = urlencode({"utr_no": utr_value})
    url = f"{RMS_UTR_DETAILS_API_URL}?{query}"

    headers = {
        "Accept": "application/json",
    }

    if RMS_UTR_API_TOKEN:
        headers["Authorization"] = RMS_UTR_API_TOKEN

    request = UrlRequest(
        url,
        headers=headers,
        method="GET",
    )

    try:
        #with urlopen(request, timeout=12) as response:
        with urlopen(request, timeout=RMS_UTR_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = response.getcode()

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "utr_no": utr_value,
            "is_present": None,
            "status_code": e.code,
            "message": body,
        }

    except URLError as e:
        return {
            "ok": False,
            "utr_no": utr_value,
            "is_present": None,
            "status_code": 0,
            "message": str(e),
        }
    


    except Exception as e:
        logger.exception(f"Unexpected RMS UTR API error for utr_no={utr_value}: {e}")
        return {
            "ok": False,
            "utr_no": utr_value,
            "is_present": None,
            "status_code": 0,
            "message": str(e),
        }


    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body}

    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data if isinstance(data, dict) else {}

    return {
        "ok": 200 <= status_code < 300,
        "utr_no": utr_value,
        "is_present": _parse_bool(data.get("is_present")),
        "status_code": status_code,
        "message": payload.get("msg") if isinstance(payload, dict) else "",
        "payload": payload,
    }


def _metadata_amount_value(metadata):
    if not isinstance(metadata, dict):
        return 0

    try:
        return float(metadata.get("amount") or 0)
    except Exception:
        return 0


@router.get("/payment-metadata/pending-utr-update/dashboard")
async def get_pending_utr_update_dashboard(
    admin_number: Optional[str] = Query(None),
    phone: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    utr: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    only_incomplete: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    scan_limit: int = Query(500, ge=1, le=5000),
    live_check_limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    Live pending UTR dashboard.

    This does NOT cache RMS result.
    Every request checks RentMyStay UTR API live.

    Speed improvement:
    - only checks rows matched by cx_number
    - only checks rows where UTR/RRN/TXN exists
    - stops once current page has enough pending rows
    """

    try:
        batch_limit = 100
        current_scan_page = 1

        source_total = 0
        scanned_count = 0
        live_checks_used = 0

        required_pending_count = (page * limit) + 1
        pending_rows = []

        semaphore = asyncio.Semaphore(30)

        async def check_row_live(row):
            try:
                if row.get("matched_by") != "cx_number":
                    return None

                metadata = row.get("payment_metadata") or {}
                extracted_text = row.get("extracted_text") or ""

                utr_candidate = _extract_utr_candidate(metadata, extracted_text)
                row["utr_candidate"] = utr_candidate

                if not utr_candidate:
                    return None

                async with semaphore:
                    rms_status = await run_in_threadpool(
                        _call_rms_utr_details_api,
                        utr_candidate,
                    )

                row["rms_utr_check"] = rms_status

                if rms_status.get("is_present") is False:
                    row["pending_utr_update"] = True
                    row["utr_update_status"] = "pending"
                    row.pop("extracted_text", None)
                    return row

                return None

            except Exception as e:
                logger.exception(
                    f"Live pending UTR check failed for message_id={row.get('message_id')}: {e}"
                )
                return None

        while scanned_count < scan_limit and live_checks_used < live_check_limit:
            batch = await get_payment_metadata_dashboard(
                admin_number=admin_number,
                phone=phone,
                email=email,
                utr=utr,
                from_date=from_date,
                to_date=to_date,
                status=status,
                only_incomplete=only_incomplete,
                include_text=True,
                page=current_scan_page,
                limit=min(batch_limit, scan_limit - scanned_count),
                db=db,
            )

            rows = batch.get("data") or []
            source_total = int(batch.get("total_count") or 0)

            if not rows:
                break

            scanned_count += len(rows)

            candidate_rows = []

            for row in rows:
                if row.get("matched_by") != "cx_number":
                    continue

                metadata = row.get("payment_metadata") or {}
                extracted_text = row.get("extracted_text") or ""
                utr_candidate = _extract_utr_candidate(metadata, extracted_text)

                if not utr_candidate:
                    continue

                row["utr_candidate"] = utr_candidate
                candidate_rows.append(row)

            if candidate_rows:
                remaining_live_checks = live_check_limit - live_checks_used
                candidate_rows = candidate_rows[:remaining_live_checks]
                live_checks_used += len(candidate_rows)

                checked_rows = await asyncio.gather(
                    *(check_row_live(row) for row in candidate_rows),
                    return_exceptions=True,
                )

                for checked_row in checked_rows:
                    if isinstance(checked_row, dict) and checked_row:
                        pending_rows.append(checked_row)

            if len(pending_rows) >= required_pending_count:
                break

            if scanned_count >= source_total:
                break

            current_scan_page += 1

        offset = (page - 1) * limit
        page_rows = pending_rows[offset: offset + limit]

        has_next = len(pending_rows) > offset + limit

        total_amount = 0.0
        amount_count = 0
        incomplete_count = 0

        for row in page_rows:
            amount_value = _metadata_amount_value(row.get("payment_metadata") or {})
            total_amount += amount_value

            if amount_value > 0:
                amount_count += 1

            if row.get("is_incomplete"):
                incomplete_count += 1

        # This is a live fast endpoint, so total_count is a known minimum,
        # not full exact global count unless scanning reached the end.
        known_total_count = offset + len(page_rows) + (1 if has_next else 0)

        return {
            "success": True,
            "page": page,
            "limit": limit,
            "count": len(page_rows),
            "total_count": known_total_count,
            "total_is_exact": scanned_count >= source_total,
            "has_next": has_next,
            "source_total_count": source_total,
            "scanned_count": scanned_count,
            "scan_limit": scan_limit,
            "live_checks_used": live_checks_used,
            "live_check_limit": live_check_limit,
            "total_amount": total_amount,
            "amount_count": amount_count,
            "incomplete_count_on_page": incomplete_count,
            "filters": {
                "admin_number": normalize_number(admin_number) if admin_number else None,
                "phone": normalize_number(phone) if phone else None,
                "email": email,
                "utr": utr,
                "from_date": from_date,
                "to_date": to_date,
                "status": status,
                "only_incomplete": only_incomplete,
                "pending_utr_update_only": True,
                "utr_check_matched_by": "cx_number",
                "live": True,
            },
            "data": page_rows,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching live pending UTR update dashboard: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch live pending UTR update dashboard",
        )






@router.get("/payment-metadata/pending-utr-update/live-batch")
async def get_pending_utr_update_live_batch(
    admin_number: Optional[str] = Query(None),
    phone: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    utr: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    only_incomplete: bool = Query(False),

    cursor_page: int = Query(1, ge=1),
    source_batch_size: int = Query(15, ge=5, le=100),
    max_candidate_checks: int = Query(15, ge=1, le=100),
    live_concurrency: int = Query(10, ge=1, le=30),

    db: Session = Depends(get_db),
):
    """
    Live small-batch UTR check.

    This returns quickly because it checks only one small DB batch.
    Frontend calls this repeatedly and appends rows live.

    No cache is used. Every RMS UTR status is checked live.
    """

    try:
        batch = await get_payment_metadata_dashboard(
            admin_number=admin_number,
            phone=phone,
            email=email,
            utr=utr,
            from_date=from_date,
            to_date=to_date,
            status=status,
            only_incomplete=only_incomplete,
            include_text=True,
            page=cursor_page,
            limit=source_batch_size,
            db=db,
        )

        rows = batch.get("data") or []
        source_total = int(batch.get("total_count") or 0)

        candidate_rows = []

        for row in rows:
            if row.get("matched_by") != "cx_number":
                continue

            metadata = row.get("payment_metadata") or {}
            extracted_text = row.get("extracted_text") or ""

            utr_candidate = _extract_utr_candidate(metadata, extracted_text)

            if not utr_candidate:
                continue

            row["utr_candidate"] = utr_candidate
            candidate_rows.append(row)

            if len(candidate_rows) >= max_candidate_checks:
                break

        semaphore = asyncio.Semaphore(live_concurrency)

        async def check_row_live(row):
            try:
                utr_candidate = row.get("utr_candidate") or ""

                async with semaphore:
                    rms_status = await run_in_threadpool(
                        _call_rms_utr_details_api,
                        utr_candidate,
                    )

                row["rms_utr_check"] = rms_status

                if rms_status.get("is_present") is False:
                    row["pending_utr_update"] = True
                    row["utr_update_status"] = "pending"
                    row.pop("extracted_text", None)
                    return row

                return None

            except Exception as e:
                logger.exception(
                    f"Live UTR row check failed for message_id={row.get('message_id')}: {e}"
                )
                return None

        checked_rows = await asyncio.gather(
            *(check_row_live(row) for row in candidate_rows),
            return_exceptions=True,
        )

        pending_rows = [
            row for row in checked_rows
            if isinstance(row, dict) and row
        ]

        next_cursor_page = cursor_page + 1
        has_more_source = bool(source_total and (cursor_page * source_batch_size) < source_total)

        return {
            "success": True,
            "cursor_page": cursor_page,
            "next_cursor_page": next_cursor_page if has_more_source else None,
            "has_more_source": has_more_source,
            "source_total_count": source_total,
            "source_batch_size": source_batch_size,
            "source_rows_scanned": len(rows),
            "candidate_rows_checked": len(candidate_rows),
            "pending_count_in_batch": len(pending_rows),
            "filters": {
                "pending_utr_update_only": True,
                "utr_check_matched_by": "cx_number",
                "live": True,
            },
            "data": pending_rows,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in live pending UTR batch: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch live pending UTR batch",
        )




def _extract_booking_rows(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in ["data", "bookings", "results", "result"]:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for nested_key in ["data", "bookings", "results", "result"]:
                nested_value = value.get(nested_key)
                if isinstance(nested_value, list):
                    return nested_value

    if payload.get("booking_id"):
        return [payload]

    return []


def _call_rms_booking_api(contact_number: str):
    query = urlencode({"contact_number": contact_number})
    url = f"{RMS_BOOKING_API_URL}?{query}"

    request = UrlRequest(
        url,
        headers={
            "Authorization": RMS_BOOKING_API_TOKEN,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = response.getcode()

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": e.code,
            "body": body,
        }

    except URLError as e:
        return {
            "ok": False,
            "status_code": 0,
            "body": str(e),
        }

    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body}

    return {
        "ok": 200 <= status_code < 300,
        "status_code": status_code,
        "payload": payload,
    }



def _normalize_booking_items(raw_bookings):
    bookings = []

    for item in raw_bookings:
        if not isinstance(item, dict):
            continue

        bookings.append({
            "booking_id": item.get("booking_id") or item.get("id") or item.get("bookingId"),
            "contact_number": item.get("contact_number") or item.get("contactNumber"),
            "contact_email": item.get("contact_email") or item.get("contactEmail") or item.get("email"),
            "contact_type": item.get("contact_type") or item.get("contactType"),
            "raw": item,
        })

    return bookings


def _fetch_bookings_for_contact_sync(contact_number: str):
    contact_digits = _only_digits(contact_number)

    if not contact_digits or not RMS_BOOKING_API_TOKEN:
        return []

    result = _call_rms_booking_api(contact_digits)

    if not result.get("ok"):
        return []

    payload = result.get("payload") or {}
    raw_bookings = _extract_booking_rows(payload)

    return _normalize_booking_items(raw_bookings)




def _save_booking_email_into_payment_metadata(db: Session, contact_number: str, bookings: list):
    contact_digits = _only_digits(contact_number)

    if not contact_digits or not bookings:
        return 0

    selected = None
    for booking in bookings:
        if booking.get("contact_email"):
            selected = booking
            break

    if not selected:
        return 0

    contact_email = selected.get("contact_email")
    booking_id = selected.get("booking_id")
    contact_type = selected.get("contact_type")

    result = db.execute(text("""
        WITH target_rows AS (
            SELECT
                id
            FROM public.messages
            WHERE image_type = 'payment_receipt'
              AND (
                    right(regexp_replace(coalesce(peer_pn, ''), '[^0-9]', '', 'g'), 10) = right(:contact_digits, 10)
                 OR right(regexp_replace(coalesce(cx_number, ''), '[^0-9]', '', 'g'), 10) = right(:contact_digits, 10)
              )
        )
        UPDATE public.messages m
        SET payment_metadata =
            COALESCE(m.payment_metadata, '{}'::jsonb)
            || jsonb_build_object(
                'booking_contact_email', :contact_email,
                'booking_id', :booking_id,
                'booking_contact_type', :contact_type
            )
        FROM target_rows t
        WHERE m.id = t.id
    """), {
        "contact_digits": contact_digits,
        "contact_email": contact_email,
        "booking_id": str(booking_id or ""),
        "contact_type": contact_type,
    })

    db.commit()
    return result.rowcount or 0




'''@router.get("/payment-metadata/bookings/by-contact")
async def get_bookings_by_contact(
    contact_number: str = Query(..., description="Customer contact number"),
):'''

@router.get("/payment-metadata/bookings/by-contact")
async def get_bookings_by_contact(
    contact_number: str = Query(..., description="Customer contact number"),
    db: Session = Depends(get_db),
):
    contact_digits = _only_digits(contact_number)

    if not contact_digits:
        raise HTTPException(status_code=400, detail="Invalid contact number")

    if not RMS_BOOKING_API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="RMS_BOOKING_API_TOKEN is not configured",
        )

    result = await run_in_threadpool(_call_rms_booking_api, contact_digits)

    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Booking API failed",
                "status_code": result.get("status_code"),
                "body": result.get("body"),
            },
        )

    payload = result.get("payload") or {}
    raw_bookings = _extract_booking_rows(payload)

    '''bookings = []

    for item in raw_bookings:
        if not isinstance(item, dict):
            continue

        bookings.append({
            "booking_id": item.get("booking_id") or item.get("id") or item.get("bookingId"),
            "contact_number": item.get("contact_number") or item.get("contactNumber"),
            "contact_email": item.get("contact_email") or item.get("contactEmail") or item.get("email"),
            "contact_type": item.get("contact_type") or item.get("contactType"),
            "raw": item,
        })'''

    bookings = _normalize_booking_items(raw_bookings)   

    updated_rows = _save_booking_email_into_payment_metadata(
        db,
        contact_digits,
        bookings,
    ) 

    '''return {
        "success": True,
        "contact_number": contact_digits,
        "count": len(bookings),
        "bookings": bookings,
    }'''

    return {
        "success": True,
        "contact_number": contact_digits,
        "count": len(bookings),
        "updated_payment_metadata_rows": updated_rows,
        "bookings": bookings,
    }





@router.get("/payment-metadata/dashboard-page", response_class=HTMLResponse)
async def payment_metadata_dashboard_page():
    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
    <title>Payment Receipts Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />

    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f5f6fa;
            color: #1f2937;
        }

        .container {
            padding: 24px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 20px;
        }

        h1 {
            margin: 0;
            font-size: 26px;
        }

        .subtitle {
            color: #6b7280;
            margin-top: 6px;
        }

        button {
            border: none;
            padding: 10px 14px;
            border-radius: 8px;
            background: #111827;
            color: white;
            cursor: pointer;
        }

        .reset-btn {
            background: #6b7280;
        }


.pending-utr-btn {
    width: 100%;
    min-height: 48px;
    padding: 12px 18px;
    border: 1px solid #2563eb;
    border-radius: 10px;
    background: #2563eb;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    transition:
        background-color 0.2s ease,
        border-color 0.2s ease,
        box-shadow 0.2s ease,
        transform 0.1s ease;
}

.pending-utr-btn:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
    box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);
}

.pending-utr-btn:active {
    transform: translateY(1px);
}

.pending-utr-btn.active {
    background: #0f766e;
    border-color: #0f766e;
}

.pending-utr-btn.active:hover {
    background: #115e59;
    border-color: #115e59;
    box-shadow: 0 4px 12px rgba(15, 118, 110, 0.25);
}

.pending-utr-btn:disabled {
    opacity: 0.65;
    cursor: not-allowed;
    box-shadow: none;
}       

.utr-pending {
    background: #ffedd5;
    color: #9a3412;
}

.utr-skipped {
    background: #f3f4f6;
    color: #6b7280;
}

        .summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 18px;
}

.summary-action-card {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 12px;
}



        .card {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
        }

        .card span {
            color: #6b7280;
            font-size: 13px;
        }

        .card strong {
            display: block;
            margin-top: 8px;
            font-size: 24px;
        }

        .filters {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 18px;
            display: grid;
            grid-template-columns: repeat(9, minmax(130px, 1fr));
            gap: 12px;
            align-items: end;
        }

        label {
            font-size: 13px;
            color: #374151;
            display: block;
            margin-bottom: 6px;
        }

        input, select {
            width: 100%;
            height: 38px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            padding: 0 10px;
            box-sizing: border-box;
        }

        .checkbox-wrap {
            display: flex;
            align-items: center;
            height: 38px;
            gap: 8px;
        }

        .checkbox-wrap input {
            width: auto;
            height: auto;
        }

        .table-card {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            overflow: hidden;
        }

        .table-wrap {
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            min-width: 1200px;
        }

        th, td {
            padding: 12px;
            border-bottom: 1px solid #e5e7eb;
            text-align: left;
            font-size: 14px;
            vertical-align: top;
        }

        th {
            background: #f3f4f6;
            color: #6b7280;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.04em;
        }

        .amount {
            font-weight: 700;
        }

        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
        }

        .success, .completed, .ok {
            background: #dcfce7;
            color: #166534;
        }

        .pending, .issue {
            background: #fef3c7;
            color: #92400e;
        }

        .failed {
            background: #fee2e2;
            color: #991b1b;
        }

        .missing {
            background: #f3f4f6;
            color: #6b7280;
        }

        .link {
            color: #2563eb;
            font-weight: 600;
            text-decoration: none;
        }

        .error {
            background: #fee2e2;
            color: #991b1b;
            padding: 12px;
            border-radius: 10px;
            margin-bottom: 16px;
            display: none;
        }

        .empty {
            text-align: center;
            padding: 30px;
            color: #6b7280;
        }

        .pagination {
            padding: 14px;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 14px;
        }

        .pagination button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        



        .booking-cell {
    min-width: 150px;
    max-width: 190px;
}

.booking-btn {
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    background: #2563eb;
    font-weight: 700;
    line-height: 1;
}

.booking-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
}

.booking-result {
    margin-top: 8px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    max-width: 190px;
}





.booking-chip {
    display: inline-flex;
    flex-direction: column;
    gap: 2px;
    border: 1px solid #dbeafe;
    background: #eff6ff;
    color: #1e3a8a;
    border-radius: 10px;
    padding: 6px 8px;
    font-size: 12px;
    line-height: 1.15;
    text-decoration: none;
    cursor: pointer;
}

.booking-chip:hover {
    border-color: #2563eb;
    background: #dbeafe;
}

.booking-chip .booking-id {
    font-weight: 800;
}

.booking-chip .booking-type {
    color: #64748b;
    font-size: 11px;
}





.booking-empty {
    color: #6b7280;
    font-size: 12px;
}

.booking-error {
    color: #991b1b;
    background: #fee2e2;
    border-radius: 8px;
    padding: 5px 7px;
    font-size: 12px;
}








        @media (max-width: 1000px) {
            .summary { grid-template-columns: repeat(2, 1fr); }
            .filters { grid-template-columns: repeat(2, 1fr); }
        }

        @media (max-width: 600px) {
            .header { flex-direction: column; align-items: flex-start; }
            .summary, .filters { grid-template-columns: 1fr; }
        }
    </style>
</head>

<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>Payment Receipts Dashboard</h1>
                <div class="subtitle">Finance team can review WhatsApp payment receipt here.</div>
            </div>
            
            <button onclick="refreshDashboard()">Refresh</button>
        </div>

        <div class="summary">
    <div class="card">
        <span>Total Receipts</span>
        <strong id="totalCount">0</strong>
    </div>

    <div class="card">
        <span>Rows on Page</span>
        <strong id="pageCount">0</strong>
    </div>

    <div class="card">
        <span>Incomplete on Page</span>
        <strong id="incompleteCount">0</strong>
    </div>

    <div class="card summary-action-card">
    <span>UTR Verification</span>

    <button id="pendingUtrUpdateBtn"
            class="pending-utr-btn"
            onclick="togglePendingUtrUpdate()"
            title="Show receipts whose UTR is not yet present in RentMyStay">
        View Pending UTRs
    </button>
</div>
</div>

        <div class="filters">
            <div>
                <label>Customer Phone</label>
                <input id="phone" placeholder="917265833012" />
            </div>

            <div>
                <label>Admin Number</label>
                <input id="adminNumber" placeholder="Admin number" />
            </div>

            <div>
                <label>Gmail ID</label>
                <input id="emailSearch" placeholder="name@gmail.com" />
            </div>

            <div>
                <label>UTR / RRN / TXN</label>
                <input id="utrSearch" placeholder="UTR / RRN / TXN" />
            </div>


            <div>
                <label>From Date</label>
                <input id="fromDate" type="date" />
            </div>

            <div>
                <label>To Date</label>
                <input id="toDate" type="date" />
            </div>

            <div>
                <label>Status</label>
                <select id="status">
                    <option value="">All</option>
                    <option value="success">Success</option>
                    <option value="completed">Completed</option>
                    <option value="pending">Pending</option>
                    <option value="failed">Failed</option>
                </select>
            </div>

            <div>
                <label>Limit</label>
                <select id="limit">
                    <option value="25">25</option>
                    <option value="50" selected>50</option>
                    <option value="100">100</option>
                    <option value="200">200</option>
                </select>
            </div>

            <div class="checkbox-wrap">
                <input id="onlyIncomplete" type="checkbox" />
                <span>Only incomplete</span>
            </div>

               <button onclick="applyFilters()">Apply</button>
               <button class="reset-btn" onclick="resetFilters()">Reset</button>      

        </div>

        <div id="errorBox" class="error"></div>

        <div class="table-card">
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Receipt Time</th>
                            <th>Customer Phone</th>
                            <th>Booking</th>
                            <th>Admin Number</th>
                            <th>Amount</th>
                            <th>Status</th>
                            <th>UTR / RRN / Txn ID</th>
                            <th>Matched By</th>
                            <th>Receipt</th>
                            <th>Issue</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody">
                        <tr><td colspan="10" class="empty">Loading...</td></tr>
                    </tbody>
                </table>
            </div>

            <div class="pagination">
                <button id="prevBtn" onclick="prevPage()">Previous</button>
                <span id="pageInfo">Page 1</span>
                <button id="nextBtn" onclick="nextPage()">Next</button>
            </div>
        </div>
    </div>

    <script>
        let currentPage = 1;
        let totalPages = 1;
        let pendingUtrUpdateOnly = false;

        let pendingUtrLoadToken = 0;


let pendingUtrPageCache = {};

let pendingUtrNextSourceCursor = 1;


let pendingUtrCarryRows = [];


let pendingUtrSourceFinished = false;


let pendingUtrSeenIds = new Set();

        function formatAmount(value) {
            if (value === null || value === undefined || value === "") return "—";
            const n = Number(value);
            if (Number.isNaN(n)) return value;

            return new Intl.NumberFormat("en-IN", {
                style: "currency",
                currency: "INR",
                maximumFractionDigits: 2
            }).format(n);
        }

        function formatDate(value) {
            if (!value) return "—";
            const d = new Date(value);
            if (Number.isNaN(d.getTime())) return value;

            return d.toLocaleString("en-IN", {
                day: "2-digit",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit"
            });
        }

        
        function getTxnRef(metadata) {
    if (!metadata) return "—";

    return (
        metadata.utr ||
        metadata.rrn ||
        metadata.transaction_reference ||
        metadata.transaction_id ||
        metadata.txn_id ||
        "—"
    );
}

        function getStatusClass(status) {
            if (!status) return "missing";
            return String(status).toLowerCase();
        }

        function buildQuery() {
            const params = new URLSearchParams();

            params.set("page", String(currentPage));
            params.set("limit", document.getElementById("limit").value || "50");

            const phone = document.getElementById("phone").value.trim();
            const adminNumber = document.getElementById("adminNumber").value.trim();
            const emailSearch = document.getElementById("emailSearch").value.trim();
            const utrSearch = document.getElementById("utrSearch").value.trim();
            const fromDate = document.getElementById("fromDate").value;
            const toDate = document.getElementById("toDate").value;
            const status = document.getElementById("status").value;
            const onlyIncomplete = document.getElementById("onlyIncomplete").checked;

            if (phone) params.set("phone", phone);
            if (adminNumber) params.set("admin_number", adminNumber);
            if (emailSearch) params.set("email", emailSearch);
            if (utrSearch) params.set("utr", utrSearch);
            if (fromDate) params.set("from_date", fromDate);
            if (toDate) params.set("to_date", toDate);
            if (status) params.set("status", status);
            if (onlyIncomplete) params.set("only_incomplete", "true");



return params.toString();

        }






        const bookingCache = {};

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, function(ch) {
        return {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;"
        }[ch];
    });
}

function formatMatchedBy(value) {
    if (value === "peer_pn") return "peer_pn";
    if (value === "cx_number") return "cx_number";
    if (value === "peer_pn_self_admin") return "self/admin peer";
    if (value === "cx_self_admin") return "self/admin chat";
    return value || "unknown";
}


function getEmailSearchValue() {
    return document.getElementById("emailSearch").value.trim().toLowerCase();
}

function bookingMatchesEmail(bookings) {
    const emailSearch = getEmailSearchValue();

    if (!emailSearch) {
        return bookings || [];
    }

    return (bookings || []).filter(function(b) {
        return String(b.contact_email || "").toLowerCase().includes(emailSearch);
    });
}

function updateVisiblePageCount() {
    const rows = Array.from(document.querySelectorAll("#tableBody tr"));
    const visibleRows = rows.filter(function(row) {
        return row.style.display !== "none" && !row.querySelector(".empty");
    });

    document.getElementById("pageCount").innerText = visibleRows.length;
}



function renderBookingResult(box, bookings) {
    if (!bookings || bookings.length === 0) {
        box.innerHTML = '<span class="booking-empty">No booking found</span>';
        return;
    }

    box.innerHTML = bookings.map(function(b) {
        const bookingId = escapeHtml(b.booking_id || "—");
        const contactType = escapeHtml(b.contact_type || "—");
        const contactEmail = escapeHtml(b.contact_email || "");

        const bookingUrl = "https://www.rentmystay.com/RBack/invoice_details/"
            + encodeURIComponent(bookingId);

        

        return `
            <a class="booking-chip"
                href="${bookingUrl}"
                target="_blank"
                title="Open booking #${bookingId}">
                 <span class="booking-id">#${bookingId}</span>
                 <span class="booking-type">${contactType}</span>
                ${contactEmail ? `<span class="booking-type">${contactEmail}</span>` : ""}
            </a>
        `;


    }).join("");
}




async function loadBookingsIntoBox(box) {
    const contact = (box.dataset.contact || "").trim();
    const row = box.closest("tr");


    const preloaded = box.dataset.preloadedBookings || "";

if (preloaded && preloaded !== "[]") {
    try {
        const bookings = JSON.parse(preloaded);
        bookingCache[contact] = bookings;
        renderBookingResult(box, bookings);
        updateVisiblePageCount();
        return;
    } catch (e) {
        // ignore bad preload and continue normal API call
    }
}

    if (!contact) {
        box.innerHTML = '<span class="booking-empty">No customer phone</span>';
        return;
    }

    function renderWithEmailFilter(bookings) {
        const matchedBookings = bookingMatchesEmail(bookings);

        if (getEmailSearchValue() && matchedBookings.length === 0) {
            row.style.display = "none";
            updateVisiblePageCount();
            return;
        }

        row.style.display = "";
        renderBookingResult(box, matchedBookings);
        updateVisiblePageCount();
    }

    if (bookingCache[contact]) {
        renderWithEmailFilter(bookingCache[contact]);
        return;
    }

    box.innerHTML = '<span class="booking-empty">Loading booking...</span>';

    try {
        const url = "/connector/api/whatsapp/payment-metadata/bookings/by-contact?contact_number="
            + encodeURIComponent(contact);

        const response = await fetch(url);

        if (!response.ok) {
            throw new Error("Booking API failed: " + response.status);
        }

        const result = await response.json();
        const bookings = result.bookings || [];

        bookingCache[contact] = bookings;
        renderWithEmailFilter(bookings);

    } catch (err) {
        if (getEmailSearchValue()) {
            row.style.display = "none";
            updateVisiblePageCount();
            return;
        }

        box.innerHTML = '<span class="booking-error">' + escapeHtml(err.message || "Failed") + '</span>';
    }
}


function autoLoadBookingsForVisibleRows() {
    const emailSearch = getEmailSearchValue();

    document.querySelectorAll("#tableBody tr").forEach(function(row) {
        const box = row.querySelector(".booking-result[data-contact]");

        if (box) {
            loadBookingsIntoBox(box);
            return;
        }

        if (emailSearch && !row.querySelector(".empty")) {
            row.style.display = "none";
        }
    });

    updateVisiblePageCount();
}


function getIssueBadge(row) {
    if (pendingUtrUpdateOnly) {
        if (row.utr_update_status === "pending") {
            return '<span class="badge utr-pending">Pending UTR update</span>';
        }

        if (row.utr_update_status === "missing_utr") {
            return '<span class="badge issue">No UTR found</span>';
        }

        return '<span class="badge utr-skipped">Not checked</span>';
    }

    return row.is_incomplete
        ? '<span class="badge issue">Missing data</span>'
        : '<span class="badge ok">OK</span>';
}




function syncPendingUtrUpdateButton() {
    const btn = document.getElementById("pendingUtrUpdateBtn");
    if (!btn) return;

    btn.classList.toggle("active", pendingUtrUpdateOnly);

    if (pendingUtrUpdateOnly) {
        btn.innerText = "Show All Receipts";
        btn.title = "Return to the complete payment receipts dashboard";
    } else {
        btn.innerText = "View Pending UTRs";
        btn.title = "Show receipts whose UTR is not yet present in RentMyStay";
    }
}




function togglePendingUtrUpdate() {
    currentPage = 1;

    pendingUtrUpdateOnly =
        !pendingUtrUpdateOnly;

    resetPendingUtrPaging();
    syncPendingUtrUpdateButton();
    loadData();
}




function renderPaymentRows(rows) {
    const tableBody = document.getElementById("tableBody");

    if (!rows || rows.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="10" class="empty">No payment receipts found.</td></tr>';
        return;
    }

    tableBody.innerHTML = rows.map(row => {
        const metadata = row.payment_metadata || {};
        const status = metadata.status || "missing";
        const issue = getIssueBadge(row);

        const receiptLink = row.r2_media_url
            ? `<a class="link" href="${row.r2_media_url}" target="_blank">View</a>`
            : "—";

        return `
            <tr>
                <td>${formatDate(row.timestamp)}</td>

                <td>
                    <strong>${escapeHtml(row.customer_number || "—")}</strong><br/>
                    <small>peer: ${escapeHtml(row.peer_pn || "—")}</small>
                </td>

                <td class="booking-cell">
                    ${
                        row.customer_number
                            ? `<div class="booking-result"
                                    data-contact="${escapeHtml(row.customer_number)}"
                                    data-preloaded-bookings='${escapeHtml(JSON.stringify(row.bookings || []))}'>
                                <span class="booking-empty">Loading booking...</span>
                            </div>`
                            : `<span class="badge missing">No customer</span>`
                    }
                </td>

                <td>${escapeHtml(row.admin_number || "—")}</td>
                <td class="amount">${formatAmount(metadata.amount)}</td>
                <td><span class="badge ${getStatusClass(status)}">${escapeHtml(status)}</span></td>
                <td>${escapeHtml(row.utr_candidate || getTxnRef(metadata))}</td>
                <td><span class="badge missing">${escapeHtml(formatMatchedBy(row.matched_by))}</span></td>
                <td>${receiptLink}</td>
                <td>${issue}</td>
            </tr>
        `;
    }).join("");

    autoLoadBookingsForVisibleRows();
}

function resetPendingUtrPaging() {
    // Cancel any currently running live scan.
    pendingUtrLoadToken++;

    pendingUtrPageCache = {};
    pendingUtrNextSourceCursor = 1;
    pendingUtrCarryRows = [];
    pendingUtrSourceFinished = false;
    pendingUtrSeenIds = new Set();
}


function renderPendingUtrPage(pageRows, customMessage = "") {
    const tableBody = document.getElementById("tableBody");

    if (pageRows.length > 0) {
        renderPaymentRows(pageRows);
    } else {
        tableBody.innerHTML =
            '<tr><td colspan="10" class="empty">No pending UTR updates found.</td></tr>';
    }

    const loadedPendingCount = Object.values(
        pendingUtrPageCache
    ).reduce(function(total, rows) {
        return total + rows.length;
    }, 0);

    document.getElementById("totalCount").innerText =
        pendingUtrSourceFinished
            ? String(loadedPendingCount)
            : loadedPendingCount + "+";

    document.getElementById("pageCount").innerText =
        pageRows.length;

    document.getElementById("incompleteCount").innerText =
        pageRows.filter(function(row) {
            return row.is_incomplete;
        }).length;

    document.getElementById("pageInfo").innerText =
        customMessage || (
            "Page " + currentPage +
            " • " + pageRows.length +
            " pending UTR updates"
        );

    document.getElementById("prevBtn").disabled =
        currentPage <= 1;

    const nextPageAlreadyLoaded = Boolean(
        pendingUtrPageCache[currentPage + 1]
    );

    const canLoadMore =
        nextPageAlreadyLoaded ||
        pendingUtrCarryRows.length > 0 ||
        !pendingUtrSourceFinished;

    document.getElementById("nextBtn").disabled =
        !canLoadMore;
}


async function loadPendingUtrLive() {
    const token = ++pendingUtrLoadToken;

    const errorBox = document.getElementById("errorBox");
    const tableBody = document.getElementById("tableBody");

    errorBox.style.display = "none";
    errorBox.innerText = "";

    const targetRows = Number(
        document.getElementById("limit").value || 50
    );

    
    const cachedPageRows = pendingUtrPageCache[currentPage];

    if (cachedPageRows) {
        renderPendingUtrPage(cachedPageRows);
        return;
    }

    let pageRows = [];

    
    while (
        pendingUtrCarryRows.length > 0 &&
        pageRows.length < targetRows
    ) {
        pageRows.push(pendingUtrCarryRows.shift());
    }

    document.getElementById("prevBtn").disabled = true;
    document.getElementById("nextBtn").disabled = true;

    tableBody.innerHTML =
        '<tr><td colspan="10" class="empty">' +
        'Loading pending UTR page ' + currentPage + '...' +
        '</td></tr>';

    let scannedRows = 0;
    let checkedCandidates = 0;

    
    while (
        token === pendingUtrLoadToken &&
        pendingUtrUpdateOnly &&
        pageRows.length < targetRows &&
        !pendingUtrSourceFinished
    ) {
        const params = new URLSearchParams(buildQuery());

        params.delete("page");
        params.delete("limit");

        params.set(
            "cursor_page",
            String(pendingUtrNextSourceCursor)
        );

        params.set("source_batch_size", "15");
        params.set("max_candidate_checks", "15");
        params.set("live_concurrency", "10");

        const url =
            "/connector/api/whatsapp/payment-metadata/" +
            "pending-utr-update/live-batch?" +
            params.toString();

        const response = await fetch(url);

        if (!response.ok) {
            let errorDetail = "";

            try {
                const errorResult = await response.json();

                if (errorResult.detail) {
                    errorDetail =
                        " - " + JSON.stringify(errorResult.detail);
                }
            } catch (parseError) {
                // Ignore non-JSON error body.
            }

            throw new Error(
                "Live UTR API failed: " +
                response.status +
                errorDetail
            );
        }

        const result = await response.json();

        
        if (
            token !== pendingUtrLoadToken ||
            !pendingUtrUpdateOnly
        ) {
            return;
        }

        scannedRows += result.source_rows_scanned || 0;
        checkedCandidates += result.candidate_rows_checked || 0;

        const rows = result.data || [];

        for (const row of rows) {
            const uniqueKey = String(
                row.id ||
                row.message_id ||
                row.utr_candidate ||
                (
                    String(row.timestamp || "") +
                    "-" +
                    String(row.customer_number || "")
                )
            );

            if (
                !uniqueKey ||
                pendingUtrSeenIds.has(uniqueKey)
            ) {
                continue;
            }

            pendingUtrSeenIds.add(uniqueKey);

            
            if (pageRows.length < targetRows) {
                pageRows.push(row);
            } else {
                pendingUtrCarryRows.push(row);
            }
        }

        if (
            result.has_more_source &&
            result.next_cursor_page
        ) {
            pendingUtrNextSourceCursor =
                result.next_cursor_page;
        } else {
            pendingUtrSourceFinished = true;
        }

    
        if (pageRows.length > 0) {
            renderPaymentRows(pageRows);
        }

        document.getElementById("pageCount").innerText =
            pageRows.length;

        document.getElementById("incompleteCount").innerText =
            pageRows.filter(function(row) {
                return row.is_incomplete;
            }).length;

        document.getElementById("pageInfo").innerText =
            "Page " + currentPage +
            " • found " + pageRows.length +
            " of " + targetRows +
            " • checked " + checkedCandidates +
            " UTRs";

       
        await new Promise(function(resolve) {
            setTimeout(resolve, 50);
        });
    }

    
    if (
        pageRows.length === 0 &&
        currentPage > 1 &&
        pendingUtrSourceFinished
    ) {
        currentPage -= 1;

        const previousRows =
            pendingUtrPageCache[currentPage] || [];

        renderPendingUtrPage(
            previousRows,
            "No more pending UTR updates."
        );

        document.getElementById("nextBtn").disabled = true;
        return;
    }

    
    pendingUtrPageCache[currentPage] = pageRows;

    renderPendingUtrPage(pageRows);
}




        async function loadData() {

            if (pendingUtrUpdateOnly) {
    try {
        await loadPendingUtrLive();
    } catch (err) {
        const errorBox = document.getElementById("errorBox");
        const tableBody = document.getElementById("tableBody");

        errorBox.style.display = "block";
        errorBox.innerText = err.message || "Failed to load live pending UTR updates";
        tableBody.innerHTML = '<tr><td colspan="10" class="empty">Failed to load data.</td></tr>';
    }

    return;
}
pendingUtrLoadToken++;

            const errorBox = document.getElementById("errorBox");
            const tableBody = document.getElementById("tableBody");

            errorBox.style.display = "none";
            errorBox.innerText = "";
            tableBody.innerHTML = '<tr><td colspan="10" class="empty">Loading...</td></tr>';

            try {
                const endpoint = pendingUtrUpdateOnly
                    ? "/connector/api/whatsapp/payment-metadata/pending-utr-update/dashboard?"
                    : "/connector/api/whatsapp/payment-metadata/dashboard?";

                const url = endpoint + buildQuery();
                
                const response = await fetch(url);

                if (!response.ok) {
                    throw new Error("API failed: " + response.status);
                }

                const result = await response.json();

                document.getElementById("totalCount").innerText = result.total_count || 0;
                document.getElementById("pageCount").innerText = result.count || 0;
                document.getElementById("incompleteCount").innerText = result.incomplete_count_on_page || 0;



                const limit = Number(document.getElementById("limit").value || 50);
totalPages = Math.max(1, Math.ceil((result.total_count || 0) / limit));

if (pendingUtrUpdateOnly) {
    document.getElementById("pageInfo").innerText = result.total_is_exact
        ? "Page " + currentPage + " of " + totalPages
        : "Page " + currentPage;

    document.getElementById("prevBtn").disabled = currentPage <= 1;
    document.getElementById("nextBtn").disabled = !result.has_next;
} else {
    document.getElementById("pageInfo").innerText = "Page " + currentPage + " of " + totalPages;
    document.getElementById("prevBtn").disabled = currentPage <= 1;
    document.getElementById("nextBtn").disabled = currentPage >= totalPages;
}

                const rows = result.data || [];

                if (rows.length === 0) {
                    tableBody.innerHTML = '<tr><td colspan="10" class="empty">No payment receipts found.</td></tr>';
                    return;
                }

                renderPaymentRows(rows);

            } catch (err) {
                errorBox.style.display = "block";
                errorBox.innerText = err.message || "Failed to load dashboard";
                tableBody.innerHTML = '<tr><td colspan="10" class="empty">Failed to load data.</td></tr>';
            }
        }


function refreshDashboard() {
    currentPage = 1;

    
    resetPendingUtrPaging();
    loadData();
}


function applyFilters() {
    currentPage = 1;


    resetPendingUtrPaging();
    loadData();
}

        function resetFilters() {
            currentPage = 1;

            document.getElementById("phone").value = "";
            document.getElementById("adminNumber").value = "";
            document.getElementById("emailSearch").value = "";
            document.getElementById("utrSearch").value = "";
            document.getElementById("fromDate").value = "";
            document.getElementById("toDate").value = "";
            document.getElementById("status").value = "";
            document.getElementById("limit").value = "50";
            document.getElementById("onlyIncomplete").checked = false;
           


pendingUtrUpdateOnly = false;

resetPendingUtrPaging();
syncPendingUtrUpdateButton();
loadData();


        }

        

        function prevPage() {
    if (pendingUtrUpdateOnly) {
        if (currentPage > 1) {
            pendingUtrLoadToken++;
            currentPage -= 1;
            loadData();
        }

        return;
    }

    if (currentPage > 1) {
        currentPage -= 1;
        loadData();
    }
}


function nextPage() {
    if (pendingUtrUpdateOnly) {
        const nextPageAlreadyLoaded = Boolean(
            pendingUtrPageCache[currentPage + 1]
        );

        const canScanMore =
            pendingUtrCarryRows.length > 0 ||
            !pendingUtrSourceFinished;

        if (
            nextPageAlreadyLoaded ||
            canScanMore
        ) {
            pendingUtrLoadToken++;
            currentPage += 1;
            loadData();
        }

        return;
    }

    if (currentPage < totalPages) {
        currentPage += 1;
        loadData();
    }
}

        loadData();
    </script>
</body>
</html>
    """)