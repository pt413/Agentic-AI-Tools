from typing import Optional

def parse_direction(direction: Optional[str]) -> Optional[bool]:
    """
    Parse a direction string into True (outgoing), False (incoming), or None (unknown).
    Mirrors the behavior in your original _is_outgoing but is slightly more permissive
    in accepted tokens while preserving exact outcomes:

    Returns:
      - True for outgoing variants: startswith "out", or equals "sent", "sender", "outgoing"
      - False for incoming variants: startswith "in", or equals "received", "incoming", "inbound"
      - None for anything else (unknown)
    """
    if not direction:
        return None
    d = direction.strip().lower()
   
    d = d.replace("_", " ").replace("-", " ").strip()

    if not d:
        return None
    if d.startswith("out") or d in ("sent", "sender", "outgoing", "sent by us"):
        return True
    if d.startswith("in") or d in ("received", "incoming", "inbound", "received by us"):
        return False
    return None
