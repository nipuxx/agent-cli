"""Source quality checks for web and browser tools."""

from __future__ import annotations

ANTI_BOT_MARKERS = (
    "performing security verification",
    "cloudflare security challenge",
    "verifies you are not a bot",
    "verify you are not a bot",
    "enable javascript and cookies",
    "checking your browser before accessing",
    "just a moment...",
    "you have been blocked",
    "browsing and clicking at a speed much faster than expected",
    "there is a robot on the same network",
)


def anti_bot_reason(*parts: str) -> str | None:
    """Return a short reason if text looks like an anti-bot interstitial."""

    text = " ".join(part for part in parts if part).lower()
    if not text:
        return None
    for marker in ANTI_BOT_MARKERS:
        if marker in text:
            if "cloudflare" in text:
                return "cloudflare anti-bot challenge"
            if "captcha" in text or "you have been blocked" in text:
                return "captcha/anti-bot block"
            return "anti-bot challenge"
    return None
