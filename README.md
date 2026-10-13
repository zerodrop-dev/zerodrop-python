# zerodrop

[![PyPI version](https://img.shields.io/pypi/v/zerodrop.svg)](https://pypi.org/project/zerodrop/)
[![Python versions](https://img.shields.io/pypi/pyversions/zerodrop.svg)](https://pypi.org/project/zerodrop/)
[![license](https://img.shields.io/pypi/l/zerodrop.svg)](https://github.com/zerodrop-dev/zerodrop-python/blob/main/LICENSE)

Email verification infrastructure for CI pipelines and AI agents.

Send a verification email. Catch it at the edge. Get `email.otp` and `email.magic_link` back — auto-extracted, no regex, no Docker, no signup.

```python
email = mail.wait_for_latest(inbox)

email.otp        # "123456" — auto-extracted
email.magic_link # "https://..." — no regex needed
```

**[Documentation](https://docs.zerodrop.dev)** · [GitHub](https://github.com/zerodrop-dev/zerodrop-python) · [Status](https://zerodrop.instatus.com)

## Install

```bash
pip install zerodrop
```

No dependencies. Python 3.8+.

## Zero-Auth Mode (Local Development)

```python
from zerodrop import ZeroDrop

mail = ZeroDrop()
inbox = mail.generate_inbox()
# → "swift-x7k29@zerodrop-sandbox.online"

email = mail.wait_for_latest(inbox, timeout=10000)
print(email.subject)     # "Reset your password"
print(email.otp)         # "123456" — auto-extracted, no regex needed
print(email.magic_link)  # "https://..." — auto-extracted verification link
```

## Free API Key (Private Inboxes)

Without a key, anyone who knows an inbox's name can read it. With a free key, your inboxes start with your own prefix and only your key can read them.

1. Sign in with GitHub at [zerodrop.dev/keys](https://zerodrop.dev/keys) and create a key.
2. Set it as `ZERODROP_API_KEY`. The client reads it automatically, or you can pass `api_key=`.

```python
mail = ZeroDrop()                  # reads ZERODROP_API_KEY when it's set
inbox = mail.generate_inbox()
# → "k7x2-swift-x7k29ab@zerodrop-sandbox.online", where k7x2 is your prefix
```

Reading a `k7x2-…` inbox without your key raises `ZeroDropAuthError` with the reason. `mail.prefix` holds your prefix if you build an address yourself.

## CI Pipeline Mode (pytest)

```python
from zerodrop import ZeroDrop

mail = ZeroDrop(api_key=os.environ.get("ZERODROP_API_KEY"))

def test_password_reset(page):
    inbox = mail.generate_inbox()

    page.goto("/forgot-password")
    page.fill('[name="email"]', inbox)
    page.click('[type="submit"]')

    email = mail.wait_for_latest(inbox, timeout=15000)
    assert "Reset your password" in email.subject

    # No regex — magic_link is auto-extracted at the edge
    page.goto(email.magic_link)
    assert page.url == "/dashboard"
```

## Email Filtering

Filter emails by sender, subject, body, or extracted fields:

```python
from zerodrop import ZeroDrop, ZeroDropFilter

mail = ZeroDrop()

# Only match emails from a specific sender
email = mail.wait_for_latest(inbox, filter_=ZeroDropFilter(
    from_="noreply@yourapp.com"
))

# Only match emails with an OTP
email = mail.wait_for_latest(inbox, filter_=ZeroDropFilter(
    has_otp=True
))

# Combine filters
email = mail.wait_for_latest(inbox, timeout=15000, filter_=ZeroDropFilter(
    from_="noreply@yourapp.com",
    subject="Verify",
    has_magic_link=True
))
```

All string filters are case-insensitive partial matches.

## OTP Auto-Extraction

ZeroDrop extracts OTP codes and magic links at the edge before emails reach your test suite. No regex required.

```python
email = mail.wait_for_latest(inbox)

email.otp        # "123456" — 4-8 digit verification code
email.magic_link # "https://app.com/verify?token=abc" — verification/reset link
email.body       # Full plain-text body (if you need it)
```

Both fields are `None` if not detected in the email.

## Parallel CI Runs

`generate_inbox()` runs locally — no network request, no throttling:

```python
# Safe to run in parallel — generate_inbox() is local
inboxes = [mail.generate_inbox() for _ in range(50)]
```

## API

### `ZeroDrop(api_key=None, base_url=BASE_URL)`
- `api_key` — optional. Defaults to the `ZERODROP_API_KEY` environment variable. Without a key, inboxes are public to anyone who knows the name.
- `base_url` — override the API base URL.

### `mail.generate_inbox() -> str`
Returns a ready-to-use email address instantly. No network request. With a key, the name starts with your prefix.

### `mail.prefix -> Optional[str]`
Your key's inbox prefix (`"k7x2"`), or `None` without a key.

### `mail.fetch_latest(inbox, filter_=None) -> Optional[ZeroDropEmail]`
Returns the latest email or `None` if inbox is empty.

### `mail.wait_for_latest(inbox, timeout=10000, poll_interval=2.0, filter_=None) -> ZeroDropEmail`
Polls until an email arrives. Raises `ZeroDropTimeoutError` on timeout.
- `timeout` — milliseconds to wait (default: 10000)
- `poll_interval` — seconds between polls (default: 2.0)
- `filter_` — optional `ZeroDropFilter` instance

### `mail.on_received(inbox, webhook_url) -> dict`
Registers a webhook. Requires API key (Workspace tier).

## Types

```python
@dataclass
class ZeroDropEmail:
    id: str
    from_: str
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
    has_otp: Optional[bool] = None     # Only match emails with an OTP
    has_magic_link: Optional[bool] = None  # Only match emails with a magic link
```

## Error Handling

```python
from zerodrop import ZeroDrop, ZeroDropTimeoutError

try:
    email = mail.wait_for_latest(inbox, timeout=10000)
except ZeroDropTimeoutError:
    print("No email received — check your app is sending correctly")
```

## Free vs Workspace

|  | Free | Workspace |
|---|---|---|
| Inbox generation | ✓ | ✓ |
| OTP auto-extraction | ✓ | ✓ |
| Magic link extraction | ✓ | ✓ |
| Email filtering | ✓ | ✓ |
| Email retention | 30 min | Extended |
| Custom domains | ✗ | ✓ |
| API key | ✓ free at [zerodrop.dev/keys](https://zerodrop.dev/keys) | ✓ |
| Webhooks | ✗ | ✓ |

Get a Workspace at [zerodrop.dev](https://zerodrop.dev)

## License

MIT