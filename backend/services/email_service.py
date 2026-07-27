"""Operator email notifications — a NO-OP unless SMTP is fully configured.

Mirrors main._init_sentry's posture: this is safe to ship before any SMTP
credentials exist. ``send_email`` reads the SMTP_* env vars and, unless
SMTP_HOST / SMTP_USER / SMTP_PASSWORD are ALL set, logs once at INFO and
returns False WITHOUT touching the network. Once the operator adds those vars
(plus optional SMTP_PORT / SMTP_FROM) to the deployment env, new "Contact
sales" lead submissions are emailed to LEADS_NOTIFY_EMAIL (default
quark2027@gmail.com).

Standard library only (smtplib + email.message) — no new pip dependency. Every
failure path is caught and logged: a broken or slow mail server must never
raise, so a lead submission can never fail because of a notification.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)

# Default recipient for lead notifications when LEADS_NOTIFY_EMAIL is unset.
DEFAULT_NOTIFY_EMAIL = "quark2027@gmail.com"


def send_email(subject: str, body: str, to: str) -> bool:
    """Send one plaintext email via SMTP + STARTTLS. Returns True on success.

    Reads SMTP_HOST, SMTP_PORT (default "587"), SMTP_USER, SMTP_PASSWORD and
    SMTP_FROM (default = SMTP_USER) from the environment.

    If SMTP_HOST / SMTP_USER / SMTP_PASSWORD are not ALL set, this logs once at
    INFO and returns False WITHOUT any network call — so the whole feature lies
    dormant until credentials are provisioned, exactly like main._init_sentry
    with SENTRY_DSN. Any exception during the send is logged as a warning and
    swallowed (returns False); this function never raises.
    """
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()

    if not (host and user and password):
        logger.info("email notifications disabled: SMTP not configured")
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM", "").strip() or user

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        msg.set_content(body)

        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)

        logger.info("notification email sent to %s", to)
        return True
    except Exception as exc:  # noqa: BLE001 — a mail failure must never break the caller
        logger.warning("failed to send notification email: %r", exc)
        return False


def _field(lead, key: str) -> object:
    """Read ``key`` from a lead dict (preferred) or any object with attrs."""
    if isinstance(lead, dict):
        return lead.get(key)
    return getattr(lead, key, None)


def send_lead_notification(lead) -> bool:
    """Email the operator the details of a new "Contact sales" lead.

    ``lead`` is a plain dict of the lead's fields (the caller in
    routers/leads.create_lead builds one so nothing lazy-loads inside the
    worker thread); an ORM object exposing the same attributes also works.

    Recipient is LEADS_NOTIFY_EMAIL (default quark2027@gmail.com). Returns
    whatever ``send_email`` returns — False (no-op) when SMTP is unconfigured.
    """
    company = _field(lead, "company_name") or "(unknown company)"
    subject = f"New Contact Sales lead: {company}"
    body = (
        "A new Enterprise 'Contact sales' lead was submitted on the landing page:\n"
        "\n"
        f"Company:       {company}\n"
        f"Contact name:  {_field(lead, 'contact_name') or '(not provided)'}\n"
        f"Contact email: {_field(lead, 'contact_email') or '(not provided)'}\n"
        f"Team size:     {_field(lead, 'team_size_estimate') or '(not provided)'}\n"
        "\n"
        "Message:\n"
        f"{_field(lead, 'message') or '(none)'}\n"
    )
    recipient = os.getenv("LEADS_NOTIFY_EMAIL", DEFAULT_NOTIFY_EMAIL)
    return send_email(subject, body, recipient)
