"""The bot check on guest tip pages (Cloudflare Turnstile): scripts can't create PayPal orders by the hundred.
Off until Platform settings has both keys. Usually invisible to guests.

It fails open: a guest who can't tip is worse than a few junk orders. When Cloudflare can't be reached (or
our keys are wrong), the tip goes through. A browser that sends no check at all (Cloudflare's script didn't
load) may still tip, but only a few times, so a script skipping the check gets almost nowhere.
"""
import logging

import requests

from .ratelimit import hit

log = logging.getLogger("tips.botcheck")

SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onBotCheckLoaded"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT = 5
# our side's problem, not the guest's: let the tip through
OUR_PROBLEM = {"missing-input-secret", "invalid-input-secret", "internal-error"}


def site_key():
    """The key the tip page shows, or "" while the check is off."""
    from .platform import config
    cfg = config()
    return cfg.turnstile_site_key if cfg.turnstile_site_key and cfg.turnstile_secret_encrypted else ""


def passed(token, ip, location_pk):
    """True when this guest may create an order: they passed, the check is off, or it can't be done."""
    if not site_key():
        return True
    if not token:
        # no check from the browser (script blocked or not loaded): a few tries, never a flood
        return hit(f"botcheck-none:{ip}", 3, 600) and hit(f"botcheck-none-loc:{location_pk}", 30, 3600)
    from .platform import config
    try:
        response = requests.post(VERIFY_URL, data={"secret": config().turnstile_secret, "response": str(token)[:2048],
                                                   "remoteip": ip}, timeout=TIMEOUT)
        result = response.json()
    except (requests.RequestException, ValueError):
        log.warning("Bot check: Cloudflare couldn't be reached, so the tip goes through")
        return True
    if not isinstance(result, dict):
        return True
    if result.get("success"):
        return True
    codes = set(result.get("error-codes") or [])
    if codes & OUR_PROBLEM:
        log.warning("Bot check: Cloudflare says %s; the tip goes through (check the keys in Platform settings)",
                    ", ".join(sorted(codes)))
        return True
    return False


def secret_works(secret):
    """Ask Cloudflare whether a secret key is right: True, False, or None when Cloudflare can't say now."""
    try:
        result = requests.post(VERIFY_URL, data={"secret": secret, "response": "tippool-key-check"},
                               timeout=TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return None
    codes = set((result or {}).get("error-codes") or []) if isinstance(result, dict) else set()
    return not ({"invalid-input-secret", "missing-input-secret"} & codes)
