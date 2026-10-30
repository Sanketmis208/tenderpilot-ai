"""Email delivery behind a provider interface. Status reflects what the provider actually confirmed."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class SendResult:
    status: str  # accepted | failed | skipped_no_provider
    message_id: str | None = None
    error: str | None = None


class EmailProvider(Protocol):
    def send(self, to: str, subject: str, body: str) -> SendResult: ...


class SmtpEmailProvider:
    def __init__(self) -> None:
        self.s = get_settings()

    def send(self, to: str, subject: str, body: str) -> SendResult:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.smtp_from, to, subject
        msg["Message-ID"] = make_msgid(domain="tenderpilot.local")
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.s.smtp_host or "", self.s.smtp_port, timeout=15) as smtp:
                if self.s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.s.smtp_username and self.s.smtp_password:
                    smtp.login(self.s.smtp_username, self.s.smtp_password.get_secret_value())
                refused = smtp.send_message(msg)
            if refused:
                return SendResult("failed", error=f"Recipients refused: {list(refused)}")
            # SMTP acceptance is not proof of delivery; we record "accepted".
            return SendResult("accepted", message_id=msg["Message-ID"])
        except (OSError, smtplib.SMTPException) as exc:
            return SendResult("failed", error=str(exc)[:500])


class NullEmailProvider:
    """No provider configured: nothing is sent, and we say so."""

    def send(self, to: str, subject: str, body: str) -> SendResult:
        log.info("email.skipped_no_provider", to_domain=to.split("@")[-1], subject=subject)
        return SendResult("skipped_no_provider")


def get_email_provider() -> EmailProvider:
    return SmtpEmailProvider() if get_settings().smtp_host else NullEmailProvider()
