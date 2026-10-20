"""Business accounts: sign up, sign in with email, invite other managers.

Staff never need an account: they use their private tips link.
"""
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import roles as role_presets
from .models import Business, Change, Invite, Location, Manager
from .ratelimit import client_ip, hit, limited, over

User = get_user_model()

TIMEZONES = [
    ("America/New_York", "US Eastern"), ("America/Chicago", "US Central"), ("America/Denver", "US Mountain"),
    ("America/Los_Angeles", "US Pacific"), ("Europe/London", "UK"), ("Europe/Bucharest", "Romania"),
    ("Europe/Berlin", "Central Europe"), ("UTC", "UTC"),
]


def manager_required(view):
    """Signed in and managing a business. A login without a business is sent to finish signing up."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")
        if getattr(request, "business", None) is None or request.location is None:
            return redirect("signup")
        return view(request, *args, **kwargs)
    return wrapper


def managers_only(view):
    """Managers and owners, not shift leads (paying, rules, team, settings, reports)."""
    @wraps(view)
    @manager_required
    def wrapper(request, *args, **kwargs):
        if request.user.manager.is_lead:
            messages.error(request, "Shift leads can't open that. Ask a manager.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def owner_required(view):
    @wraps(view)
    @manager_required
    def wrapper(request, *args, **kwargs):
        if not request.user.manager.is_owner:
            messages.error(request, "Only the owner of the account can do that.")
            return redirect("settings")
        return view(request, *args, **kwargs)
    return wrapper


class EmailBackend(ModelBackend):
    """Sign in with your email (or your username, for older logins).
    Password guessing is limited per address and per IP."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        ip = client_ip(request) if request is not None else "local"
        name = username.strip().lower()
        # only wrong passwords count, so nobody can lock a manager out who knows their own password...
        if over(f"login-ip:{ip}", 50) or over(f"login-user:{name}", 10):
            if request is not None:
                request.login_limited = True
            return None
        user = (User.objects.filter(email__iexact=username.strip()).order_by("pk").first()
                or User.objects.filter(username__iexact=username.strip()).first())
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        # ...and guessing stops after 10 wrong tries per address (50 per network) for 15 minutes
        hit(f"login-ip:{ip}", 50, 900)
        hit(f"login-user:{name}", 10, 900)
        return None


def _new_user(email, password, name):
    first, _, last = name.strip().partition(" ")
    return User.objects.create_user(username=email.lower()[:150], email=email.lower(), password=password,
                                    first_name=first[:150], last_name=last[:150])


def _check_password(password, user=None):
    try:
        validate_password(password, user)
        return []
    except ValidationError as exc:
        return list(exc.messages)


# --- Sign up ------------------------------------------------------------------
@limited("signup", 5, 3600)
def signup(request):
    """New business: name, type, first location, and the owner's login.
    A login that exists but has no business (e.g. made in the admin) only fills in the business part."""
    finishing = request.user.is_authenticated
    if finishing and getattr(request.user, "manager", None):
        return redirect("dashboard")
    errors, data = [], {}
    if request.method == "POST":
        data = {k: request.POST.get(k, "").strip() for k in
                ("business", "location", "business_type", "timezone", "name", "email")}
        password = request.POST.get("password", "")
        if not data["business"]:
            errors.append("Enter your business name.")
        if data["business_type"] not in role_presets.PRESETS:
            data["business_type"] = "restaurant"
        if data["timezone"] not in dict(TIMEZONES):
            data["timezone"] = "America/New_York"
        if not finishing:
            if not data["name"]:
                errors.append("Enter your name.")
            if "@" not in data["email"]:
                errors.append("Enter a valid email.")
            elif User.objects.filter(email__iexact=data["email"]).exists() or \
                    User.objects.filter(username__iexact=data["email"]).exists():
                errors.append("There's already an account with that email. Sign in instead.")
            errors += _check_password(password)
        if not errors:
            with transaction.atomic():
                user = request.user if finishing else _new_user(data["email"], password, data["name"])
                business = Business.objects.create(name=data["business"][:80])
                Manager.objects.create(user=user, business=business, is_owner=True)
                location_name = data["location"] or data["business"]
                location = Location.objects.create(
                    business=business, name=location_name[:80], slug=Location.new_slug(location_name),
                    business_type=data["business_type"], timezone=data["timezone"])
                role_presets.apply_preset(location, data["business_type"])
                Change.log(user.get_username(), f"Created the business account {business.name}", location=location)
            if not finishing:
                login(request, user, backend="tips.accounts.EmailBackend")
            request.session["location_id"] = location.pk
            messages.success(request, f"Welcome to TipPool! {business.name} is ready. Follow the steps below to set it up.")
            return redirect("dashboard")
    return render(request, "registration/signup.html", {
        "errors": errors, "data": data, "finishing": finishing, "timezones": TIMEZONES,
        "presets": [(k, p["label"]) for k, p in role_presets.PRESETS.items()],
    })


