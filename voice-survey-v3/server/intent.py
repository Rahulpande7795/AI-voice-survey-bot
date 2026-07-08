"""
intent.py — Fast-path intent classifier. Zero network calls. Runs in <1ms.

HOW IT WORKS (simple terms):
  Before asking the LLM "what did the user mean?", we first check if we
  can figure it out ourselves with simple pattern matching:
  - "haan" / "yes" / "bilkul" → the user said YES
  - "nahi" / "no" / "nope"   → the user said NO
  - "5000"                    → the user said an amount
  - "UPI se"                  → the user said a payment method

  If we can classify it locally, we skip the LLM entirely (saves 1300ms).
  If we can't, we pass it to the semantic cache, then the LLM.

DECISION ORDER in pipeline.py:
  intent classifier (<1ms) → semantic cache (~5ms) → Groq LLM (~1300ms)

AUTO-ADVANCE NODES:
  Some nodes (PURPOSE, CALLBACK, CAPTURE_PAYER, CAPTURE_EXEC) are
  informational — the customer just listens and says "okay" or anything.
  We don't need to understand the answer, just move on. Intent classifier
  handles ALL responses on these nodes without touching the LLM.
"""
import re
from dataclasses import dataclass
from typing import Optional

# ── Nodes that auto-advance on ANY utterance ───────────────────────────────
# These are informational — customer just acknowledges, no branching needed.
_AUTO_ADVANCE_NODES = {"PURPOSE", "CALLBACK", "CAPTURE_PAYER", "CAPTURE_EXEC"}

# ── Routing: (current_node, intent_type) → next_node ──────────────────────
_ROUTING: dict[tuple[str, str], str] = {
    # Auto-advance (any utterance advances)
    ("PURPOSE",        "any"):    "PAYMENT_CHECK",
    ("CALLBACK",       "any"):    "CLOSE",
    ("CAPTURE_PAYER",  "any"):    "DATE",
    ("CAPTURE_EXEC",   "any"):    "CLOSE",
    # Availability
    ("AVAILABILITY",   "yes"):    "PURPOSE",
    ("AVAILABILITY",   "no"):     "CALLBACK",
    # Payment check
    ("PAYMENT_CHECK",  "yes"):    "WHO_PAID",
    ("PAYMENT_CHECK",  "no"):     "REASON",
    # Who paid
    ("WHO_PAID",       "self"):   "DATE",
    ("WHO_PAID",       "other"):  "CAPTURE_PAYER",
    # Structured data
    ("DATE",           "date"):   "METHOD",
    ("METHOD",         "method"): "AMOUNT",
    ("AMOUNT",         "amount"): "CLOSE",
    # Survey compat (Q1-Q3)
    ("Q3",             "yes"):    "Q_close",
    ("Q3",             "no"):     "Q_close",
    ("Q3",             "maybe"):  "Q_close",
    ("Q1",             "score_1"):"Q2_neg",
    ("Q1",             "score_2"):"Q2_neg",
    ("Q1",             "score_3"):"Q2_neu",
    ("Q1",             "score_4"):"Q2_pos",
    ("Q1",             "score_5"):"Q2_pos",
}

# ── Acknowledgement text per intent ───────────────────────────────────────
_ACKS: dict[str, str] = {
    "yes":     "Ji haan, samajh gaye.",
    "no":      "Theek hai, note kar lete hain.",
    "maybe":   "Bilkul, note kar lete hain.",
    "self":    "Aapne khud payment ki, shukriya.",
    "other":   "Theek hai, unka naam note karenge.",
    "date":    "Tarikh note kar li.",
    "method":  "Payment method note kar li.",
    "amount":  "Amount note kar li.",
    "any":     "Theek hai, samajh gaye.",
    "score_1": "I'm sorry to hear that.",
    "score_2": "Thank you for the honest feedback.",
    "score_3": "Noted, thank you.",
    "score_4": "Great, glad to hear it!",
    "score_5": "Excellent, thank you so much!",
}

# Exported so main.py can pre-warm TTS cache for all ack texts
ALL_ACK_TEXTS: list[str] = list(set(_ACKS.values()))


@dataclass
class IntentResult:
    intent_type: str
    next_node:   str
    ack_text:    str
    sentiment:   str
    confidence:  float
    extracted:   dict


# ── Patterns ───────────────────────────────────────────────────────────────
_YES = re.compile(
    r"\b(yes|haan|ji\s*haan|bilkul|zaroor|theek\s*hai|kar\s*diya|ho\s*gaya|"
    r"paid|bhara|de\s*diya|definitely|sure|okay|ok|able|available|speak|"
    r"bol\s*sakte|ha|haa)\b", re.I)
_NO  = re.compile(
    r"\b(no|nahi|nahin|nahi\s*kiya|abhi\s*nahi|mat|not\s*yet|haven.?t|"
    r"didn.?t|cannot|can.?t|busy|unavailable|nope)\b", re.I)
_MAYBE = re.compile(r"\b(maybe|shayad|soch|later|baad\s*mein)\b", re.I)
_SELF  = re.compile(r"\b(maine|main|mujhe|khud|myself|i\s+paid|i\s+did|apne\s*aap)\b", re.I)
_OTHER = re.compile(
    r"\b(bhai|behen|beta|beti|pati|patni|friend|dost|ghar|family|"
    r"relative|uncle|aunty|kisi\s*aur|someone\s*else)\b", re.I)

