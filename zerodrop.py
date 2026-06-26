"""
zerodrop-python
Email verification infrastructure for CI pipelines and AI agents.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

__version__ = "0.1.0"
__all__ = [
    "ZeroDrop",
    "ZeroDropEmail",
    "ZeroDropFilter",
    "ZeroDropTimeoutError",
    "ZeroDropAuthError",
    "ZeroDropNetworkError",
]

BASE_URL = "https://zerodrop.dev"
FREE_DOMAIN = "zerodrop-sandbox.online"
POLL_INTERVAL = 2.0  # seconds


# ============================================
# Types
# ============================================

@dataclass
class ZeroDropEmail:
    id: str
    from_: str          # 'from' is a reserved keyword in Python
    to: str
    subject: str
    body: str
    raw_body: str
    received_at: datetime
    otp: Optional[str]        # Auto-extracted OTP code (4-8 digits)
    magic_link: Optional[str] # Auto-extracted verification/reset link


@dataclass
class ZeroDropFilter:
    from_: Optional[str] = None        # Partial match on sender address
    subject: Optional[str] = None      # Partial match on subject line
    body: Optional[str] = None         # Partial match on email body
    has_otp: Optional[bool] = None     # Only match emails with an extracted OTP
    has_magic_link: Optional[bool] = None  # Only match emails with a magic link


# ============================================
# Custom Errors
# ============================================

class ZeroDropTimeoutError(Exception):
    def __init__(self, inbox: str, timeout_ms: int):
        super().__init__(
            f'ZeroDrop: No email received at "{inbox}" within {timeout_ms}ms. '
            f'Check that your app is sending to the correct address.'
        )
        self.inbox = inbox
        self.timeout_ms = timeout_ms


class ZeroDropAuthError(Exception):
    def __init__(self):
        super().__init__("ZeroDrop: Invalid or missing API key.")


class ZeroDropNetworkError(Exception):
    def __init__(self, message: str):
        super().__init__(
            f"ZeroDrop: Network error — {message}. "
            f"Check https://zerodrop.instatus.com for service status."
        )


# ============================================
# Internal helpers
# ============================================

def _extract_body(raw: str) -> str:
    if not raw:
        return ""
    match = re.search(
        r"Content-Type: text/plain[^\r\n]*\r\n\r\n([\s\S]*?)(?:\r\n--|\r\n\r\n--)",
        raw
    )
    if match:
        return match.group(1).strip()
    lines = raw.split("\r\n")
    try:
        body_start = lines.index("") + 1
    except ValueError:
        body_start = 0
    return "\n".join(lines[body_start:]).strip()[:5000]


def _matches_filter(email: ZeroDropEmail, filter_: Optional[ZeroDropFilter]) -> bool:
    if filter_ is None:
        return True

    if filter_.from_ is not None:
        if filter_.from_.lower() not in email.from_.lower():
            return False

    if filter_.subject is not None:
        if filter_.subject.lower() not in email.subject.lower():
            return False

    if filter_.body is not None:
        if filter_.body.lower() not in email.body.lower():
            return False

    if filter_.has_otp is True and not email.otp:
        return False
    if filter_.has_otp is False and email.otp:
        return False

    if filter_.has_magic_link is True and not email.magic_link:
        return False
    if filter_.has_magic_link is False and email.magic_link:
        return False

    return True


def _parse_email(data: dict) -> ZeroDropEmail:
    raw = data.get("raw", "")
    return ZeroDropEmail(
        id=data.get("id", ""),
        from_=data.get("from", ""),
        to=data.get("to", ""),
        subject=data.get("subject", ""),
        body=_extract_body(raw),
        raw_body=raw,
        received_at=datetime.fromisoformat(
            data.get("receivedAt", datetime.utcnow().isoformat())
        ),
        otp=data.get("otp"),
        magic_link=data.get("magicLink"),
    )


def _generate_inbox_name() -> str:
    adjectives = ["swift", "dark", "cold", "null", "void", "zero", "dead", "raw", "base", "core"]
    adj = random.choice(adjectives)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
    return f"{adj}-{suffix}"


# ============================================
# Main ZeroDrop Client
# ============================================

class ZeroDrop:
    """
    ZeroDrop client for email verification in CI pipelines.

    Usage:
        mail = ZeroDrop()
        inbox = mail.generate_inbox()
        email = mail.wait_for_latest(inbox, timeout=15000)
        print(email.otp)        # "123456"
        print(email.magic_link) # "https://..."
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

        if not api_key:
            import warnings
            warnings.warn(
                "[ZeroDrop] No API key provided. Running in public sandbox mode.\n"
                "[ZeroDrop] Emails are AI-filtered and inboxes expire in 30 minutes.\n"
                "[ZeroDrop] Free tier uses a shared domain — for production CI use Workspaces: "
                "https://zerodrop.dev",
                stacklevel=2,
            )

    def generate_inbox(self) -> str:
        """
        Returns a ready-to-use email address instantly.
        No network request needed.
        """
        name = _generate_inbox_name()
        return f"{name}@{FREE_DOMAIN}"

    def fetch_latest(
        self,
        inbox: str,
        filter_: Optional[ZeroDropFilter] = None,
    ) -> Optional[ZeroDropEmail]:
        """
        Fetches the latest email for an inbox.
        Returns None if inbox is empty or no email matches the filter.
        """
        inbox_name = inbox.split("@")[0].lower()
        url = f"{self.base_url}/api/inbox/{inbox_name}?source=sdk"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 401:
                raise ZeroDropAuthError()
            raise ZeroDropNetworkError(f"API returned {e.code}")
        except URLError as e:
            raise ZeroDropNetworkError(str(e.reason))

        emails = data.get("emails", [])
        if not emails:
            return None

        for raw_email in emails:
            email = _parse_email(raw_email)
            if _matches_filter(email, filter_):
                return email

        return None

    def wait_for_latest(
        self,
        inbox: str,
        timeout: int = 10000,
        poll_interval: float = POLL_INTERVAL,
        filter_: Optional[ZeroDropFilter] = None,
    ) -> ZeroDropEmail:
        """
        Polls until an email arrives in the inbox.
        Throws ZeroDropTimeoutError on timeout.

        Args:
            inbox: The inbox address to poll.
            timeout: Milliseconds to wait (default: 10000).
            poll_interval: Seconds between polls (default: 2.0).
            filter_: Optional filter to match specific emails.

        Returns:
            ZeroDropEmail with otp and magic_link auto-extracted.
        """
        timeout_s = timeout / 1000
        start = time.monotonic()

        while time.monotonic() - start < timeout_s:
            try:
                email = self.fetch_latest(inbox, filter_=filter_)
                if email:
                    return email
            except ZeroDropAuthError:
                raise
            except ZeroDropNetworkError as e:
                import warnings
                warnings.warn(f"[ZeroDrop] Poll error (retrying): {e}", stacklevel=2)

            time.sleep(poll_interval)

        raise ZeroDropTimeoutError(inbox, timeout)

    def on_received(
        self,
        inbox: str,
        webhook_url: str,
    ) -> dict:
        """
        Registers a webhook for a workspace inbox.
        Requires API key (Workspace tier).
        """
        if not self.api_key:
            raise ZeroDropAuthError()

        url = f"{self.base_url}/api/webhooks/register"
        payload = json.dumps({"inbox": inbox, "webhookUrl": webhook_url}).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            req = Request(url, data=payload, headers=headers, method="POST")
            with urlopen(req, timeout=10) as resp:
                return {"registered": resp.status == 200}
        except HTTPError as e:
            if e.code == 401:
                raise ZeroDropAuthError()
            raise ZeroDropNetworkError(f"Webhook registration failed: {e.code}")
        except URLError as e:
            raise ZeroDropNetworkError(str(e.reason))