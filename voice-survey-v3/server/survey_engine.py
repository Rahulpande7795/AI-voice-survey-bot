"""
survey_engine.py — TASK 4: L&T Finance payment verification call script.
Supports Hindi and English via SURVEY_LANGUAGE env var.

Call flow:
  INTRO → AVAILABILITY → PURPOSE → PAYMENT_CHECK
      ├─ yes → WHO_PAID → DATE → METHOD → AMOUNT → CLOSE
      └─ no  → REASON → CAPTURE_EXEC → CLOSE
  CALLBACK: schedule callback (terminal-ish)
"""
import os
import re
from typing import Optional

SURVEY_LANG = os.getenv("SURVEY_LANGUAGE", "en")  # "en" or "hi"

# ── Node definitions ─────────────────────────────────────────────────────
# Each node: text (spoken), next_nodes (condition → next node id)

_NODES_EN = {
    "INTRO": {
        "text": (
            "Hello, I am calling from L&T Finance. "
            "This is an automated payment verification call. "
            "Your loan account number is on record. "
            "This call may be recorded for quality purposes."
        ),
        "next": {"default": "AVAILABILITY"},
    },
    "AVAILABILITY": {
        "text": "Am I speaking with the loan account holder? Are you available to speak for 2 minutes?",
        "next": {"yes": "PURPOSE", "no": "CALLBACK"},
    },
    "CALLBACK": {
        "text": "No problem. When would be a convenient time to call you back? Please share a preferred time.",
        "next": {"default": "CLOSE"},
        "terminal": False,
    },
    "PURPOSE": {
        "text": (
            "This call is regarding your L&T Finance loan EMI. "
            "Our records show a payment was due recently on your account. "
            "We are calling to verify the payment status."
        ),
        "next": {"default": "PAYMENT_CHECK"},
    },
    "PAYMENT_CHECK": {
        "text": "Has the EMI payment been made for this month?",
        "next": {"yes": "WHO_PAID", "no": "REASON"},
    },
    "WHO_PAID": {
        "text": "Thank you. Who made the payment — was it you yourself, or someone else from your family?",
        "next": {"self": "DATE", "other": "CAPTURE_PAYER"},
    },
    "CAPTURE_PAYER": {
        "text": "Could you please share the name of the person who made the payment?",
        "next": {"default": "DATE"},
    },
    "DATE": {
        "text": "On which date was the payment made? Please share the exact date.",
        "next": {"default": "METHOD"},
    },
    "METHOD": {
        "text": "How was the payment made — UPI, cash, NACH auto-debit, NEFT, cheque, or card?",
        "next": {"default": "AMOUNT"},
    },
    "AMOUNT": {
        "text": "What was the exact amount paid in rupees?",
        "next": {"default": "CLOSE"},
    },
    "REASON": {
        "text": (
            "I understand. Could you please share the reason why the payment "
            "could not be made this month?"
        ),
        "next": {"default": "CAPTURE_EXEC"},
    },
    "CAPTURE_EXEC": {
        "text": (
            "Thank you for letting us know. Could you please share the best contact number "
            "or executive name at your nearest L&T Finance branch, if any?"
        ),
        "next": {"default": "CLOSE"},
    },
    "CLOSE": {
        "text": (
            "Thank you for your time. Your response has been recorded. "
            "If you have any queries, please contact L&T Finance customer care. "
            "Have a good day. Goodbye."
        ),
        "next": {},
        "terminal": True,
    },
}

