"""
Email delivery via SendGrid.

Required env vars (set in palletpro-core/.env):
  SENDGRID_API_KEY     — from app.sendgrid.com
  SENDGRID_FROM_EMAIL  — verified sender, e.g. noreply@palletpro.app

Optional:
  PALLET_PRO_APP_URL   — frontend base URL (default: http://localhost:3000)

send_email() and send_invite_email() are always best-effort — they never raise,
so an email failure never blocks user creation.
"""

import logging
import os

log = logging.getLogger(__name__)

_API_KEY  = os.environ.get("SENDGRID_API_KEY")
_FROM     = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@palletpro.app")
_APP_URL  = os.environ.get("PALLET_PRO_APP_URL", "http://localhost:3000").rstrip("/")


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """Send a plain or HTML email via SendGrid. Returns True on success."""
    if not _API_KEY:
        log.warning("Email not sent to %s — SENDGRID_API_KEY not configured.", to)
        return False
    try:
        import sendgrid as sg_module
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email=_FROM,
            to_emails=to,
            subject=subject,
            plain_text_content=body_text,
            html_content=body_html or body_text.replace("\n", "<br>"),
        )
        sg_module.SendGridAPIClient(api_key=_API_KEY).send(message)
        log.info("Email sent to %s", to)
        return True
    except Exception as exc:
        log.error("Email delivery failed to %s: %s", to, exc)
        return False


def send_invite_email(to: str, setup_token: str, display_name: str, invited_by: str) -> bool:
    """Send the new-user invite email containing the /setup link."""
    link = f"{_APP_URL}/setup?token={setup_token}"
    subject = "You've been invited to Pallet Pro"
    body_text = (
        f"Hi {display_name},\n\n"
        f"You've been invited to Pallet Pro by {invited_by}.\n\n"
        f"Tap the link below to set your password and get started:\n"
        f"{link}\n\n"
        f"This link expires in 7 days.\n\n"
        f"— The Pallet Pro Team"
    )
    body_html = f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 16px;">
  <div style="background:#1D4ED8;border-radius:16px;padding:24px;text-align:center;margin-bottom:24px;">
    <span style="color:white;font-size:28px;font-weight:800;letter-spacing:-1px;">PP</span>
    <p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">Pallet Pro</p>
  </div>
  <h2 style="margin:0 0 8px;color:#0F172A;">Hi {display_name},</h2>
  <p style="color:#475569;margin:0 0 24px;">
    <strong>{invited_by}</strong> has invited you to join their team on Pallet Pro.
  </p>
  <a href="{link}"
     style="display:block;background:#1D4ED8;color:white;text-decoration:none;
            text-align:center;padding:16px 24px;border-radius:12px;
            font-weight:700;font-size:16px;margin-bottom:16px;">
    Set Up My Account →
  </a>
  <p style="color:#94A3B8;font-size:12px;text-align:center;margin:0;">
    Link expires in 7 days. If you weren't expecting this, you can ignore it.
  </p>
</div>
"""
    return send_email(to, subject, body_text, body_html)
