"""
survey_engine.py — Survey node definitions, branching logic, sentiment detection.

Tree:
    INTRO → Q1 (rate 1-5)
        1-2  → Q2_neg
        3    → Q2_neu
        4-5  → Q2_pos
    Q2_* → Q3 (recommend?)
    Q3   → Q_close (farewell)
"""
import re
from typing import Optional

# ── Opening context spoken before Q1 ─────────────────────────────────────────
INTRO_LINES = [
    "Hi there! Welcome to our quick customer satisfaction survey.",
    "This will only take about 60 seconds — just 3 short questions.",
    "You'll be rating your recent experience with our service, "
    "telling us what stood out, and whether you'd recommend us.",
    "Speak naturally after each question. Let's get started.",
]
INTRO_TEXT = " ".join(INTRO_LINES)

NODES: dict[str, str] = {
    "Q1":      "On a scale of 1 to 5, how satisfied are you with our service today? 1 is very poor, 5 is excellent.",
    "Q2_pos":  "That's wonderful to hear! What specifically made your experience so great today?",
    "Q2_neg":  "We're really sorry to hear that. Could you tell us what went wrong so we can improve?",
    "Q2_neu":  "We appreciate your honesty. What one thing would most improve your experience with us?",
    "Q3":      "Based on your experience, would you recommend us to a friend or colleague? Please say yes, no, or maybe.",
    "Q_close": "Thank you so much for your time. Your feedback genuinely helps us get better every day. Have a great one!",
}

TERMINAL_NODE = "Q_close"
ALL_PHRASES   = [INTRO_TEXT] + list(NODES.values())


def get_question(node: str) -> str:
    return NODES.get(node, NODES[TERMINAL_NODE])

def is_terminal(node: str) -> bool:
    return node == TERMINAL_NODE

def next_node(current: str, answer: str, sentiment: str = "neutral") -> str:
    if current == "Q1":
        score = _extract_score(answer)
        if score is not None:
            if score <= 2:   return "Q2_neg"
            elif score >= 4: return "Q2_pos"
            else:            return "Q2_neu"
        if sentiment == "positive": return "Q2_pos"
        if sentiment == "negative": return "Q2_neg"
        return "Q2_neu"
    if current in ("Q2_pos", "Q2_neg", "Q2_neu"): return "Q3"
    if current == "Q3":                            return "Q_close"
    return TERMINAL_NODE

def detect_sentiment(text: str) -> str:
    t = text.lower()
    pos = {"great","good","excellent","amazing","love","wonderful","fantastic",
           "happy","satisfied","best","perfect","awesome","brilliant","superb"}
    neg = {"bad","terrible","horrible","worst","hate","awful","disappointed",
           "poor","wrong","broken","issue","problem","slow","rude","useless"}
    p = sum(1 for w in pos if w in t)
    n = sum(1 for w in neg if w in t)
    if p > n: return "positive"
    if n > p: return "negative"
    return "neutral"

def _extract_score(text: str) -> Optional[int]:
    m = re.search(r"\b([1-5])\b", text)
    if m: return int(m.group(1))
    word_map = {"one":1,"two":2,"three":3,"four":4,"five":5,
                "first":1,"second":2,"third":3,"fourth":4,"fifth":5}
    lower = text.lower()
    for word, num in word_map.items():
        if re.search(rf"\b{word}\b", lower): return num
    return None