"""extractor.py — Structured data extraction per node. Zero network cost."""
import re
from typing import Optional

_PAYER = {
    "self":   re.compile(r"\b(maine|main|khud|myself|i\s+paid|i\s+did|i\s+made|apne\s*aap)\b",re.I),
    "spouse": re.compile(r"\b(pati|patni|husband|wife|spouse)\b",re.I),
    "parent": re.compile(r"\b(papa|mummy|maa|baap|father|mother)\b",re.I),
    "sibling":re.compile(r"\b(bhai|behen|brother|sister)\b",re.I),
    "friend": re.compile(r"\b(dost|friend|yaar)\b",re.I),
    "other":  re.compile(r"\b(kisi\s*aur|someone|relative|uncle|aunty)\b",re.I),
}
_METHOD = {
    "upi":    re.compile(r"\b(upi|gpay|google\s*pay|phonepe|paytm|bhim)\b",re.I),
    "cash":   re.compile(r"\b(cash|nakit|naqad)\b",re.I),
    "nach":   re.compile(r"\b(nach|ecs|auto\s*debit|mandate)\b",re.I),
    "neft":   re.compile(r"\b(neft|rtgs|imps|transfer|bank\s*transfer|online)\b",re.I),
    "cheque": re.compile(r"\b(cheque|check|dd|demand\s*draft)\b",re.I),
    "card":   re.compile(r"\b(card|credit|debit|atm)\b",re.I),
}
_REASON = {
    "financial_hardship": re.compile(
        r"\b(no\s*money|paise\s*nahi|koi\s*paisa|funds?\s*nahi|paisa\s*khatam|broke|"
        r"afford\s*nahi|financial|job\s*(gai|chali|nahi)|salary\s*nahi|"
        r"bimaar|hospital|medical|loss|garibo|kami)\b",re.I),
    "forgot":   re.compile(r"\b(bhool|forgot|yaad\s*nahi|missed)\b",re.I),
    "technical":re.compile(r"\b(net\s*nahi|server|technical|app|bounce|fail)\b",re.I),
    "dispute":  re.compile(r"\b(galat|wrong|dispute|agree\s*nahi)\b",re.I),
    "busy":     re.compile(r"\b(busy|time\s*nahi|kaam|occupied|travel)\b",re.I),
}
_DATE=re.compile(
    r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b|"
    r"\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|"
    r"\b(aaj|kal|parso|today|yesterday)\b",re.I)

def extract(node:str,transcript:str)->dict:
    t=transcript.strip(); result: dict={}
    if node=="DATE":
        m=_DATE.search(t)
        if m: result["payment_date"]=m.group(0)
    elif node=="AMOUNT":
        a=_ext_amount(t)
        if a: result["payment_amount"]=a
    elif node=="METHOD":
        for m,p in _METHOD.items():
            if p.search(t): result["payment_method"]=m; break
    elif node=="WHO_PAID":
        for p,pat in _PAYER.items():
            if pat.search(t): result["payer"]=p; break
    elif node=="REASON":
        for r,pat in _REASON.items():
            if pat.search(t): result["nonpayment_reason"]=r; break
        if "nonpayment_reason" not in result: result["nonpayment_reason"]="unspecified"
        result["reason_raw"]=t
    elif node=="CAPTURE_PAYER":  result["payer_name"]=t
    elif node=="CAPTURE_EXEC":   result["exec_contact"]=t
    result[f"{node.lower()}_raw"]=t
    return result

def _ext_amount(text:str)->Optional[int]:
    m=re.search(r"(?:rs\.?\s*|₹\s*|inr\s*)?(\d+(?:\.\d+)?)\s*(k|thousand|hazaar|lakh|lac)?",
                text.replace(",",""),re.I)
    if not m: return None
    try:
        val=float(m.group(1))
        mult=m.group(2) or ""
        if re.match(r"lakh|lac",mult,re.I):           val*=100_000
        elif re.match(r"k|thousand|hazaar",mult,re.I): val*=1_000
        return int(val) if val>=100 else None
    except: return None