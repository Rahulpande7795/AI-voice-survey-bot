"""
start_with_tunnel.py — Start ngrok tunnel + uvicorn server.

Place this in the PROJECT ROOT (not inside server/).
Running from server/ causes uvicorn --reload to detect changes
in start_with_tunnel.py and kill the ngrok connection on every file save.

Usage:
  cd voice-survey-v4
  python start_with_tunnel.py

How it avoids the reload-loop bug:
  1. This file lives in the project ROOT, outside server/.
  2. uvicorn is started with --reload-dir server   (watches server/ only)
  3. ngrok tunnel is managed by THIS process (the parent).
     uvicorn runs as a child subprocess — if uvicorn restarts, ngrok stays alive.
  4. PUBLIC_URL is passed as a real env var to the child process env dict,
     so uvicorn inherits it and os.getenv("PUBLIC_URL") works at request time.
"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key
from pyngrok import conf, ngrok

# ── Load existing .env ────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

# ── Configure ngrok auth ──────────────────────────────────────────────────────
auth_token = os.getenv("NGROK_AUTH_TOKEN", "")
if auth_token:
    conf.get_default().auth_token = auth_token
else:
    print("WARNING: NGROK_AUTH_TOKEN not set in .env")
    print("         Get your token at: https://dashboard.ngrok.com/authtokens")
    print()

# ── Start ngrok tunnel ────────────────────────────────────────────────────────
print("Starting ngrok tunnel on port 8000…")

# Use static domain if configured, otherwise get dynamic URL
static_domain = os.getenv("NGROK_STATIC_DOMAIN", "")
if static_domain:
    tunnel = ngrok.connect(8000, "http", domain=static_domain)
else:
    tunnel = ngrok.connect(8000, "http", domain="photo-dinginess-unicorn.ngrok-free.dev")

public_url = tunnel.public_url
# Ensure HTTPS (ngrok may return http://)
if public_url.startswith("http://"):
    public_url = "https://" + public_url[7:]

# ── Persist PUBLIC_URL to .env and current env ────────────────────────────────
# set_key writes it properly even if the value has special chars
set_key(str(ENV_PATH), "PUBLIC_URL", public_url)
# Also set in THIS process env — child subprocess inherits it
os.environ["PUBLIC_URL"] = public_url

print()
print("=" * 64)
print(f"  ngrok tunnel   : {public_url}")
print(f"  Twilio webhook : {public_url}/incoming-call")
print(f"  Browser UI     : {public_url}/")
print("=" * 64)
print()
print("  Copy the Twilio webhook URL into:")
print("  Twilio Console → Phone Numbers → +18782830614")
print("  → Voice → A call comes in → Webhook → [paste URL]")
print()
print(f"  PUBLIC_URL saved to .env ✅")
print()
print("  Starting uvicorn (Ctrl+C to stop everything)…")
print()

# ── Start uvicorn as a child subprocess ──────────────────────────────────────
# Key design choices:
#   --reload-dir server   → watchfiles ONLY watches server/, not project root
#   cwd=server_dir        → uvicorn imports main:app from server/
#   env={...PUBLIC_URL}   → child inherits PUBLIC_URL in its environment
#
# ngrok is managed by THIS (parent) process.
# When uvicorn --reload restarts, it's the child that restarts, not us.
# So ngrok stays alive across uvicorn reloads.

server_dir = Path(__file__).parent / "server"

try:
    subprocess.run(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--reload",
            "--reload-dir", str(server_dir),
        ],
        cwd=str(server_dir),
        env={**os.environ, "PUBLIC_URL": public_url},
    )
except KeyboardInterrupt:
    pass
finally:
    print("\nShutting down ngrok tunnel…")
    ngrok.kill()
    print("Done.")