# --- Staff: get your private tips link by email ---------------------------------------
@limited("my-link", 5, 3600)
def email_my_link(request):
    """A worker who lost their link types their email; the link goes to that inbox only.
    The answer is the same whether or not the email is on a team, so nobody can test emails here."""
    from django.core.cache import cache
    from .models import StaffMember
    sent = False
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        sent = True
        # at most one email per address every 2 minutes
        if "@" in email and cache.add(f"my-link:{email}", 1, 120):
            people = StaffMember.objects.filter(paypal_email__iexact=email, memberships__active=True).distinct()
            lines = [f"{p.business.name}: {request.build_absolute_uri(reverse('my_tips', args=[p.access_token]))}"
                     for p in people.select_related("business")]
            if lines:
                send_mail("Your TipPool tips link",
                          "Here is your private link to your tips. Keep it to yourself: anyone with it can see "
                          "your tips.\n\n" + "\n".join(lines) + "\n\nTip: add it to your phone's home screen.\n",
                          settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)
    return render(request, "registration/my_link.html", {"sent": sent})


# --- Invite other managers ----------------------------------------------------------
@require_POST
@owner_required
def invite_manager(request):
    email = request.POST.get("email", "").strip().lower()
    if "@" not in email:
        messages.error(request, "Enter their email.")
        return redirect("settings")
    if Manager.objects.filter(business=request.business, user__email__iexact=email).exists():
        messages.error(request, f"{email} already manages {request.business.name}.")
        return redirect("settings")
    level = Manager.Level.LEAD if request.POST.get("level") == "lead" else Manager.Level.MANAGER
    invite = Invite.objects.create(business=request.business, email=email, level=level,
                                   invited_by=request.user.get_username())
    link = request.build_absolute_uri(reverse("accept_invite", args=[invite.token]))
    send_mail(f"Join {request.business.name} on TipPool",
              f"{request.user.get_full_name() or request.user.get_username()} invited you to manage "
              f"{request.business.name} on TipPool.\n\nAccept here (valid for {Invite.VALID_DAYS} days):\n{link}\n",
              settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)
    Change.log(request, f"Invited {email} as a manager")
    messages.success(request, f"Invite sent to {email}. You can also copy the link below and send it yourself.")
    return redirect(f"{reverse('settings')}#managers")


@require_POST
@owner_required
def remove_manager(request, pk):
    manager = get_object_or_404(Manager, pk=pk, business=request.business)
    if manager.is_owner:
        messages.error(request, "The owner can't be removed.")
    else:
        name = manager.user.get_full_name() or manager.user.email or manager.user.get_username()
        manager.delete()
        Change.log(request, f"Removed {name} as a manager")
        messages.success(request, f"{name} can no longer sign in to {request.business.name}.")
    return redirect(f"{reverse('settings')}#managers")


@require_POST
@owner_required
def manager_level(request, pk):
    manager = get_object_or_404(Manager, pk=pk, business=request.business, is_owner=False)
    if request.POST.get("level") in Manager.Level.values:
        manager.level = request.POST["level"]
        manager.save(update_fields=["level"])
        name = manager.user.get_full_name() or manager.user.email or manager.user.get_username()
        Change.log(request, f"{name} is now a {manager.get_level_display().lower()}")
    return redirect(f"{reverse('settings')}#managers")


@require_POST
@owner_required
def cancel_invite(request, pk):
    invite = get_object_or_404(Invite, pk=pk, business=request.business, accepted_at__isnull=True)
    invite.delete()
    return redirect(f"{reverse('settings')}#managers")


