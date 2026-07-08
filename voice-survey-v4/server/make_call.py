"""
make_call.py — Trigger Twilio to call YOUR verified Indian number.
Run this while start_with_tunnel.py is already running.

Usage:
    python server/make_call.py
"""
import os
from dotenv import load_dotenv
from twilio.rest import Client
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

account_sid  = os.getenv("TWILIO_ACCOUNT_SID")
auth_token   = os.getenv("TWILIO_AUTH_TOKEN")
from_number  = os.getenv("TWILIO_PHONE_NUMBER")   # +18782830614
public_url   = os.getenv("PUBLIC_URL", "").rstrip("/")

# ── PUT YOUR VERIFIED INDIAN NUMBER HERE ──────────────────────
to_number = "+918147582124"   # replace with your actual +91 number
# ─────────────────────────────────────────────────────────────

if not public_url:
    print("ERROR: PUBLIC_URL not set. Run start_with_tunnel.py first.")
    exit(1)

webhook_url = f"{public_url}/incoming-call"
print(f"Calling {to_number} from {from_number}")
print(f"Webhook: {webhook_url}")

client = Client(account_sid, auth_token)
call = client.calls.create(
    to   = to_number,
    from_= from_number,
    url  = webhook_url,
    method = "POST",
)

print(f"\n✅ Call initiated!")
print(f"   Call SID : {call.sid}")
print(f"   Status   : {call.status}")
print(f"\nYour phone should ring in 5–10 seconds.")
print("Answer and speak to the bot.")