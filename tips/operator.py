"""Platform settings: everything about running the TipPool site, set up on the website (no code, no .env).
Only the site's operator sees it: the owner who claimed it with the setup code from the server's log."""
import smtplib

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import platform
from .ratelimit import limited
from .models import Change, PlatformConfig
from .vault import encrypt

DEMO_STAFF = ["Maria", "John", "Lea", "Alex", "Sam"]
TYPED_FIELDS = ['email_provider', 'email_host', 'email_port', 'email_user', 'email_from', 'claude_model', 'paypal_client_id', 'demo_buyer_email', 'demo_buyer_password', 'contact_email']


def _operator_only(request):
    """None when this person may use the page; otherwise the response to send instead."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if platform.is_operator(request.user):
        if platform.needs_two_step(request.user):
            return render(request, "tips/platform_claim.html", {"needs_two_step": True}, status=403)
        return None
    can_claim = platform.can_claim(request.user)
    if can_claim:
        import logging
        logging.getLogger("tips.operator").warning("TipPool setup code (to take over running this site): %s",
                                                   platform.setup_code())
    return render(request, "tips/platform_claim.html", {"can_claim": can_claim}, status=200 if can_claim else 403)


def _who(request):
    return request.user.get_full_name() or request.user.get_username()


def _done(request, text, section):
    Change.log(_who(request), f"Platform settings: {text}")  # site-wide: not in any business's history
    messages.success(request, text[0].upper() + text[1:] + ".")
    return redirect(f"{reverse('platform_settings')}#{section}")


def platform_settings(request):
    stop = _operator_only(request)
    if stop is not None:
        return stop
    cfg = PlatformConfig.load()
    if request.method == "POST":
        # what was typed comes back if the save fails (never passwords, keys or secrets)
        request.session["platform_typed"] = {k: v for k, v in request.POST.items()
                                             if k in TYPED_FIELDS or k.startswith("staff_")}
        section = request.POST.get("section", "")
        handler = {"email": _save_email, "email_test": _test_email, "ai": _save_ai, "paypal": _save_paypal,
                   "paypal_disconnect": _disconnect_paypal, "demo": _save_demo, "cron_new": _new_cron,
                   "contact": _save_contact, "paypal_webhook": _retry_webhook}.get(section)
        if handler:
            return handler(request, cfg)
    typed = request.session.pop("platform_typed", {})
    form = {field: typed.get(field, getattr(cfg, field)) for field in TYPED_FIELDS}
    tick = request.build_absolute_uri(reverse("cron_tick")) + f"?key={cfg.cron_secret}"
    minutes_since_tick = int((timezone.now() - cfg.last_tick_at).total_seconds() // 60) if cfg.last_tick_at else None
    return render(request, "tips/platform.html", {
        "cfg": cfg, "providers": PlatformConfig.EmailProvider.choices, "models": platform.CLAUDE_MODELS,
        "email_on": platform.email_settings() is not None, "ai_on": platform.ai_mode() == "claude",
        "ai_name": platform.ai_name(), "ai_used_today": _ai().used_today(), "other_ai": _other_ai(cfg),
        "demo_daily": _ai().DEMO_DAILY, "business_daily": _ai().BUSINESS_DAILY,
        "paypal_on": bool(cfg.paypal_client_id), "tick_url": tick, "minutes_since_tick": minutes_since_tick,
        "up_url": request.build_absolute_uri("/up/"),
        "demo_staff": [(name, typed.get(f"staff_{name}", (cfg.demo_staff or {}).get(name, ""))) for name in DEMO_STAFF],
        "form": form,
        "https": request.is_secure(),
    })


@require_POST
@limited("platform-claim", 5, 3600)
def claim_platform(request):
    """On a fresh site with no operator yet, the owner of a real business can take the role."""
    import hmac

    from django.db import transaction
    if not request.user.is_authenticated:
        return redirect("dashboard")
    profile = getattr(request.user, "manager", None)
    if profile is None or not profile.is_owner or profile.business.is_demo:
        return redirect("dashboard")
    typed = request.POST.get("code", "").strip().upper()
    if not hmac.compare_digest(typed, platform.setup_code()):
        messages.error(request, "That setup code isn't right. It's in your server's log (on Render: your service → Logs).")
        return redirect("platform_settings")
    with transaction.atomic():
        PlatformConfig.objects.get_or_create(pk=1)
        PlatformConfig.objects.select_for_update().get(pk=1)
        if platform.has_operator():
            return redirect("dashboard")
        request.user.is_superuser = request.user.is_staff = True
        request.user.save(update_fields=["is_superuser", "is_staff"])
    Change.log(_who(request), "Became the operator of this TipPool site")
    messages.success(request, "You run this TipPool site now. Set up email and the rest below.")
    return redirect("platform_settings")


# --- email ---------------------------------------------------------------------------------
GUIDE = {
    "gmail": "Gmail needs an app password, not your normal one: Google Account → Security → turn on 2-Step "
             "Verification → App passwords → create one named TipPool, and paste the 16 letters here.",
    "outlook": "Use your Outlook / Microsoft 365 email and password. If your account has two-step sign-in, "
               "create an app password in your Microsoft account's security settings.",
    "brevo": "In Brevo: SMTP & API → API Keys → Generate a new API key, and paste it here. "
             "Free for up to 300 emails a day.",
}


def _save_email(request, cfg):
    provider = request.POST.get("email_provider", "")
    if provider not in PlatformConfig.EmailProvider.values:
        messages.error(request, "Choose which email service to send from.")
        return redirect(f"{reverse('platform_settings')}#email")
    if provider in platform.EMAIL_PRESETS:
        host, port = platform.EMAIL_PRESETS[provider]
    else:
        host = request.POST.get("email_host", "").strip()[:120]
        port = int(request.POST.get("email_port") or 587) if str(request.POST.get("email_port", "587")).isdigit() else 587
    user = request.POST.get("email_user", "").strip()[:200]
    if provider == "brevo":
        return _save_brevo(request, cfg, host, port, user)
    password = request.POST.get("email_password", "").replace(" ", "") if provider == "gmail" else \
        request.POST.get("email_password", "").strip()
    if not password and (host != cfg.email_host or user != cfg.email_user):
        messages.error(request, "You changed the server or the address, so type the password again.")
        return redirect(f"{reverse('platform_settings')}#email")
    password = password or cfg.email_password
    if not (host and user and password):
        messages.error(request, "Fill in the email address and its password (and the server for another service).")
        return redirect(f"{reverse('platform_settings')}#email")
    try:  # log in once, so a typo shows up now instead of when someone needs a code
        import ssl
        context = ssl.create_default_context()  # the server's certificate is checked
        connection = (smtplib.SMTP_SSL(host, port, timeout=15, context=context) if port == 465
                      else smtplib.SMTP(host, port, timeout=15))
        if port != 465:
            connection.starttls(context=context)
        connection.login(user, password)
        connection.quit()
    except smtplib.SMTPAuthenticationError:
        messages.error(request, "The email service didn't accept that address and password. "
                       + GUIDE.get(provider, "Check them with your email provider."))
        return redirect(f"{reverse('platform_settings')}#email")
    except (OSError, smtplib.SMTPException):
        messages.error(request, f"Couldn't reach {host}. Check the server name and port, or try again in a minute.")
        return redirect(f"{reverse('platform_settings')}#email")
    cfg.email_provider, cfg.email_host, cfg.email_port, cfg.email_user = provider, host, port, user
    cfg.email_password_encrypted = encrypt(password)
    cfg.email_from = request.POST.get("email_from", "").strip()[:200]
    cfg.save()
    return _done(request, f"email connected: TipPool sends from {cfg.email_from or user}", "email")


def _save_brevo(request, cfg, host, port, sender):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from .mailer import brevo_check
    key = request.POST.get("email_password", "").strip()
    if key.startswith("xsmtpsib-"):
        messages.error(request, "That's a Brevo SMTP key. TipPool needs an API key instead: in Brevo, SMTP & API → "
                                "API Keys → Generate a new API key (it starts with xkeysib-).")
        return redirect(f"{reverse('platform_settings')}#email")
    try:
        validate_email(sender)
    except ValidationError:
        messages.error(request, "Type the address emails are sent from (a sender confirmed in Brevo).")
        return redirect(f"{reverse('platform_settings')}#email")
    if not key and (cfg.email_provider != "brevo" or sender != cfg.email_user):
        messages.error(request, "Paste your Brevo API key.")
        return redirect(f"{reverse('platform_settings')}#email")
    key = key or cfg.email_password
    from email.utils import parseaddr
    send_as = parseaddr(request.POST.get("email_from", "").strip())[1] or sender
    problem = brevo_check(key, send_as)
    if problem:
        messages.error(request, problem)
        return redirect(f"{reverse('platform_settings')}#email")
    cfg.email_provider, cfg.email_host, cfg.email_port, cfg.email_user = "brevo", host, port, sender
    cfg.email_password_encrypted = encrypt(key)
    cfg.email_from = request.POST.get("email_from", "").strip()[:200]
    cfg.save()
    return _done(request, f"email connected through Brevo: TipPool sends from {cfg.email_from or sender}", "email")


def _test_email(request, cfg):
    if platform.email_settings() is None:
        messages.error(request, "Connect an email account first.")
        return redirect(f"{reverse('platform_settings')}#email")
    to = request.user.email or cfg.email_user
    try:
        from . import emails
        emails.build(to, "TipPool test email", eyebrow="Platform settings", heading="Email works",
                     paragraphs=["TipPool can send email now: invites, password codes and reset links, alerts and "
                                 "summaries, for every business on this site."],
                     details=[("Sent with", cfg.get_email_provider_display()), ("From", cfg.email_from or cfg.email_user)],
                     button=("Open Platform settings", emails.absolute(reverse("platform_settings")))
                     ).send(fail_silently=False)
    except Exception as exc:  # show the reason, in a sentence
        from .mailer import BrevoError
        reason = str(exc) if isinstance(exc, BrevoError) else exc.__class__.__name__
        messages.error(request, f"The test email didn't go out: {reason}. Check the email settings.")
        return redirect(f"{reverse('platform_settings')}#email")
    messages.success(request, f"Test email sent to {to}. Check the inbox (and spam).")
    return redirect(f"{reverse('platform_settings')}#email")


# --- AI ---------------------------------------------------------------------------------------
def _other_ai(cfg):
    """The OpenAI-compatible services for the AI section: key saved? model in use?"""
    from .openai_compat import PROVIDERS
    return [{"value": value, "label": label, "key_url": key_url, "key_hint": prefix + "…" if prefix else "",
             "saved": bool((cfg.other_ai_keys or {}).get(value)),
             "model": (cfg.other_ai_models or {}).get(value, ""), "recommended": preferred[0]}
            for value, (label, _, key_url, prefix, preferred) in PROVIDERS.items()]


def _ai():
    from . import ai
    return ai


def _save_ai(request, cfg):
    raw = request.POST.get("ai_daily_limit", "").strip()
    if raw.isdigit():
        cfg.ai_daily_limit = min(int(raw), 100000)
    if request.POST.get("ai_provider") == "gemini":
        return _save_gemini(request, cfg)
    if request.POST.get("ai_provider") in platform.OTHER_AI:
        return _save_other_ai(request, cfg, request.POST["ai_provider"])
    cfg.ai_provider = "claude"
    key = request.POST.get("anthropic_key", "").strip()
    model = request.POST.get("claude_model", "")
    if key:
        try:
            import anthropic
            anthropic.Anthropic(api_key=key).models.list(limit=1)
        except Exception:
            messages.error(request, "Claude didn't accept that key. Copy it again from console.anthropic.com → API keys.")
            return redirect(f"{reverse('platform_settings')}#ai")
        cfg.anthropic_key_encrypted = encrypt(key)
    if model in dict(platform.CLAUDE_MODELS):
        cfg.claude_model = model
    cfg.ai_enabled = bool(request.POST.get("ai_enabled")) and bool(cfg.anthropic_key_encrypted)
    cfg.save()
    if request.POST.get("ai_enabled") and not cfg.anthropic_key_encrypted:
        messages.warning(request, "Paste a Claude key to switch Claude on. Until then the built-in assistant is used.")
        return redirect(f"{reverse('platform_settings')}#ai")
    return _done(request, "Claude is " + ("on" if cfg.ai_enabled else "off (the built-in assistant is used)"), "ai")


def _save_other_ai(request, cfg, provider):
    """ChatGPT, DeepSeek, Kimi or Mistral: check the key, pick (or check) the model, test it once."""
    from . import openai_compat
    from .vault import decrypt
    label = openai_compat.name(provider)
    keys, models_ = dict(cfg.other_ai_keys or {}), dict(cfg.other_ai_models or {})
    typed = request.POST.get(f"key_{provider}", "").strip()
    key = typed or (decrypt(keys[provider]) if keys.get(provider) else "")
    enable = bool(request.POST.get("ai_enabled"))
    if not key:
        if enable:
            messages.error(request, f"Paste your {label} API key ({openai_compat.PROVIDERS[provider][2]}).")
            return redirect(f"{reverse('platform_settings')}#ai")
        cfg.ai_provider, cfg.ai_enabled = provider, False
        cfg.save()
        return _done(request, f"{label} is off (the built-in assistant is used)", "ai")
    wanted = request.POST.get(f"model_{provider}", "").strip()[:80]
    model, problem = openai_compat.best_model(provider, key, wanted)
    if model and not problem:
        problem = openai_compat.check(provider, key, model)
    if problem:
        messages.error(request, problem)
        return redirect(f"{reverse('platform_settings')}#ai")
    keys[provider], models_[provider] = encrypt(key), model
    cfg.other_ai_keys, cfg.other_ai_models = keys, models_
    cfg.ai_provider, cfg.ai_enabled = provider, enable
    cfg.save()
    return _done(request, f"{label} ({model}) is " + ("on" if enable else "saved, but switched off"), "ai")


def _save_gemini(request, cfg):
    from . import gemini
    key = request.POST.get("gemini_key", "").strip() or (cfg.gemini_key if cfg.gemini_key_encrypted else "")
    enable = bool(request.POST.get("ai_enabled"))
    if not key:
        if enable:
            messages.error(request, "Paste your Gemini API key (from aistudio.google.com → Get API key).")
            return redirect(f"{reverse('platform_settings')}#ai")
        cfg.ai_provider, cfg.ai_enabled = "gemini", False
        cfg.save()
        return _done(request, "Gemini is off (the built-in assistant is used)", "ai")
    model, problem = gemini.best_model(key)
    if model and not problem:
        problem = gemini.check(key, model)
    if problem:
        messages.error(request, problem)
        return redirect(f"{reverse('platform_settings')}#ai")
    cfg.ai_provider, cfg.gemini_model, cfg.ai_enabled = "gemini", model, enable
    cfg.gemini_key_encrypted = encrypt(key)
    cfg.save()
    return _done(request, f"Gemini ({model}) is " + ("on" if enable else "saved, but switched off"), "ai")


# --- TipPool's own PayPal ----------------------------------------------------------------------------
def _save_paypal(request, cfg):
    from .paypal import LIVE_API, SANDBOX_API, PayPalClient, PayPalError, detect_mode
    client_id = request.POST.get("paypal_client_id", "").strip()
    secret = request.POST.get("paypal_secret", "").strip() or (cfg.paypal_secret if client_id == cfg.paypal_client_id else "")
    try:
        live = detect_mode(client_id, secret) if client_id and secret else None
    except PayPalError:
        messages.error(request, "Couldn't reach PayPal just now. Nothing was changed: try again in a minute.")
        return redirect(f"{reverse('platform_settings')}#paypal")
    if live is None:
        messages.error(request, "PayPal didn't accept those two codes. Copy the Client ID, then the Secret, again.")
        return redirect(f"{reverse('platform_settings')}#paypal")
    if live:
        messages.error(request, "Those are real-money (Live) codes. TipPool's own PayPal runs the demo, so use the "
                                "codes from the Sandbox tab. Each business connects its real PayPal in its Settings.")
        return redirect(f"{reverse('platform_settings')}#paypal")
    cfg.paypal_client_id, cfg.paypal_live = client_id, live
    cfg.paypal_secret_encrypted = encrypt(secret)
    cfg.paypal_webhook_id = _register_webhook(request, PayPalClient(client_id, secret,
                                                                     LIVE_API if live else SANDBOX_API))
    cfg.save()
    return _done(request, "TipPool's PayPal is connected" + (" (real money)" if live else " (test money)"), "paypal")


def _retry_webhook(request, cfg):
    from .paypal import LIVE_API, SANDBOX_API, PayPalClient
    if not cfg.paypal_client_id:
        return redirect(f"{reverse('platform_settings')}#paypal")
    hook = _register_webhook(request, PayPalClient(cfg.paypal_client_id, cfg.paypal_secret,
                                                   LIVE_API if cfg.paypal_live else SANDBOX_API))
    if hook:
        cfg.paypal_webhook_id = hook
        cfg.save(update_fields=["paypal_webhook_id"])
        return _done(request, "PayPal's live updates are on", "paypal")
    return redirect(f"{reverse('platform_settings')}#paypal")


def _register_webhook(request, client):
    """Ask PayPal to tell this site about payments and payouts. Returns the webhook id, or "" with a message
    saying why not (PayPal only sends to an https address, so not to a computer)."""
    url = request.build_absolute_uri(reverse("paypal_webhook"))
    if not url.startswith("https://"):
        messages.warning(request, "PayPal's live updates need your live site (an https address). On your computer, "
                                  "TipPool asks PayPal itself instead: it works, just a little slower.")
        return ""
    try:
        return client.ensure_webhook(url)[:60]
    except Exception:
        messages.warning(request, "PayPal didn't turn on live updates just now. TipPool asks PayPal itself meanwhile; "
                                  "try \"Turn on live updates\" again later.")
        return ""


def _disconnect_paypal(request, cfg):
    cfg.paypal_client_id = cfg.paypal_secret_encrypted = cfg.paypal_webhook_id = ""
    cfg.paypal_live = False
    cfg.save()
    return _done(request, "TipPool's PayPal was disconnected", "paypal")


# --- the demo, the scheduler, contact ----------------------------------------------------------------
def _valid_email(value):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    try:
        validate_email(value)
        return True
    except ValidationError:
        return False


def _save_demo(request, cfg):
    typed = [request.POST.get("demo_buyer_email", "").strip()] + [request.POST.get(f"staff_{n}", "").strip()
                                                                  for n in DEMO_STAFF]
    bad = [t for t in typed if t and not _valid_email(t)]
    if bad:
        messages.error(request, f"\"{bad[0][:60]}\" isn't an email address. Nothing was saved.")
        return redirect(f"{reverse('platform_settings')}#demo")
    cfg.demo_buyer_email = request.POST.get("demo_buyer_email", "").strip()[:200]
    cfg.demo_buyer_password = request.POST.get("demo_buyer_password", "").strip()[:100]
    cfg.demo_staff = {name: request.POST.get(f"staff_{name}", "").strip()[:200] for name in DEMO_STAFF
                      if request.POST.get(f"staff_{name}", "").strip()}
    cfg.save()
    return _done(request, "demo accounts saved", "demo")


def _new_cron(request, cfg):
    from .models import new_cron_secret
    cfg.cron_secret, cfg.cron_env_retired = new_cron_secret(), True  # the old address really stops working
    cfg.save()
    messages.warning(request, "New scheduler address made. Paste it into cron-job.org again: the old one stopped working.")
    return redirect(f"{reverse('platform_settings')}#scheduler")


def _save_contact(request, cfg):
    value = request.POST.get("contact_email", "").strip()[:200]
    if value and not _valid_email(value):
        messages.error(request, "That isn't an email address.")
        return redirect(f"{reverse('platform_settings')}#contact")
    cfg.contact_email = value
    cfg.save()
    return _done(request, "contact email saved", "contact")
