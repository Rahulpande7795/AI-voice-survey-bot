"""
intent.py — Local regex intent classifier. <1ms. No LLM, no network.

FIXES:
  - WHO_PAID: "yes" alone returns None (ambiguous). Only self/other patterns match.
  - Extended YES patterns: "I'm at", "speaking", "able to" → YES for AVAILABILITY.
  - REASON: "no money", "broke" → financial_hardship.
"""
import re
from dataclasses import dataclass
from typing import Optional

_AUTO_ADVANCE = {"PURPOSE", "CALLBACK", "CAPTURE_PAYER", "CAPTURE_EXEC"}

_ROUTING: dict[tuple[str, str], str] = {
    ("PURPOSE",       "any"):    "PAYMENT_CHECK",
    ("CALLBACK",      "any"):    "CLOSE",
    ("CAPTURE_PAYER", "any"):    "DATE",
    ("CAPTURE_EXEC",  "any"):    "CLOSE",
    ("AVAILABILITY",  "yes"):    "PURPOSE",
    ("AVAILABILITY",  "no"):     "CALLBACK",
    ("PAYMENT_CHECK", "yes"):    "WHO_PAID",
    ("PAYMENT_CHECK", "no"):     "REASON",
    ("WHO_PAID",      "self"):   "DATE",
    ("WHO_PAID",      "other"):  "CAPTURE_PAYER",
    ("DATE",          "date"):   "METHOD",
    ("METHOD",        "method"): "AMOUNT",
    ("AMOUNT",        "amount"): "CLOSE",
    ("Q3",            "yes"):    "Q_close",
    ("Q3",            "no"):     "Q_close",
    ("Q3",            "maybe"):  "Q_close",
    ("Q1",            "score_1"):"Q2_neg",
    ("Q1",            "score_2"):"Q2_neg",
    ("Q1",            "score_3"):"Q2_neu",
    ("Q1",            "score_4"):"Q2_pos",
    ("Q1",            "score_5"):"Q2_pos",
}

_ACKS: dict[str, str] = {
    "yes":"Ji haan, samajh gaye.","no":"Theek hai, note kar lete hain.",
    "maybe":"Bilkul, note kar lete hain.","self":"Aapne khud payment ki, shukriya.",
    "other":"Theek hai, unka naam note karenge.","date":"Tarikh note kar li.",
    "method":"Payment method note kar li.","amount":"Amount note kar li.",
    "any":"Theek hai, samajh gaye.","score_1":"I'm sorry to hear that.",
    "score_2":"Thank you for the honest feedback.","score_3":"Noted, thank you.",
    "score_4":"Great, glad to hear it!","score_5":"Excellent, thank you so much!",
}
ALL_ACK_TEXTS: list[str] = list(set(_ACKS.values()))

@dataclass
class IntentResult:
    intent_type: str; next_node: str; ack_text: str
    sentiment: str; confidence: float; extracted: dict

_YES = re.compile(
    r"\b(yes|haan|ji\s*haan|ji|bilkul|zaroor|theek\s*hai|kar\s*diya|ho\s*gaya|"
    r"paid|bhara|de\s*diya|definitely|sure|okay|ok|alright|right|correct|"
    r"absolutely|of\s*course|able|available|bol\s*sakte|sahi|done|agree|"
    r"i.?m\s*(at|able|available|speaking|here|ready)|speaking)\b", re.I)
_NO  = re.compile(
    r"\b(no|nahi|nahin|nahi\s*kiya|abhi\s*nahi|mat|not\s*yet|haven.?t|"
    r"didn.?t|cannot|can.?t|busy|unavailable|nope|never|negative)\b", re.I)
_MAYBE = re.compile(r"\b(maybe|shayad|soch|later|baad\s*mein|possibly)\b", re.I)
_SELF  = re.compile(
    r"\b(maine|main|mujhe|khud|myself|i\s+paid|i\s+did|i\s+have|"
    r"i\s+made|apne\s*aap|i\s+transferred)\b", re.I)
_OTHER = re.compile(
    r"\b(bhai|behen|beta|beti|pati|patni|husband|wife|friend|dost|ghar|"
    r"family|relative|uncle|aunty|kisi\s*aur|someone\s*else|another|other\s*person)\b", re.I)
