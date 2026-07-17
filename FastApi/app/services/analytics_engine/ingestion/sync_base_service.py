import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from .sync_checkpoint_service import StagingSyncCheckpointService


TIMEZONE_NAME = "Asia/Kolkata"
IST_OFFSET = timedelta(hours=5, minutes=30)
IST_TZ = timezone(IST_OFFSET)


class StagingSyncBaseService:
    def __init__(self, db: Session):
        self.db = db
        self.checkpoint = StagingSyncCheckpointService(db)

    def _bulk_upsert_in_chunks(self, sql_text, rows, chunk_size=2000):
        if not rows:
            return

        total = len(rows)
        for start in range(0, total, chunk_size):
            chunk = rows[start:start + chunk_size]
            try:
                self.db.execute(sql_text, chunk)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    @staticmethod
    def _clean_text(val):
        if val is None:
            return None
        txt = str(val).strip()
        return txt or None

    @staticmethod
    def _clean_lower_email(val):
        if val is None:
            return None
        txt = str(val).strip().lower()
        return txt or None

    @staticmethod
    def norm_phone(val):
        if val in (None, ""):
            return None

        digits = "".join(ch for ch in str(val) if ch.isdigit())
        if not digits:
            return None

        if len(digits) == 10:
            return "91" + digits
        if len(digits) == 11 and digits.startswith("0"):
            return "91" + digits[1:]
        if len(digits) == 12 and digits.startswith("91"):
            return digits

        return digits[-12:] if len(digits) >= 12 else digits

    @staticmethod
    def safe_dt(val):
        if val in (None, "", "0000-00-00 00:00:00", "0000-00-00"):
            return None

        txt = str(val).strip()
        if not txt:
            return None

        # MySQL can contain invalid zero month/day dates such as
        # 2016-00-00 00:00:00. PostgreSQL rejects these as timestamps.
        if txt.startswith("0000-") or "-00-" in txt or txt[5:7] == "00" or txt[8:10] == "00":
            return None

        if isinstance(val, datetime):
            if val.tzinfo is not None:
                return val.astimezone(IST_TZ).replace(tzinfo=None)
            return val.replace(tzinfo=None)
        if isinstance(val, date):
            return datetime.combine(val, datetime.min.time())

        # If a general source supplies an explicit offset/Z timestamp, normalize it
        # to IST at storage time. Plain naive strings are kept unchanged because
        # RMS/MySQL source timestamps are already local IST.
        if isinstance(val, str) and (val.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", val.strip())):
            try:
                parsed = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(IST_TZ).replace(tzinfo=None)
            except Exception:
                pass
        return val

    @staticmethod
    def parse_dt(val: Any):
        cleaned = StagingSyncBaseService.safe_dt(val)
        if cleaned in (None, ""):
            return None
        if isinstance(cleaned, datetime):
            return cleaned
        if isinstance(cleaned, date):
            return datetime.combine(cleaned, datetime.min.time())
        text_value = str(cleaned).strip()
        if not text_value:
            return None
        try:
            return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except Exception:
            return cleaned

    @staticmethod
    def safe_dt_from_utc(val: Any):
        """Return an Asia/Kolkata naive timestamp for UTC/UTC-like source values.

        Use this only for third-party source tables whose datetime columns are
        UTC or naive-UTC. MySQL/RMS source datetimes are already IST and should
        continue using safe_dt().
        """
        parsed = StagingSyncBaseService.parse_dt(val)
        if parsed in (None, ""):
            return None
        if not isinstance(parsed, datetime):
            return parsed
        if parsed.tzinfo is not None:
            return parsed.astimezone(IST_TZ).replace(tzinfo=None)
        return parsed + IST_OFFSET

    @staticmethod
    def source_utc_cursor_from_ist(last_timestamp: Any):
        """Convert an IST checkpoint cursor back to UTC for source DB filters."""
        parsed = StagingSyncBaseService.parse_dt(last_timestamp)
        if parsed in (None, ""):
            return None
        if not isinstance(parsed, datetime):
            return parsed
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(IST_TZ).replace(tzinfo=None)
        return parsed - IST_OFFSET

    @staticmethod
    def safe_numeric(val):
        if val in (None, "", "NA", "na", "N/A", "null", "NULL"):
            return None
        try:
            return float(val)
        except Exception:
            return None

    @staticmethod
    def safe_bool(val):
        if val is None:
            return None
        txt = str(val).strip().lower()
        if txt in {"yes", "1", "true", "y"}:
            return True
        if txt in {"no", "0", "false", "n"}:
            return False
        return None

    @staticmethod
    def safe_int(val):
        if val in (None, "", "NULL", "null"):
            return None

        if isinstance(val, int):
            return val

        val_str = str(val).strip()
        if not val_str:
            return None

        if val_str.lstrip("-").isdigit():
            try:
                return int(val_str)
            except Exception:
                return None

        digits = "".join(ch for ch in val_str if ch.isdigit())
        if not digits:
            return None
        return int(digits)

    @staticmethod
    def _normalize_call_direction(val):
        txt = str(val).strip().lower() if val is not None else ""
        if not txt:
            return None
        if txt in {"incoming", "inbound", "received", "receive", "in"}:
            return "incoming"
        if txt in {"outgoing", "outbound", "dialed", "dial", "out"}:
            return "outgoing"
        return txt[:20]

    @staticmethod
    def _derive_call_result(duration_val):
        try:
            duration = int(duration_val) if duration_val is not None and str(duration_val).strip() != "" else 0
        except Exception:
            duration = 0
        return "connected" if duration > 0 else "missed"

    @staticmethod
    def stable_bigint(*parts) -> int:
        joined = "|".join("" if p is None else str(p) for p in parts)
        return int(hashlib.sha1(joined.encode("utf-8")).hexdigest()[:15], 16)
