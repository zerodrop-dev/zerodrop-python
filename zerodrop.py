"""
zerodrop-python
Email verification infrastructure for CI pipelines and AI agents.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

__version__ = "0.3.0"
__all__ = [
    "ZeroDrop",
    "ZeroDropEmail",
    "ZeroDropFilter",
    "ZeroDropTimeoutError",
    "ZeroDropAuthError",
    "ZeroDropNetworkError",
    "ZeroDropHeldError",
]

BASE_URL = "https://zerodrop.dev"
FREE_DOMAIN = "zerodrop-sandbox.online"
POLL_INTERVAL = 2.0  # seconds

# A free key from https://zerodrop.dev/keys: zd_<prefix>_<32 letters/digits>.
# Inboxes named <prefix>-... can be read only with that key.
_KEY_FORMAT = re.compile(r"^zd_([a-z0-9]{4})_[A-Za-z0-9]{32}$")

# A key goes into an HTTP header, so after trimming it may hold only visible
# ASCII. Anything else is refused without echoing the key: the HTTP library's
# own check would quote the whole value in its error message.
_SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")
# Whitespace around a key goes, including no-break spaces and byte-order marks.
_KEY_PADDING = re.compile(r"^[\s\ufeff]+|[\s\ufeff]+$")


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
    def __init__(self, detail: Optional[str] = None):
        super().__init__(f"ZeroDrop: {detail or 'Invalid or missing API key.'}")


class ZeroDropNetworkError(Exception):
    def __init__(self, message: str):
        super().__init__(
            f"ZeroDrop: Network error — {message}. "
            f"Check https://zerodrop.instatus.com for service status."
        )


_HELD_DEFAULT = (
    "ZeroDrop held the email sent to this inbox: without an API key, one sender can reach "
    "only a few different inboxes a day. Get a free key at https://zerodrop.dev/keys and "
    "set ZERODROP_API_KEY."
)


class ZeroDropHeldError(Exception):
    """Without an API key, one sender can reach only a few different inboxes a
    day. Past that, ZeroDrop holds the email and leaves a notice in the inbox
    instead; fetch_latest() and wait_for_latest() then raise this, with the
    server's reason, rather than returning None or waiting until the timeout."""

    def __init__(self, reason: Optional[str] = None):
        super().__init__(reason or _HELD_DEFAULT)


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


def _is_notice(data: dict) -> bool:
    """A notice that ZeroDrop held the email, rather than an email."""
    return data.get("held") is True


def _held_error(notice: dict) -> ZeroDropHeldError:
    reason = notice.get("reason")
    return ZeroDropHeldError(reason.strip() if isinstance(reason, str) else None)


def _parse_time(value: object) -> datetime:
    """The API's "2026-10-23T09:15:02.481Z" as a UTC datetime. Before
    Python 3.11, datetime.fromisoformat doesn't accept the trailing "Z"."""
    if isinstance(value, str) and value:
        text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _parse_email(data: dict) -> ZeroDropEmail:
    raw = data.get("raw", "")
    return ZeroDropEmail(
        id=data.get("id", ""),
        from_=data.get("from", ""),
        to=data.get("to", ""),
        subject=data.get("subject", ""),
        body=_extract_body(raw),
        raw_body=raw,
        received_at=_parse_time(data.get("receivedAt")),
        otp=data.get("otp"),
        magic_link=data.get("magicLink"),
    )


def _key_prefix(api_key: Optional[str]) -> Optional[str]:
    """The inbox prefix a key carries: "zd_k7x2_..." -> "k7x2"."""
    match = _KEY_FORMAT.match(api_key or "")
    return match.group(1) if match else None


def _auth_error(error: HTTPError) -> ZeroDropAuthError:
    """The server explains a 401 in its JSON body; pass that on."""
    try:
        detail = json.loads(error.read().decode()).get("error")
    except Exception:
        detail = None
    return ZeroDropAuthError(detail if isinstance(detail, str) else None)


