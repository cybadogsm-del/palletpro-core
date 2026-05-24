"""
SMS delivery via Twilio.

Required env vars (set in palletpro-core/.env):
  TWILIO_ACCOUNT_SID   — from console.twilio.com
  TWILIO_AUTH_TOKEN    — from console.twilio.com
  TWILIO_FROM_NUMBER   — your Twilio number, e.g. +441234567890

Optional:
  PALLET_PRO_APP_URL   — base URL of the frontend (default: http://localhost:3000)

send_sms() and send_invite_sms() are always best-effort — they never raise,
so a Twilio failure never blocks user creation.
"""

import logging
import os

log = logging.getLogger(__name__)

_SID      = os.environ.get("TWILIO_ACCOUNT_SID")
_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN")
_FROM     = os.environ.get("TWILIO_FROM_NUMBER")
_APP_URL  = os.environ.get("PALLET_PRO_APP_URL", "http://localhost:3000").rstrip("/")


def send_sms(to: str, body: str) -> bool:
    """Send a raw SMS. Returns True on success, False on any failure."""
    if not (_SID and _TOKEN and _FROM):
        log.warning(
            "SMS not sent to %s — TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / "
            "TWILIO_FROM_NUMBER not configured in .env.",
            to,
        )
        return False
    try:
        from twilio.rest import Client
        client = Client(_SID, _TOKEN)
        client.messages.create(body=body, from_=_FROM, to=to)
        log.info("SMS sent to %s", to)
        return True
    except Exception as exc:
        log.error("SMS delivery failed to %s: %s", to, exc)
        return False


def send_invite_sms(to: str, setup_token: str, invited_by: str) -> bool:
    """
    Send the new-user invite SMS.
    The link lands on /setup?token=<TOKEN> in the frontend app.
    """
    link = f"{_APP_URL}/setup?token={setup_token}"
    body = (
        f"You've been invited to Pallet Pro by {invited_by}.\n\n"
        f"Tap the link below to set your password and get started:\n"
        f"{link}\n\n"
        f"This link expires in 7 days."
    )
    return send_sms(to, body)
