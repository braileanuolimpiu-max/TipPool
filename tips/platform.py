"""Site-wide settings: what the operator saved on the Platform settings page, falling back to the
environment (.env) for local development. Everything that used to need code reads from here."""
from django.conf import settings
from django.core.cache import cache

CACHE_KEY = "tippool-platform-config"

EMAIL_PRESETS = {  # provider: (host, port)
    "gmail": ("smtp.gmail.com", 587),
    "outlook": ("smtp.office365.com", 587),
    "brevo": ("smtp-relay.brevo.com", 587),
}
CLAUDE_MODELS = [("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5 (faster, cheaper)"),
                 ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cheapest)")]


def config():
    """The saved settings (a blank one before anything is saved). Cached for 30 seconds."""
    from .models import PlatformConfig
    cfg = cache.get(CACHE_KEY)
    if cfg is None:
        try:
            cfg = PlatformConfig.objects.filter(pk=1).first() or PlatformConfig()
        except Exception:  # e.g. before the first migration
            cfg = PlatformConfig()
        if not getattr(settings, "TESTING", False):
            cache.set(CACHE_KEY, cfg, 30)
    return cfg


def forget():
    cache.delete(CACHE_KEY)


# --- email ------------------------------------------------------------------------------
def email_settings():
    """(host, port, user, password, from) when email is set up on the website, else None."""
    cfg = config()
    if not (cfg.email_host and cfg.email_user and cfg.email_password_encrypted):
        return None
    return cfg.email_host, cfg.email_port, cfg.email_user, cfg.email_password, cfg.email_from or cfg.email_user


def email_enabled():
    return email_settings() is not None or settings.EMAIL_ENABLED


# --- AI ---------------------------------------------------------------------------------
def ai_mode():
    cfg = config()
    if cfg.anthropic_key_encrypted:
        return "claude" if cfg.ai_enabled else "mock"
    return settings.AI_MODE


def anthropic_key():
    import os
    cfg = config()
    return (cfg.anthropic_key if cfg.anthropic_key_encrypted else "") or os.environ.get("ANTHROPIC_API_KEY", "")


def claude_model():
    return config().claude_model or settings.CLAUDE_MODEL


# --- TipPool's own PayPal ---------------------------------------------------------------------
def paypal_platform():
    """(client id, secret, API address, live?) of TipPool's own PayPal account."""
    from .paypal import LIVE_API, SANDBOX_API
    cfg = config()
    if cfg.paypal_client_id and cfg.paypal_secret_encrypted:
        return cfg.paypal_client_id, cfg.paypal_secret, (LIVE_API if cfg.paypal_live else SANDBOX_API), cfg.paypal_live
    base = settings.PAYPAL_BASE_URL
    return settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET, base, "sandbox" not in base


def paypal_platform_fallback():
    """Businesses without their own PayPal may use TipPool's account only while it's test money."""
    cfg = config()
    if cfg.paypal_client_id:
        return not cfg.paypal_live
    return settings.PAYPAL_PLATFORM_FALLBACK


def paypal_platform_webhook():
    return config().paypal_webhook_id or settings.PAYPAL_WEBHOOK_ID


# --- the demo, the scheduler, contact ----------------------------------------------------------
def demo_buyer():
    cfg = config()
    if cfg.demo_buyer_email:
        return cfg.demo_buyer_email, cfg.demo_buyer_password
    return (settings.DEMO_BUYER_EMAIL, settings.DEMO_BUYER_PASSWORD) if settings.DEMO_BUYER_EMAIL else None


def demo_staff_email(name):
    import os
    return (config().demo_staff or {}).get(name) or os.environ.get(f"{name.upper()}_EMAIL", "")


def cron_secret():
    cfg = config()
    return cfg.cron_secret if cfg.pk else settings.CRON_SECRET


def contact_email():
    return config().contact_email or settings.CONTACT_EMAIL


# --- who runs the site -------------------------------------------------------------------------
def is_operator(user):
    return bool(user and user.is_authenticated and user.is_superuser)


def has_operator():
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(is_superuser=True).exists()