@limited("invite", 10, 3600)
def accept_invite(request, token):
    invite = get_object_or_404(Invite, token=token)
    if not invite.usable:
        return render(request, "registration/invite.html", {"invite": invite, "dead": True})
    existing = User.objects.filter(email__iexact=invite.email).first()
    errors = []
    if request.method == "POST":
        if request.user.is_authenticated:
            if (request.user.email or "").lower() != invite.email.lower():
                errors.append(f"This invite is for {invite.email}. Sign in with that email to accept it.")
                return render(request, "registration/invite.html", {
                    "invite": invite, "existing": existing, "errors": errors})
            user = request.user
        elif existing:
            return redirect(f"{reverse('login')}?next={request.path}")
        else:
            name = request.POST.get("name", "").strip()
            password = request.POST.get("password", "")
            if not name:
                errors.append("Enter your name.")
            errors += _check_password(password)
            user = None if errors else _new_user(invite.email, password, name)
        if user is not None:
            if getattr(user, "manager", None) and user.manager.business_id != invite.business_id:
                errors.append(f"This login already manages {user.manager.business.name}. "
                              "Use a different email for this business.")
            elif not errors:
                Manager.objects.get_or_create(user=user, defaults={"business": invite.business, "level": invite.level})
                invite.accepted_at = timezone.now()
                invite.save(update_fields=["accepted_at"])
                Change.log(user.get_username(), f"Joined as a manager (invited by {invite.invited_by})",
                           location=invite.business.locations.first())
                if not request.user.is_authenticated:
                    login(request, user, backend="tips.accounts.EmailBackend")
                request.session.pop("location_id", None)
                messages.success(request, f"You're now a manager of {invite.business.name}.")
                return redirect("dashboard")
    return render(request, "registration/invite.html", {
        "invite": invite, "existing": existing, "errors": errors,
    })


# --- Payments: currency, fees, the business's own PayPal ---------------------------------
@require_POST
@owner_required
def payment_settings(request):
    from decimal import Decimal, InvalidOperation

    from .models import Tip
    from .paypal import LIVE_API, SANDBOX_API, PayPalClient, PayPalError
    business = request.business
    if request.POST.get("action") == "disconnect":
        business.paypal_client_id = business.paypal_secret_encrypted = business.paypal_webhook_id = ""
        business.paypal_live = False
        business.save()
        Change.log(request, "Disconnected the business's PayPal account")
        messages.success(request, "PayPal disconnected.")
        return redirect(f"{reverse('settings')}#payments")

    currency = request.POST.get("currency", business.currency)
    if currency != business.currency and currency in Business.CURRENCIES:
        if Tip.objects.filter(night__location__business=business).exists():
            messages.error(request, "The currency can't change once you have tips: past amounts would be wrong.")
        else:
            business.currency = currency
    if request.POST.get("fee_mode") in Business.FeeMode.values:
        business.fee_mode = request.POST["fee_mode"]
    try:
        percent = Decimal(request.POST.get("card_fee_percent", "0") or "0")
        if 0 <= percent <= 10:
            business.card_fee_percent = percent
    except InvalidOperation:
        pass

    client_id = request.POST.get("paypal_client_id", "").strip()
    secret = request.POST.get("paypal_secret", "").strip()
    live = bool(request.POST.get("paypal_live"))
    if client_id and (secret or client_id != business.paypal_client_id or live != business.paypal_live):
        secret = secret or business.paypal_secret
        try:
            PayPalClient(client_id, secret, LIVE_API if live else SANDBOX_API).access_token()
        except PayPalError:
            business.save()
            messages.error(request, "PayPal didn't accept that client ID and secret. Check them in your PayPal "
                                    "developer dashboard (Apps & Credentials) and that you picked the right mode.")
            return redirect(f"{reverse('settings')}#payments")
        business.paypal_client_id, business.paypal_live = client_id, live
        business.set_paypal_secret(secret)
        Change.log(request, f"Connected a {'live' if live else 'sandbox'} PayPal account")
    business.paypal_webhook_id = request.POST.get("paypal_webhook_id", business.paypal_webhook_id).strip()[:60]
    business.save()
    Change.log(request, f"Changed payment settings: {business.currency}, {business.get_fee_mode_display().lower()}")
    messages.success(request, "Payment settings saved.")
    return redirect(f"{reverse('settings')}#payments")


# --- Two-step sign-in ---------------------------------------------------------------
from django.contrib.auth.views import LoginView  # noqa: E402

TWO_STEP_SECONDS = 300


class TwoStepLoginView(LoginView):
    """Password first; people with two-step sign-in on then type the code from their app."""
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        profile = getattr(user, "manager", None)
        if profile and profile.totp_enabled:
            self.request.session["two_step"] = {"uid": user.pk, "backend": user.backend, "at": int(timezone.now().timestamp()),
                                                "next": self.get_success_url()}
            return redirect("login_code")
        return super().form_valid(form)


