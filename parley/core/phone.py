"""Phone numbers in the E.164 form telephony providers expect ("+919812345678").

Source systems hold numbers however people typed them: "98123 45678",
"098123-45678", "+91 98123 45678". Anything that cannot be read with confidence
returns None, and the customer is not called.
"""

import re

DEFAULT_COUNTRY_CODE = "91"  # India


def to_e164(raw: str | None, country_code: str = DEFAULT_COUNTRY_CODE) -> str | None:
    if not raw:
        return None
    plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if digits.startswith("00"):  # international prefix written as 00
        digits = digits[2:]
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if country_code == "91":
        if len(digits) == 11 and digits.startswith("0"):  # trunk prefix
            digits = digits[1:]
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        # Indian mobile numbers are 10 digits starting 6 to 9.
        return f"+91{digits}" if re.fullmatch(r"[6-9]\d{9}", digits) else None
    return None