def _generate_inbox_name() -> str:
    # Without a key, an inbox's name is its only access control, so it comes
    # from the secrets module rather than random.
    adjectives = ["swift", "dark", "cold", "null", "void", "zero", "dead", "raw", "base", "core"]
    adj = secrets.choice(adjectives)
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(7))
    return f"{adj}-{suffix}"


# ============================================
# Main ZeroDrop Client
# ============================================

class ZeroDrop:
    """
    ZeroDrop client for email verification in CI pipelines.

    Usage:
        mail = ZeroDrop()          # reads ZERODROP_API_KEY if it's set
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
        # Trimmed, and a blank key falls back to ZERODROP_API_KEY.
        key = _KEY_PADDING.sub("", api_key or "") or _KEY_PADDING.sub(
            "", os.environ.get("ZERODROP_API_KEY", "")
        )
        if key and not _SENDABLE_KEY.fullmatch(key):
            raise ValueError(
                "ZeroDrop: the API key contains a character that can't be sent in an HTTP header, "
                "such as a line break. Copy it again from https://zerodrop.dev/keys."
            )
        self.api_key = key or None
        # The inbox prefix your key carries ("k7x2"), or None without a key.
        self.prefix = _key_prefix(self.api_key)
        self.base_url = base_url.rstrip("/")

        if not self.api_key:
            import warnings
            warnings.warn(
                "[ZeroDrop] No API key: anyone who knows an inbox's name can read it, and one "
                "sender can reach only a few different inboxes a day.\n"
                "[ZeroDrop] Free key for private, unlimited inboxes: https://zerodrop.dev/keys "
                "(set ZERODROP_API_KEY).\n"
                "[ZeroDrop] Inboxes expire in 30 minutes. Free tier uses a shared domain — "
                "for production CI use Workspaces: https://zerodrop.dev",
                stacklevel=2,
            )
        elif not self.prefix:
            import warnings
            warnings.warn(
                "[ZeroDrop] That API key isn't in the zd_xxxx_... format from "
                "https://zerodrop.dev/keys, so generated inboxes get no prefix and stay public.",
                stacklevel=2,
            )

    def generate_inbox(self) -> str:
        """
        Returns a ready-to-use email address instantly.
        No network request needed. With a key, the name starts
        with your prefix: k7x2-swift-x7k29ab
        """
        name = _generate_inbox_name()
        if self.prefix:
            name = f"{self.prefix}-{name}"
        return f"{name}@{FREE_DOMAIN}"

    def fetch_latest(
        self,
        inbox: str,
        filter_: Optional[ZeroDropFilter] = None,
    ) -> Optional[ZeroDropEmail]:
        """
        Fetches the latest email for an inbox.
        Returns None if inbox is empty or no email matches the filter.
        Raises ZeroDropHeldError if no email matches and the inbox has a
        notice that ZeroDrop held an email.
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
                raise _auth_error(e)
            raise ZeroDropNetworkError(f"API returned {e.code}")
        except URLError as e:
            raise ZeroDropNetworkError(str(e.reason))

        emails = data.get("emails", [])
        if not emails:
            return None

        # A notice of held mail is never returned as an email; if nothing
        # matches, it explains why.
        notice = None
        for raw_email in emails:
            if _is_notice(raw_email):
                notice = notice or raw_email
                continue
            email = _parse_email(raw_email)
            if _matches_filter(email, filter_):
                return email

        if notice is not None:
            raise _held_error(notice)
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
        Raises ZeroDropTimeoutError on timeout, and ZeroDropHeldError at once
        if ZeroDrop held the email.

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
            except (ZeroDropAuthError, ZeroDropHeldError):
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
        Deprecated: ZeroDrop has no webhooks, and the endpoint this called
        was never built. Kept so existing code still imports; it raises at
        once. Use wait_for_latest().
        """
        raise RuntimeError(
            "ZeroDrop: webhooks aren't available. Use wait_for_latest() to wait for an email."
        )