_NODES_HI = {
    "INTRO": {
        "text": (
            "Namaste, main L&T Finance se call kar raha hoon. "
            "Yeh ek automated payment verification call hai. "
            "Aapka loan account number hamare records mein hai. "
            "Yeh call quality purposes ke liye record ho sakti hai."
        ),
        "next": {"default": "AVAILABILITY"},
    },
    "AVAILABILITY": {
        "text": "Kya main loan account holder se baat kar raha hoon? Kya aap 2 minute baat kar sakte hain?",
        "next": {"yes": "PURPOSE", "no": "CALLBACK"},
    },
    "CALLBACK": {
        "text": "Koi baat nahi. Aap kab free honge? Main us time call karta hoon.",
        "next": {"default": "CLOSE"},
        "terminal": False,
    },
    "PURPOSE": {
        "text": (
            "Aapke L&T Finance loan ki EMI ke baare mein baat karni thi. "
            "Hamare records ke anusaar, is mahine aapke account mein ek payment due thi. "
            "Hum payment status verify karne ke liye call kar rahe hain."
        ),
        "next": {"default": "PAYMENT_CHECK"},
    },
    "PAYMENT_CHECK": {
        "text": "Kya is mahine ki EMI payment ho gayi hai?",
        "next": {"yes": "WHO_PAID", "no": "REASON"},
    },
    "WHO_PAID": {
        "text": "Shukriya. Payment kisne ki — aapne khud, ya ghar mein kisi aur ne?",
        "next": {"self": "DATE", "other": "CAPTURE_PAYER"},
    },
    "CAPTURE_PAYER": {
        "text": "Unka naam bata sakte hain jo payment ki?",
        "next": {"default": "DATE"},
    },
    "DATE": {
        "text": "Payment kis tarikh ko ki gayi? Exact date batayein.",
        "next": {"default": "METHOD"},
    },
    "METHOD": {
        "text": "Payment kaise ki — UPI, cash, NACH auto-debit, NEFT, cheque, ya card se?",
        "next": {"default": "AMOUNT"},
    },
    "AMOUNT": {
        "text": "Kitne rupaye ki payment ki gayi?",
        "next": {"default": "CLOSE"},
    },
    "REASON": {
        "text": "Samajh gaye. Is mahine payment kyun nahi ho payi, kya karan tha?",
        "next": {"default": "CAPTURE_EXEC"},
    },
    "CAPTURE_EXEC": {
        "text": (
            "Shukriya batane ke liye. Aapke nearest L&T Finance branch mein "
            "koi contact number ya executive ka naam hai jo share kar saken?"
        ),
        "next": {"default": "CLOSE"},
    },
    "CLOSE": {
        "text": (
            "Bahut shukriya aapka. Aapka jawab record ho gaya hai. "
            "Koi bhi sawaal ho toh L&T Finance customer care se sampark karein. "
            "Aapka din shubh rahe. Dhanyawaad."
        ),
        "next": {},
        "terminal": True,
    },
}

NODES = _NODES_HI if SURVEY_LANG == "hi" else _NODES_EN

# ── Opening context lines (shown in UI intro card) ────────────────────────
INTRO_LINES_EN = [
    "This is an automated L&T Finance payment verification call.",
    "The bot will ask about your EMI payment status for this month.",
    "You will be asked: payment date, method (UPI/cash/NACH), and amount.",
    "Please speak clearly. The call takes about 60 seconds.",
]
INTRO_LINES_HI = [
    "Yeh L&T Finance ka automated payment verification call hai.",
    "Bot aapki is mahine ki EMI payment ke baare mein poochega.",
    "Aapse payment ki tarikh, tarika (UPI/cash/NACH), aur amount poochi jayegi.",
    "Saaf bolein. Yeh call lagbhag 60 second mein poori ho jayegi.",
]
INTRO_LINES = INTRO_LINES_HI if SURVEY_LANG == "hi" else INTRO_LINES_EN
INTRO_TEXT  = " ".join(INTRO_LINES)

START_NODE  = "INTRO"

# All node texts + intro — pre-warmed into TTS cache at startup
ALL_PHRASES = [INTRO_TEXT] + [n["text"] for n in NODES.values()]


# ── Public API ────────────────────────────────────────────────────────────

def get_question(node: str) -> str:
    return NODES.get(node, NODES["CLOSE"])["text"]


def is_terminal(node: str) -> bool:
    return bool(NODES.get(node, {}).get("terminal", False))


def next_node(current: str, intent_type: str = "default") -> str:
    """
    Resolve next node from current node + intent_type.
    Intent types: "yes" | "no" | "self" | "other" | "default"
    Falls back to "default" if specific intent not in routing table.
    """
    node_def = NODES.get(current, {})
    nexts    = node_def.get("next", {})
    result   = nexts.get(intent_type) or nexts.get("default") or "CLOSE"
    return result


def detect_sentiment(text: str) -> str:
    t = text.lower()
    pos = {"paid","ho\s*gaya","kar\s*diya","yes","haan","bilkul","shukriya","great","good"}
    neg = {"nahi","nahin","no","problem","nahi\s*kar","sorry","issue","late"}
    p = sum(1 for w in pos if re.search(rf"\b{w}\b", t))
    n = sum(1 for w in neg if re.search(rf"\b{w}\b", t))
    if p > n: return "positive"
    if n > p: return "negative"
    return "neutral"