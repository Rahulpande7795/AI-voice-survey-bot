"""
extractor.py — TASK 5: Structured data extraction per survey node.

Called after every turn. Extracted values are stored in session.data_capture.
All extraction is local (regex + lookup), zero network cost.
"""
import re
from typing import Optional

# ── Month lookup ──────────────────────────────────────────────────────────
_MONTHS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

_PAYER_MAP = {
    "self":   re.compile(r"\b(maine|main|khud|myself|i\s+paid|i\s+did|mujhe)\b", re.I),
    "spouse": re.compile(r"\b(pati|patni|husband|wife|spouse)\b", re.I),
    "parent": re.compile(r"\b(papa|mummy|maa|baap|father|mother|parents)\b", re.I),
    "child":  re.compile(r"\b(beta|beti|son|daughter|bacha)\b", re.I),
    "sibling":re.compile(r"\b(bhai|behen|brother|sister)\b", re.I),
    "friend": re.compile(r"\b(dost|friend|yaar)\b", re.I),
    "other":  re.compile(r"\b(kisi\s*aur|someone|relative|uncle|aunty|neighbour)\b", re.I),
}

_METHOD_MAP = {
    "upi":    re.compile(r"\b(upi|gpay|google\s*pay|phonepe|paytm|bhim)\b", re.I),
    "cash":   re.compile(r"\b(cash|nakit|naqad|haath\s*se)\b", re.I),
    "nach":   re.compile(r"\b(nach|ecs|auto\s*debit|auto\s*pay|mandate)\b", re.I),
    "neft":   re.compile(r"\b(neft|rtgs|imps|transfer|bank\s*transfer|online)\b", re.I),
    "cheque": re.compile(r"\b(cheque|check|dd|demand\s*draft)\b", re.I),
    "card":   re.compile(r"\b(card|credit|debit|atm)\b", re.I),
}

_REASON_KEYWORDS = {
    "financial_hardship": re.compile(r"\b(paisa\s*nahi|job\s*(gai|chali)|salary\s*nahi|unemployment|bimaar|hospital|medical)\b", re.I),
    "forgot":             re.compile(r"\b(bhool|forgot|yaad\s*nahi|missed)\b", re.I),
    "technical":          re.compile(r"\b(net\s*nahi|server|technical|app|bounce|fail)\b", re.I),
    "dispute":            re.compile(r"\b(galat|wrong|dispute|agree\s*nahi)\b", re.I),
}


def extract(node: str, transcript: str) -> dict:
    """
    Extract structured data from transcript based on the current survey node.
    Returns a dict (possibly empty) to merge into session.data_capture.
    """
    t = transcript.strip()
    result: dict = {}

    if node == "DATE":
        d = _extract_date(t)
        if d:
            result["payment_date"] = d

    elif node == "AMOUNT":
        a = _extract_amount(t)
        if a:
            result["payment_amount"] = a

    elif node == "METHOD":
        m = _extract_method(t)
        if m:
            result["payment_method"] = m

    elif node == "WHO_PAID":
        p = _extract_payer(t)
        if p:
            result["payer"] = p

    elif node == "REASON":
        r = _extract_reason(t)
        result["nonpayment_reason"] = r or "unspecified"
        result["reason_raw"] = t

    elif node == "CAPTURE_PAYER":
        # Just store raw name — too unstructured for regex
        result["payer_name"] = t

    elif node == "CAPTURE_EXEC":
        result["exec_contact"] = t

    # Always store raw transcript for every node
    result[f"{node.lower()}_raw"] = t

    return result


# ── Extraction helpers ────────────────────────────────────────────────────

def _extract_date(text: str) -> Optional[str]:
    # Numeric: 15/4, 15-04-2024
    m = re.search(r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        yr = m.group(3)
        yr_str = f"/{yr}" if yr else ""
        return f"{d:02d}/{mo:02d}{yr_str}"

    # Word month: "15 January", "teen February"
    m = re.search(
        r"\b(\d{1,2}|ek|do|teen|char|paanch|chhe|saat|aath|nau|das)\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        text, re.I,
    )
    if m:
        day_raw = m.group(1)
        day = int(day_raw) if day_raw.isdigit() else _HINDI_NUMS.get(day_raw, 1)
        month = _MONTHS.get(m.group(2).lower()[:3], 0)
        return f"{day:02d}/{month:02d}"

    # Relative: kal, aaj, parso
    m = re.search(r"\b(aaj|kal|parso|today|yesterday)\b", text, re.I)
    if m:
        return m.group(0).lower()

    return None


_HINDI_NUMS = {
    "ek":1,"do":2,"teen":3,"char":4,"paanch":5,
    "chhe":6,"saat":7,"aath":8,"nau":9,"das":10,
}


def _extract_amount(text: str) -> Optional[int]:
    t = text.replace(",", "")
    # Numeric with optional multiplier
    m = re.search(r"(?:rs\.?\s*|₹\s*|inr\s*)?(\d+(?:\.\d+)?)\s*(k|thousand|hazaar|lakh|lac)?", t, re.I)
    if m:
        val = float(m.group(1))
        mult = m.group(2) or ""
        if re.match(r"lakh|lac", mult, re.I): val *= 100_000
        elif re.match(r"k|thousand|hazaar", mult, re.I): val *= 1_000
        if val >= 100:  # ignore tiny noise like "1" from "1 second"
            return int(val)
    return None


def _extract_method(text: str) -> Optional[str]:
    for method, pat in _METHOD_MAP.items():
        if pat.search(text):
            return method
    return None


def _extract_payer(text: str) -> Optional[str]:
    for payer, pat in _PAYER_MAP.items():
        if pat.search(text):
            return payer
    return None


def _extract_reason(text: str) -> Optional[str]:
    for reason, pat in _REASON_KEYWORDS.items():
        if pat.search(text):
            return reason
    return None