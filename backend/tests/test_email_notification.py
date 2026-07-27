"""Tests for the operator lead-notification email (services/email_service.py)
and its wiring into POST /api/leads/.

NO REAL EMAIL IS EVER SENT: smtplib.SMTP is monkeypatched to an in-memory fake,
so these tests make no network call. The behaviour that actually matters here
is the *gating* — the feature must be a silent no-op until SMTP credentials are
configured, and a mail failure must never turn a successful lead capture into
an error for the prospect.
"""
from __future__ import annotations

import pytest

from services import email_service

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here — most tests in
# this file are plain sync functions, and a blanket asyncio mark makes pytest
# warn on every one of them. The three endpoint tests are marked individually.


class _FakeSMTP:
    """Records what would have been sent instead of touching the network."""

    sent: list[dict] = []
    raise_on_send = False

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        if _FakeSMTP.raise_on_send:
            raise RuntimeError("simulated SMTP outage")
        _FakeSMTP.sent.append(
            {
                "to": msg["To"],
                "from": msg["From"],
                "subject": msg["Subject"],
                "body": msg.get_content(),
            }
        )


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.sent = []
    _FakeSMTP.raise_on_send = False
    monkeypatch.setattr(email_service.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


@pytest.fixture
def smtp_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password-not-real")


@pytest.fixture
def smtp_unconfigured(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Gating — the whole feature is dormant until SMTP_* is provisioned
# ---------------------------------------------------------------------------


def test_send_email_noops_when_unconfigured(fake_smtp, smtp_unconfigured):
    """No host/user/password => returns False and never constructs an SMTP."""
    assert email_service.send_email("subj", "body", "someone@example.test") is False
    assert fake_smtp.sent == []


def test_send_email_sends_when_configured(fake_smtp, smtp_configured):
    assert email_service.send_email("subj", "body", "someone@example.test") is True
    assert len(fake_smtp.sent) == 1
    assert fake_smtp.sent[0]["to"] == "someone@example.test"


def test_send_email_swallows_smtp_failure(fake_smtp, smtp_configured):
    """A broken mail server returns False rather than raising."""
    fake_smtp.raise_on_send = True
    assert email_service.send_email("subj", "body", "someone@example.test") is False


def test_lead_notification_defaults_to_operator_inbox(fake_smtp, smtp_configured, monkeypatch):
    monkeypatch.delenv("LEADS_NOTIFY_EMAIL", raising=False)
    ok = email_service.send_lead_notification(
        {
            "company_name": "Acme Pharma",
            "contact_name": "Jane Doe",
            "contact_email": "jane@acme.example",
            "team_size_estimate": "50-100",
            "message": "Interested in enterprise.",
        }
    )
    assert ok is True
    sent = fake_smtp.sent[0]
    assert sent["to"] == email_service.DEFAULT_NOTIFY_EMAIL == "quark2027@gmail.com"
    assert "Acme Pharma" in sent["subject"]
    assert "jane@acme.example" in sent["body"]


def test_lead_notification_tolerates_missing_optional_fields(fake_smtp, smtp_configured):
    """team_size/message are optional on the form — no KeyError, no crash."""
    assert email_service.send_lead_notification({"company_name": "Beta CRO"}) is True
    assert "(not provided)" in fake_smtp.sent[0]["body"]


# ---------------------------------------------------------------------------
# Endpoint wiring — capture must never depend on the notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lead_emails_the_operator(api_client, fake_smtp, smtp_configured):
    resp = await api_client.post(
        "/api/leads/",
        json={
            "company_name": "Gamma Bio",
            "contact_name": "Sam Lee",
            "contact_email": "sam@gamma.example",
            "team_size_estimate": "10-20",
            "message": "Requesting a demo.",
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(fake_smtp.sent) == 1
    assert "Gamma Bio" in fake_smtp.sent[0]["subject"]
    assert "sam@gamma.example" in fake_smtp.sent[0]["body"]


@pytest.mark.asyncio
async def test_create_lead_succeeds_with_smtp_unconfigured(api_client, fake_smtp, smtp_unconfigured):
    """The pre-credentials state: lead is still captured, nothing is emailed."""
    resp = await api_client.post(
        "/api/leads/",
        json={
            "company_name": "Delta Labs",
            "contact_name": "Ana Roy",
            "contact_email": "ana@delta.example",
        },
    )
    assert resp.status_code == 201, resp.text
    assert fake_smtp.sent == []


@pytest.mark.asyncio
async def test_create_lead_succeeds_even_if_smtp_breaks(api_client, fake_smtp, smtp_configured):
    """A mail outage must not surface as an error to the prospect."""
    fake_smtp.raise_on_send = True
    resp = await api_client.post(
        "/api/leads/",
        json={
            "company_name": "Epsilon Rx",
            "contact_name": "Kim Park",
            "contact_email": "kim@epsilon.example",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["company_name"] == "Epsilon Rx"
