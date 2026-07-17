from dataclasses import dataclass
from typing import Iterable, Set, Optional

try:
    from app.transforms.normalizers import normalize_phone_to_10, normalize_email
except ImportError:
    def normalize_phone_to_10(phone: Optional[str]) -> Optional[str]:
        if not phone:
            return None
        return phone.strip()

    def normalize_email(email: Optional[str]) -> Optional[str]:
        if not email:
            return None
        return email.strip().lower()


@dataclass(frozen=True)
class UserIdentifiers:
    phones: Set[str]
    emails: Set[str]
    wa_nums: Set[str]

    @classmethod
    def from_raw(
        cls,
        phones: Iterable[Optional[str]] = (),
        emails: Iterable[Optional[str]] = (),
        wa_nums: Iterable[Optional[str]] = (),
        treat_phone_as_wa: bool = True,
    ) -> "UserIdentifiers":
        """Build a normalized identifier set from raw values."""
        norm_phones = {
            p_norm
            for p in phones
            if (p_norm := normalize_phone_to_10(p))  
        }
        norm_emails = {
            e_norm
            for e in emails
            if (e_norm := normalize_email(e))
        }
        norm_wa = {
            w_norm
            for w in wa_nums
            if (w_norm := normalize_phone_to_10(w))
        }

        if treat_phone_as_wa:
            norm_wa |= norm_phones

        return cls(
            phones=norm_phones,
            emails=norm_emails,
            wa_nums=norm_wa,
        )

    def is_empty(self) -> bool:
        return not (self.phones or self.emails or self.wa_nums)
