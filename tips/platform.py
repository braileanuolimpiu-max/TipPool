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
    """"claude" when a real AI service answers (Claude or Gemini, see ai_provider()), else "mock"."""
    cfg = config()
    if cfg.ai_provider == "gemini":
        return "claude" if (cfg.ai_enabled and cfg.gemini_key_encrypted) else "mock"
    if cfg.ai_provider in OTHER_AI:
        return "claude" if (cfg.ai_enabled and (cfg.other_ai_keys or {}).get(cfg.ai_provider)) else "mock"
    if cfg.anthropic_key_encrypted:
        return "claude" if cfg.ai_enabled else "mock"
    return settings.AI_MODE


OTHER_AI = ("openai", "deepseek", "kimi", "mistral")


def ai_provider():
    provider = config().ai_provider
    return provider if provider == "gemini" or provider in OTHER_AI else "claude"


def ai_name():
    """What people see: "Gemini" or "Claude"."""
    provider = ai_provider()
    if provider in OTHER_AI:
        from .openai_compat import name
        return name(provider)
    return "Gemini" if provider == "gemini" else "Claude"


def other_ai_key(provider):
    from .vault import decrypt
    stored = (config().other_ai_keys or {}).get(provider, "")
    return decrypt(stored) if stored else ""


def other_ai_model(provider):
    from .openai_compat import PROVIDERS
    return (config().other_ai_models or {}).get(provider) or PROVIDERS[provider][4][0]


def gemini_key():
    import os
    cfg = config()
    return (cfg.gemini_key if cfg.gemini_key_encrypted else "") or os.environ.get("GEMINI_API_KEY", "")


def gemini_model():
    return config().gemini_model or "gemini-flash-latest"


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
    # from the environment: never with real money, even if PAYPAL_PLATFORM_FALLBACK=1 is set by mistake
    return settings.PAYPAL_PLATFORM_FALLBACK and "sandbox" in settings.PAYPAL_BASE_URL


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
    breaks, the one from the environment, until "Make a new address" retires it."""
    cfg = config()
    keys = [cfg.cron_secret if cfg.pk else "", "" if cfg.cron_env_retired else settings.CRON_SECRET]
    return [k for k in keys if k]


def contact_email():
    return config().contact_email or settings.CONTACT_EMAIL


# --- the site's own address (for links in emails sent outside a request: alerts, summaries) ------
def site_url():
    """https://… of this site: SITE_URL, else Render's address, else the address it was last opened at."""
    import os
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    if getattr(settings, "RENDER_HOST", ""):
        return f"https://{settings.RENDER_HOST}/"
    return cache.get("site-url", "")


# --- who runs the site -------------------------------------------------------------------------
def is_operator(user):
    return bool(user and user.is_authenticated and user.is_superuser)


def needs_two_step(user):
    """Running the site (Platform settings, the admin) controls every business's keys and money: the operator
    must have two-step sign-in on. Not on your own computer (DEBUG)."""
    profile = getattr(user, "manager", None) if user is not None and user.is_authenticated else None
    return not settings.DEBUG and not (profile is not None and profile.totp_enabled)


def has_operator():
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(is_superuser=True, is_active=True).exists()


def setup_code():
    """A code only someone who can read the server's log (the person who deployed TipPool) knows.
    Needed to run a site: signing up first is not enough, since anyone could find a new site before its owner."""
    import hashlib
    import hmac
    digest = hmac.new(settings.SECRET_KEY.encode(), b"tippool-operator-claim", hashlib.sha256).hexdigest()
    return digest[:10].upper()


def can_claim(user):
    """Nobody runs the site yet and this is the owner of a real business: they may take the role with the setup code."""
    profile = getattr(user, "manager", None) if user is not None and user.is_authenticated else None
    return (profile is not None and profile.is_owner and not profile.business.is_demo
            and not is_operator(user) and not has_operator())