_METHODS = {
    "upi":    re.compile(r"\b(upi|gpay|google\s*pay|phonepe|paytm|bhim)\b", re.I),
    "cash":   re.compile(r"\b(cash|nakit|naqad)\b", re.I),
    "nach":   re.compile(r"\b(nach|ecs|auto\s*debit|auto\s*pay|mandate)\b", re.I),
    "neft":   re.compile(r"\b(neft|rtgs|imps|transfer|bank\s*transfer)\b", re.I),
    "cheque": re.compile(r"\b(cheque|check|dd|demand\s*draft)\b", re.I),
    "card":   re.compile(r"\b(card|credit|debit|atm)\b", re.I),
}
_DATE = re.compile(
    r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b|"
    r"\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|"
    r"\b(aaj|kal|parso|today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I)
_SCORE = re.compile(r"\b([1-5])\b")
_SCORE_WORDS = {
    "one":1,"two":2,"three":3,"four":4,"five":5,
    "ek":1,"do":2,"teen":3,"char":4,"paanch":5,
}
_AMOUNT = re.compile(
    r"(?:rs\.?\s*|₹\s*|inr\s*)?(\d{1,3}(?:,\d{3})*|\d+)"
    r"(?:\s*(?:k|thousand|hazaar|lakh|lac))?", re.I)


# ── Main classifier ────────────────────────────────────────────────────────

def classify(text: str, current_node: str) -> Optional[IntentResult]:
    """
    Try to classify the utterance locally. Returns IntentResult or None.
    None means: try semantic cache, then LLM.
    """
    t = text.strip()

    # 1. Auto-advance nodes — ANY response moves forward, no LLM needed
    if current_node in _AUTO_ADVANCE_NODES:
        nd = _ROUTING.get((current_node, "any"), "CLOSE")
        return IntentResult("any", nd, _ACKS["any"], "neutral", 0.99, {})

    # 2. Survey rating score (Q1)
    if current_node == "Q1":
        s = _extract_score(t)
        if s:
            it = f"score_{s}"
            nd = _ROUTING.get(("Q1", it))
            if nd:
                sent = "positive" if s >= 4 else "negative" if s <= 2 else "neutral"
                return IntentResult(it, nd, _ACKS[it], sent, 0.98, {"score": s})
        return None

    # 3. Rupee amount
    if current_node == "AMOUNT":
        amt = _extract_amount(t)
        if amt:
            nd = _ROUTING.get(("AMOUNT", "amount"), "CLOSE")
            return IntentResult("amount", nd,
                                f"₹{amt:,} note kar liya. Shukriya.",
                                "neutral", 0.95, {"payment_amount": amt})
        return None

    # 4. Date
    if current_node == "DATE":
        m = _DATE.search(t)
        if m:
            nd = _ROUTING.get(("DATE", "date"), "METHOD")
            return IntentResult("date", nd,
                                f"Theek hai, {m.group(0)} note kar li.",
                                "neutral", 0.93, {"payment_date": m.group(0)})
        return None

    # 5. Payment method
    if current_node == "METHOD":
        for method, pat in _METHODS.items():
            if pat.search(t):
                nd = _ROUTING.get(("METHOD", "method"), "AMOUNT")
                return IntentResult("method", nd,
                                    f"{method.upper()} se payment, note kar li.",
                                    "neutral", 0.95, {"payment_method": method})
        return None

    # 6. Who paid
    if current_node == "WHO_PAID":
        if _SELF.search(t):
            nd = _ROUTING.get(("WHO_PAID", "self"), "DATE")
            return IntentResult("self", nd, _ACKS["self"], "neutral", 0.92, {"payer": "self"})
        if _OTHER.search(t):
            nd = _ROUTING.get(("WHO_PAID", "other"), "CAPTURE_PAYER")
            return IntentResult("other", nd, _ACKS["other"], "neutral", 0.90, {"payer": "other"})
        return None

    # 7. Yes / No / Maybe for all other nodes
    has_yes   = bool(_YES.search(t))
    has_no    = bool(_NO.search(t))
    has_maybe = bool(_MAYBE.search(t))

    if has_yes and not has_no:     it, sent = "yes",   "positive"
    elif has_no and not has_yes:   it, sent = "no",    "negative"
    elif has_maybe:                it, sent = "maybe", "neutral"
    else:                          return None

    nd = _ROUTING.get((current_node, it))
    if nd is None:
        return None

    return IntentResult(it, nd, _ACKS.get(it, "Theek hai."), sent, 0.96, {})


def _extract_score(text: str) -> Optional[int]:
    m = _SCORE.search(text)
    if m: return int(m.group(1))
    t = text.lower()
    for word, num in _SCORE_WORDS.items():
        if re.search(rf"\b{word}\b", t): return num
    return None


def _extract_amount(text: str) -> Optional[int]:
    m = _AMOUNT.search(text.replace(",", ""))
    if not m: return None
    try:
        val = float(re.sub(r"[^\d.]", "", m.group(0)))
        t   = text.lower()
        if re.search(r"lakh|lac", t):              val *= 100_000
        elif re.search(r"\bk\b|thousand|hazaar", t): val *= 1_000
        return int(val) if val >= 100 else None
    except (ValueError, TypeError):
        return None