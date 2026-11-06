"""Outgoing email, behind one small interface. Pick with EMAIL_SENDER:

- console: prints the message to stdout (local development only; config.check_signin
  refuses it for SIGNIN_MODE=email on a public URL)
- smtp:    any SMTP server, via the standard library
- resend:  the Resend HTTP API (https://resend.com)

Senders never log what they send: a sign-in link in a log is a sign-in for
whoever can read the log."""
import asyncio
import logging
import smtplib
import sys
from email.message import EmailMessage
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger("hallway.mail")


class SendError(Exception):
    pass


def mask_email(email: str) -> str:
    """a***@example.com: enough to tell log lines apart, not enough to know who."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class Sender(Protocol):
    async def send(self, to: str, subject: str, text: str) -> None: ...


class ConsoleSender:
    """Development: the 'inbox' is the server's terminal."""

    async def send(self, to: str, subject: str, text: str) -> None:
        print(f"\n----- email to {to}: {subject}\n{text}\n-----", file=sys.stdout, flush=True)


class SMTPSender:
    def __init__(self, s: Settings):
        self.s = s

    def _send(self, msg: EmailMessage) -> None:
        s = self.s
        if s.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        with server:
            if s.smtp_port != 465 and s.smtp_starttls:
                server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)

    async def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.smtp_from, to, subject
        msg.set_content(text)
        try:
            await asyncio.to_thread(self._send, msg)
        except (smtplib.SMTPException, OSError) as e:
            raise SendError(f"smtp: {type(e).__name__}") from None


class ResendSender:
    URL = "https://api.resend.com/emails"

    def __init__(self, s: Settings):
        self.s = s

    async def send(self, to: str, subject: str, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(self.URL, headers={"Authorization": f"Bearer {self.s.resend_api_key}"},
                                 json={"from": self.s.smtp_from, "to": [to], "subject": subject, "text": text})
        except httpx.HTTPError as e:
            raise SendError(f"resend: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise SendError(f"resend: HTTP {r.status_code}")


def make_sender(s: Settings) -> Sender:
    if s.email_sender == "smtp":
        return SMTPSender(s)
    if s.email_sender == "resend":
        return ResendSender(s)
    return ConsoleSender()