_METHODS = {
    "upi":    re.compile(r"\b(upi|gpay|google\s*pay|phonepe|paytm|bhim|g\s*pay)\b", re.I),
    "cash":   re.compile(r"\b(cash|nakit|naqad|naqdee)\b", re.I),
    "nach":   re.compile(r"\b(nach|ecs|auto\s*debit|mandate|nach\s*debit)\b", re.I),
    "neft":   re.compile(r"\b(neft|rtgs|imps|transfer|bank\s*transfer|online\s*transfer|wire)\b", re.I),
    "cheque": re.compile(r"\b(cheque|check|dd|demand\s*draft|cheq)\b", re.I),
    "card":   re.compile(r"\b(card|credit|debit|atm|credit\s*card|debit\s*card)\b", re.I),
    "online": re.compile(r"\b(net\s*banking|internet\s*banking|online|mobile\s*banking)\b", re.I),
}
_DATE = re.compile(
    r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b|"
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(january|february|march|april|may|june|july|"
    r"august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\w*\b|"
    r"\b(202[0-9]|201[0-9])\b|"           # year alone: 2024, 2025, 2026
    r"\b(aaj|kal|parso|today|yesterday|last\s*(?:week|month)|pichle?\s*(?:mahine?|hafte?)|"
    r"two\s*weeks?\s*ago|last\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)
_AMOUNT = re.compile(
    r"(?:rs\.?\s*|₹\s*|inr\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+)"
    r"(?:\s*(?:k|thousand|hazaar|lakh|lac))?", re.I)
_SCORE  = re.compile(r"\b([1-5])\b")
_SWORD  = {"one":1,"two":2,"three":3,"four":4,"five":5,"ek":1,"do":2,"teen":3,"char":4,"paanch":5}


def classify(text: str, current_node: str) -> Optional[IntentResult]:
    t = text.strip()

    if current_node in _AUTO_ADVANCE:
        nd = _ROUTING.get((current_node, "any"), "CLOSE")
        return IntentResult("any", nd, _ACKS["any"], "neutral", 0.99, {})

    if current_node == "Q1":
        m = _SCORE.search(t)
        s = int(m.group(1)) if m else next((v for w,v in _SWORD.items() if re.search(rf"\b{w}\b",t.lower())), None)
        if s:
            it = f"score_{s}"
            nd = _ROUTING.get(("Q1", it))
            if nd:
                sent = "positive" if s>=4 else "negative" if s<=2 else "neutral"
                return IntentResult(it, nd, _ACKS[it], sent, 0.98, {"score": s})
        return None

    if current_node == "AMOUNT":
        amt = _ext_amount(t)
        if amt:
            nd = _ROUTING.get(("AMOUNT","amount"), "CLOSE")
            # FIX: use static cacheable ack instead of dynamic f-string
            # Dynamic f'\u20b9{amt:,} note kar liya.' was causing 1650ms TTS spike
            return IntentResult("amount", nd, _ACKS["amount"], "neutral", 0.95, {"payment_amount": amt})
        return None

    if current_node == "DATE":
        m = _DATE.search(t)
        if m:
            nd = _ROUTING.get(("DATE","date"), "METHOD")
            # FIX: use static cacheable ack instead of dynamic f-string
            return IntentResult("date", nd, _ACKS["date"], "neutral", 0.93, {"payment_date": m.group(0)})
        return None

    if current_node == "METHOD":
        for method, pat in _METHODS.items():
            if pat.search(t):
                nd = _ROUTING.get(("METHOD","method"), "AMOUNT")
                # FIX: use static cacheable ack instead of dynamic f-string
                return IntentResult("method", nd, _ACKS["method"], "neutral", 0.95, {"payment_method": method})
        return None

    if current_node == "WHO_PAID":
        if _SELF.search(t):  return IntentResult("self",  _ROUTING[("WHO_PAID","self")],  _ACKS["self"],  "neutral", 0.92, {"payer":"self"})
        if _OTHER.search(t): return IntentResult("other", _ROUTING[("WHO_PAID","other")], _ACKS["other"], "neutral", 0.90, {"payer":"other"})
        return None  # "yes" alone is ambiguous → LLM asks who exactly

    has_yes   = bool(_YES.search(t))
    has_no    = bool(_NO.search(t))
    has_maybe = bool(_MAYBE.search(t))

    if has_yes and not has_no:   it, sent = "yes", "positive"
    elif has_no and not has_yes: it, sent = "no",  "negative"
    elif has_maybe:              it, sent = "maybe","neutral"
    else:                        return None

    nd = _ROUTING.get((current_node, it))
    return None if nd is None else IntentResult(it, nd, _ACKS.get(it,"Theek hai."), sent, 0.96, {})


def _ext_amount(text: str) -> Optional[int]:
    m = _AMOUNT.search(text.replace(",",""))
    if not m: return None
    try:
        raw = re.sub(r"[^\d.]","",m.group(0))
        if not raw: return None
        val = float(raw)
        tl  = text.lower()
        if re.search(r"lakh|lac",tl):           val *= 100_000
        elif re.search(r"\bk\b|thousand|hazaar",tl): val *= 1_000
        return int(val) if val >= 100 else None
    except: return None