@limited("login-code", 12, 900)
def login_code(request):
    from django.utils.http import url_has_allowed_host_and_scheme
    from . import totp
    from .vault import decrypt
    pending = request.session.get("two_step")
    if not pending or timezone.now().timestamp() - pending["at"] > TWO_STEP_SECONDS:
        request.session.pop("two_step", None)
        messages.error(request, "Please sign in again.")
        return redirect("login")
    error = ""
    if request.method == "POST":
        user = User.objects.filter(pk=pending["uid"]).select_related("manager").first()
        if user and totp.verify(decrypt(user.manager.totp_secret_encrypted), request.POST.get("code", "")):
            request.session.pop("two_step", None)
            login(request, user, backend=pending["backend"])
            nxt = pending.get("next") or reverse("dashboard")
            safe = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
            return redirect(nxt if safe else "dashboard")
        error = "That code isn't right. Use the newest code in your authenticator app."
    return render(request, "registration/login_code.html", {"error": error})


@manager_required
def two_step(request):
    """Turn two-step sign-in on (scan, then confirm one code) or off (needs a current code)."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    from . import totp
    from .vault import decrypt, encrypt
    profile = request.user.manager
    if request.method == "POST":
        action = request.POST.get("action")
        code = request.POST.get("code", "")
        if action == "start":
            request.session["two_step_new"] = totp.new_secret()
        elif action == "confirm":
            secret = request.session.get("two_step_new", "")
            if totp.verify(secret, code):
                profile.totp_secret_encrypted, profile.totp_enabled = encrypt(secret), True
                profile.save(update_fields=["totp_secret_encrypted", "totp_enabled"])
                request.session.pop("two_step_new", None)
                Change.log(request, "Turned on two-step sign-in")
                messages.success(request, "Two-step sign-in is on. You'll type a code from your app after your password.")
            else:
                messages.error(request, "That code didn't match. Scan the code again and type the newest number.")
        elif action == "disable":
            if totp.verify(decrypt(profile.totp_secret_encrypted), code):
                profile.totp_secret_encrypted, profile.totp_enabled = "", False
                profile.save(update_fields=["totp_secret_encrypted", "totp_enabled"])
                Change.log(request, "Turned off two-step sign-in")
                messages.success(request, "Two-step sign-in is off.")
            else:
                messages.error(request, "That code didn't match, so two-step sign-in stays on.")
        elif action == "cancel":
            request.session.pop("two_step_new", None)
        return redirect(f"{reverse('settings')}#signin")
    return redirect(f"{reverse('settings')}#signin")


def two_step_setup_context(request):
    """For the settings page: the QR code to scan while turning two-step sign-in on."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    from . import totp
    secret = request.session.get("two_step_new")
    if not secret:
        return {}
    account = request.user.email or request.user.get_username()
    image = qrcode.make(totp.setup_uri(secret, account), image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return {"two_step_secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "two_step_qr": "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()}


# --- Try the demo ---------------------------------------------------------------------
@require_POST
@limited("demo", 5, 3600)
def demo_start(request):
    """One click: a fresh demo business full of sample data, signed in. For judges and the curious."""
    from . import demo
    user = demo.create_demo()
    login(request, user, backend="tips.accounts.EmailBackend")
    request.session.pop("location_id", None)
    messages.success(request, "Welcome to the demo: Luna Bistro, with three weeks of sample shifts. Yesterday's shift "
                              "is waiting for you to check and pay. Nothing here is real money.")
    return redirect("dashboard")


@require_POST
@managers_only
def load_sample(request):
    """An empty, new business can fill itself with sample data to look around (sandbox only)."""
    from . import demo
    from .models import Tip
    if request.business.paypal_live or Tip.objects.filter(night__location__business=request.business).exists():
        messages.error(request, "Sample data can only go into a new, empty sandbox business.")
        return redirect("dashboard")
    demo.fill(request.location)
    Change.log(request, "Loaded sample data")
    messages.success(request, "Sample data loaded: a team, tip rules and three weeks of shifts. Yesterday's shift is "
                              "ready to check and pay.")
    return redirect("dashboard")


def cron_tick(request):
    """Called every few minutes by an outside scheduler (and keeps a free server awake):
    runs automatic close-outs and removes week-old demos. Needs ?key=CRON_SECRET."""
    import hmac

    from django.http import Http404, JsonResponse

    from . import autoclose, demo
    secret = settings.CRON_SECRET
    if not secret or not hmac.compare_digest(request.GET.get("key", ""), secret):
        raise Http404()
    done = autoclose.run()
    removed = demo.cleanup()
    return JsonResponse({"closed": len(done), "demos_removed": removed})
