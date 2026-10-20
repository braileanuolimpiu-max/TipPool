"""Site-wide settings: what the operator saved on the Platform settings page, falling back to the
environment (.env) for local development. Everything that used to need code reads from here."""
from django.conf import settings
from django.core.cache import cache

CACHE_KEY = "tippool-platform-config"

EMAIL_PRESETS = {  # provider: (host, port)
    "gmail": ("smtp.gmail.com", 587),
    "outlook": ("smtp.office365.com", 587),
    "brevo": ("api.brevo.com", 443),  # its web API, not SMTP: works where email ports are blocked
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


def cron_secrets():
    """The scheduler keys that work: the one shown on Platform settings and, so an existing cron job never
    breaks, the one from the environment."""
    keys = [config().cron_secret if config().pk else "", settings.CRON_SECRET]
    return [k for k in keys if k]


def contact_email():
    return config().contact_email or settings.CONTACT_EMAIL


# --- who runs the site -------------------------------------------------------------------------
def is_operator(user):
    return bool(user and user.is_authenticated and user.is_superuser)


def has_operator():
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(is_superuser=True, is_active=True).exists()


def setup_code():
    """A code only someone who can read the server's log (the person who deployed TipPool) knows.
    Needed to take over running a site that already has businesses but nobody running it."""
    import hashlib
    import hmac
    digest = hmac.new(settings.SECRET_KEY.encode(), b"tippool-operator-claim", hashlib.sha256).hexdigest()
    return digest[:10].upper()


def make_first_operator(user, business):
    """The first real business on a brand-new site runs it. Locked, so two sign-ups at once can't both win."""
    from django.db import transaction

    from .models import Business, PlatformConfig
    with transaction.atomic():
        PlatformConfig.objects.get_or_create(pk=1)
        PlatformConfig.objects.select_for_update().get(pk=1)
        # any other real business with people in it means the site is already in use
        others = Business.objects.filter(is_demo=False, managers__isnull=False).exclude(pk=business.pk).exists()
        if has_operator() or others:
            return False
        user.is_superuser = user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
        return True
