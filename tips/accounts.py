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
from django.core.validators import validate_email
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import emails, roles as role_presets
from .models import Business, Change, Invite, Location, Manager
from .ratelimit import client_ip, hit, limited, over

User = get_user_model()

from .timezones import TIMEZONES  # noqa: E402


def manager_required(view):
    """Signed in and managing a business. A login without a business is sent to finish signing up."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")
        if getattr(request, "business", None) is None:
            return redirect("signup")
        if request.location is None:  # e.g. every location they run was archived
            return render(request, "registration/no_locations.html", status=403)
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
            return redirect("dashboard" if request.user.manager.is_lead else "settings")
        return view(request, *args, **kwargs)
    return wrapper


WRONG_IN_A_ROW = 10
PAUSE_SECONDS = 60
# each new round of 10 wrong tries within a day pauses longer: 1, then 5, then 15 minutes
PAUSES = (PAUSE_SECONDS, 5 * 60, 15 * 60)


class EmailBackend(ModelBackend):
    """Sign in with your email (or your username, for older logins).
    10 wrong passwords in a row for an account pause it (a minute, longer each time it happens again that day)
    and email its owner; a network that keeps guessing (50 wrong tries) waits 15 minutes."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        from django.core.cache import cache
        if not username or not password:
            return None
        ip = client_ip(request) if request is not None else "local"
        name = username.strip().lower()
        limits_on = getattr(settings, "RATELIMIT_ENABLED", True)
        if over(f"login-ip:{ip}", 50):
            if request is not None:
                request.login_limited = "network"
            return None
        user = (User.objects.filter(email__iexact=username.strip()).order_by("pk").first()
                or User.objects.filter(username__iexact=username.strip()).first())
        name = f"user-{user.pk}" if user is not None else name  # email or username: one account, one count
        if limits_on and cache.get(f"login-pause:{name}"):
            if request is not None:
                request.login_limited = "pause"
            return None
        if user is None:
            User().set_password(password)  # same time as a real check, so timing can't tell who has an account
        elif user.check_password(password) and self.user_can_authenticate(user):
            cache.delete_many([f"login-streak:{name}", f"login-rounds:{name}"])  # only wrong tries *in a row* count
            return user
        hit(f"login-ip:{ip}", 50, 900)
        if not limits_on:
            return None
        streak_key = f"login-streak:{name}"
        if cache.add(streak_key, 1, 900):
            streak = 1
        else:
            try:
                streak = cache.incr(streak_key)
            except ValueError:
                cache.add(streak_key, 1, 900)
                streak = 1
        if streak >= WRONG_IN_A_ROW:
            rounds_key = f"login-rounds:{name}"
            if cache.add(rounds_key, 1, 24 * 3600):
                rounds = 1
            else:
                try:
                    rounds = cache.incr(rounds_key)
                except ValueError:
                    cache.add(rounds_key, 1, 24 * 3600)
                    rounds = 1
            pause = PAUSES[min(rounds, len(PAUSES)) - 1]
            cache.set(f"login-pause:{name}", 1, pause)
            cache.delete(streak_key)
            if request is not None:
                request.login_limited = "pause"
            if user is not None and user.email and cache.add(f"login-warned:{user.pk}", 1, 3600):
                _warn_owner(user, ip, pause)  # at most once an hour, so nobody can flood an inbox with these
        return None


def _warn_owner(user, ip, pause=PAUSE_SECONDS):
    """Tell the account's owner someone kept typing the wrong password."""
    when = timezone.localtime().strftime("%b %d, %I:%M %p")
    minutes = max(1, pause // 60)
    how_long = "a minute" if minutes == 1 else f"{minutes} minutes"
    emails.send(user.email, "Someone kept trying to sign in to your TipPool account",
                eyebrow="Security alert", heading="Someone kept trying your password",
                greeting=f"Hi {user.get_full_name() or user.get_username()},",
                paragraphs=[f"Someone typed the wrong password {WRONG_IN_A_ROW} times in a row for your TipPool "
                            f"account, so signing in to it is paused for {how_long}."],
                details=[("Account", user.email), ("When", when), ("From", ip)],
                button=("Reset your password", emails.absolute(reverse("password_reset"))),
                notes=[f"If it was you: wait {how_long}, or reset your password.",
                       "If it wasn't you: your password is still safe, but change it to be sure, and turn on two-step "
                       "sign-in (Settings → Your sign-in) so a password alone is never enough."])


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
    profile = getattr(request.user, "manager", None) if request.user.is_authenticated else None
    if profile is not None and profile.business.is_demo:
        from django.contrib.auth import logout
        logout(request)  # leaving the demo to make a real account
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
            try:
                validate_email(data["email"])
            except ValidationError:
                errors.append("Enter a valid email.")
            else:
                if User.objects.filter(email__iexact=data["email"]).exists() or \
                        User.objects.filter(username__iexact=data["email"]).exists():
                    errors.append("There's already an account with that email. Sign in instead.")
            errors += _check_password(password)
        if not errors:
            with transaction.atomic():
                user = request.user if finishing else _new_user(data["email"], password, data["name"])
                business = Business.objects.create(name=" ".join(data["business"].split())[:80])
                Manager.objects.create(user=user, business=business, is_owner=True)
                location_name = " ".join((data["location"] or data["business"]).split())
                location = Location.objects.create(
                    business=business, name=location_name[:80], slug=Location.new_slug(location_name),
                    business_type=data["business_type"], timezone=data["timezone"],
                    tip_mode=role_presets.default_tip_mode(data["business_type"]))
                role_presets.apply_preset(location, data["business_type"])
                Change.log(user.get_full_name() or user.get_username(), f"Created the business account {business.name}",
                           location=location)
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
            links = [(p.business.name, request.build_absolute_uri(reverse("my_tips", args=[p.access_token])))
                     for p in people.select_related("business")]
            if links:
                emails.send(email, "Your TipPool tips link", eyebrow="Your tips", heading="Your private tips link",
                            paragraphs=["Your tips, how each share was worked out, and clocking in and out."],
                            button=("Open my tips", links[0][1]) if len(links) == 1 else None, links=links,
                            notes=["Keep it to yourself: anyone with it can see your tips.",
                                   "Tip: open it on your phone and add it to the home screen."])
    return render(request, "registration/my_link.html", {"sent": sent})


# --- Invite other managers ----------------------------------------------------------
@require_POST
@owner_required
def invite_manager(request):
    if request.business.is_demo:
        messages.error(request, "Demo businesses can't invite people. Create your own account to invite your managers.")
        return redirect(f"{reverse('settings')}#managers")
    email = request.POST.get("email", "").strip().lower()[:254]
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter their email address.")
        return redirect(f"{reverse('settings')}#managers")
    if Manager.objects.filter(business=request.business, user__email__iexact=email).exists():
        messages.error(request, f"{email} already manages {request.business.name}.")
        return redirect("settings")
    level = Manager.Level.LEAD if request.POST.get("level") == "lead" else Manager.Level.MANAGER
    # a new invite replaces an older one to the same person
    request.business.invites.filter(email__iexact=email, accepted_at__isnull=True).delete()
    invite = Invite.objects.create(business=request.business, email=email, level=level,
                                   invited_by=request.user.get_username())
    invite.locations.set(Location.objects.filter(business=request.business, archived=False,
                                                 pk__in=[p for p in request.POST.getlist("locations") if p.isdigit()]))
    link = request.build_absolute_uri(reverse("accept_invite", args=[invite.token]))
    where = ", ".join(loc.display_name for loc in invite.locations.all())
    role = "a shift lead" if level == Manager.Level.LEAD else "a manager"
    inviter = request.user.get_full_name() or request.user.get_username()
    emails.send(email, f"Join {request.business.name} on TipPool", eyebrow=request.business.name,
                heading=f"Join {request.business.name} on TipPool",
                paragraphs=[f"{inviter} invited you to be {role} at {request.business.name}"
                            + (f", for {where}" if where else "") + "."],
                details=[("Role", "Shift lead" if level == Manager.Level.LEAD else "Manager"),
                         ("Locations", where or "All locations"), ("Valid for", f"{Invite.VALID_DAYS} days")],
                button=("Accept the invite", link), links=[("Or copy this link", link)],
                notes=["Didn't expect this? You can ignore this email."])
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
def paypal_live_updates(request):
    """Turn on PayPal's live updates (webhook) for the business's own PayPal account."""
    from .operator import _register_webhook
    from .paypal import LIVE_API, SANDBOX_API, PayPalClient
    business = request.business
    if business.paypal_connected and not business.is_demo:
        hook = _register_webhook(request, PayPalClient(business.paypal_client_id, business.paypal_secret,
                                                       LIVE_API if business.paypal_live else SANDBOX_API))
        if hook:
            business.paypal_webhook_id = hook
            business.save(update_fields=["paypal_webhook_id"])
            messages.success(request, "PayPal's live updates are on.")
    return redirect(f"{reverse('settings')}#payments")


@require_POST
@owner_required
def manager_two_step_off(request, pk):
    """A manager lost their phone: the owner turns their two-step sign-in off, so they can sign in and set it
    up again."""
    manager = get_object_or_404(Manager, pk=pk, business=request.business, is_owner=False)
    manager.totp_enabled, manager.totp_secret_encrypted = False, ""
    manager.save(update_fields=["totp_enabled", "totp_secret_encrypted"])
    name = manager.user.get_full_name() or manager.user.email or manager.user.get_username()
    Change.log(request, f"Turned off two-step sign-in for {name} (lost their phone)")
    messages.success(request, f"Two-step sign-in is off for {name}. Ask them to turn it on again after signing in.")
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
                manager, made = Manager.objects.get_or_create(
                    user=user, defaults={"business": invite.business, "level": invite.level})
                if made:
                    manager.locations.set(invite.locations.all())
                invite.accepted_at = timezone.now()
                invite.save(update_fields=["accepted_at"])
                Change.log(user.get_full_name() or user.get_username(), f"Joined as a manager (invited by {invite.invited_by})",
                           location=invite.business.locations.first())
                if not request.user.is_authenticated:
                    login(request, user, backend="tips.accounts.EmailBackend")
                request.session.pop("location_id", None)
                role = "a shift lead" if invite.level == Manager.Level.LEAD else "a manager"
                messages.success(request, f"You're now {role} at {invite.business.name}.")
                return redirect("dashboard")
    return render(request, "registration/invite.html", {
        "invite": invite, "existing": existing, "errors": errors,
    })


# --- Payments: currency, fees, the business's own PayPal ---------------------------------
def _paypal_switch_blocked(business, client_id, live):
    """Why another PayPal account (or real money after test money) can't take over yet, or "".
    Payouts in progress belong to the old account, and test tips must never be paid out with real money."""
    from . import services
    from .models import Night, PayoutBatch, Tip
    if client_id == business.paypal_client_id and live == business.paypal_live:
        return ""
    nights = Night.objects.filter(location__business=business)
    if nights.filter(status__in=[Night.Status.REVIEW, Night.Status.PAYING]).exists():
        return "a shift is waiting to be paid or being paid. Pay it (or recalculate it) first, then try again."
    if any(services.needs_recheck(b) for b in PayoutBatch.objects.filter(night__location__business=business)
           .exclude(paypal_batch_id__gt="")):
        return "a payout's answer from PayPal is still unclear. Check it again on its shift first."
    if live and not business.paypal_live and Tip.objects.filter(
            night__in=nights.exclude(status=Night.Status.PAID), source=Tip.Source.PAYPAL,
            status=Tip.Status.COMPLETED).exists():
        return ("some unpaid shifts have tips paid with test money. Close and pay them with the test account first, "
                "so test money is never paid out as real money.")
    return ""


@require_POST
@owner_required
def payment_settings(request):
    from decimal import Decimal, InvalidOperation

    from .models import Tip
    from .paypal import LIVE_API, SANDBOX_API, PayPalClient, PayPalError, detect_mode
    business = request.business
    if request.POST.get("action") == "disconnect":
        # payouts in progress belong to this account: checking them with another one could mark them wrongly
        stop = _paypal_switch_blocked(business, "", False) if business.paypal_client_id else ""
        if stop:
            messages.error(request, "PayPal wasn't disconnected: " + stop)
            return redirect(f"{reverse('settings')}#payments")
        business.paypal_client_id = business.paypal_secret_encrypted = business.paypal_webhook_id = ""
        business.paypal_live = False
        business.save()
        Change.log(request, "Disconnected the business's PayPal account")
        messages.success(request, "PayPal disconnected.")
        return redirect(f"{reverse('settings')}#payments")

    ignored = []
    currency = request.POST.get("currency", business.currency)
    if currency != business.currency:
        if currency not in Business.CURRENCIES:
            ignored.append("the currency (pick one from the list)")
        elif Tip.objects.filter(night__location__business=business).exists():
            ignored.append("the currency (it can't change once you have tips: past amounts would be wrong)")
        else:
            business.currency = currency
    if request.POST.get("fee_mode") in Business.FeeMode.values:
        business.fee_mode = request.POST["fee_mode"]
    try:
        percent = Decimal(request.POST.get("card_fee_percent", "0") or "0")
        if percent.is_finite() and 0 <= percent <= 10:
            business.card_fee_percent = percent.quantize(Decimal("0.01"))
        else:
            ignored.append("the card fee (a percent from 0 to 10)")
    except InvalidOperation:
        ignored.append("the card fee (a percent from 0 to 10)")

    client_id = request.POST.get("paypal_client_id", "").strip()
    secret = request.POST.get("paypal_secret", "").strip()
    connected_now = False
    if business.is_demo and client_id:
        business.save()
        messages.error(request, "The demo always runs on test money. Create your own account to connect your PayPal.")
        return redirect(f"{reverse('settings')}#payments")
    if client_id and (secret or client_id != business.paypal_client_id):
        secret = secret or business.paypal_secret
        try:
            live = detect_mode(client_id, secret)  # real-money keys or test keys: PayPal tells us
        except PayPalError:
            business.save()
            messages.error(request, "Your other payment settings were saved, but PayPal couldn't be reached just now. "
                                    "Try connecting again in a minute.")
            return redirect(f"{reverse('settings')}#payments")
        if live is None:
            business.save()  # everything else in the form is kept
            messages.error(request, "Your other payment settings were saved, but PayPal didn't accept those two codes. "
                                    "Copy them again from PayPal (step 4 below): the Client ID, then the Secret.")
            return redirect(f"{reverse('settings')}#payments")
        stop = _paypal_switch_blocked(business, client_id, live)
        if stop:
            business.save()
            messages.error(request, "Your other payment settings were saved, but PayPal wasn't switched: " + stop)
            return redirect(f"{reverse('settings')}#payments")
        business.paypal_client_id, business.paypal_live = client_id, live
        business.set_paypal_secret(secret)
        # PayPal tells TipPool about payments and payouts as they happen (needs the site's https address)
        from .operator import _register_webhook
        business.paypal_webhook_id = _register_webhook(request, PayPalClient(client_id, secret,
                                                                          LIVE_API if live else SANDBOX_API))
        connected_now = True
        Change.log(request, f"Connected a {'real-money' if live else 'test (sandbox)'} PayPal account")
    business.save()
    Change.log(request, f"Changed payment settings: {business.currency}, {business.get_fee_mode_display().lower()}")
    if ignored:
        messages.warning(request, "Saved, except " + " and ".join(ignored) + ".")
    elif connected_now:
        messages.success(request, "PayPal is connected" + (" for real money." if business.paypal_live else
                                  " in test mode (sandbox): no real money moves until you use your live codes."))
    else:
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


CODE_TRIES = 5
CODE_LOCK_SECONDS = 900


@limited("login-code", 12, 900)
def login_code(request):
    from django.core.cache import cache
    from django.utils.http import url_has_allowed_host_and_scheme
    from . import totp
    from .vault import decrypt
    pending = request.session.get("two_step")
    if not pending or timezone.now().timestamp() - pending["at"] > TWO_STEP_SECONDS:
        request.session.pop("two_step", None)
        messages.error(request, "Please sign in again.")
        return redirect("login")
    uid = pending["uid"]
    if cache.get(f"login-code-lock:{uid}"):
        request.session.pop("two_step", None)
        messages.error(request, "Too many wrong codes for this account. Please try again in 15 minutes.")
        return redirect("login")
    error = ""
    if request.method == "POST":
        user = User.objects.filter(pk=uid).select_related("manager").first()
        step = totp.matching_step(decrypt(user.manager.totp_secret_encrypted), request.POST.get("code", "")) \
            if user else None
        used = cache.get(f"login-code-used:{uid}")
        if step is not None and (used is None or step > used):
            cache.set(f"login-code-used:{uid}", step, 120)  # the same code can't sign in twice
            cache.delete(f"login-code-fails:{uid}")
            request.session.pop("two_step", None)
            login(request, user, backend=pending["backend"])
            nxt = pending.get("next") or reverse("dashboard")
            safe = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
            return redirect(nxt if safe else "dashboard")
        fails_key = f"login-code-fails:{uid}"
        if not cache.add(fails_key, 1, CODE_LOCK_SECONDS):
            try:
                fails = cache.incr(fails_key)
            except ValueError:
                cache.add(fails_key, 1, CODE_LOCK_SECONDS)
                fails = 1
        else:
            fails = 1
        if fails >= CODE_TRIES:
            cache.set(f"login-code-lock:{uid}", 1, CODE_LOCK_SECONDS)
            cache.delete(fails_key)
            request.session.pop("two_step", None)
            if user and user.email and cache.add(f"login-code-warned:{uid}", 1, 3600):
                emails.send(user.email, "Someone has your TipPool password", eyebrow="Security alert",
                            heading="Someone has your password",
                            greeting=f"Hi {user.get_full_name() or user.get_username()},",
                            paragraphs=[f"Someone typed your TipPool password correctly, then got the two-step code "
                                        f"wrong {CODE_TRIES} times. Your two-step sign-in stopped them."],
                            details=[("Account", user.email), ("From", client_ip(request))],
                            button=("Change your password", emails.absolute(reverse("password_change"))),
                            notes=["Please change your password now, and anywhere else you use the same one."])
            messages.error(request, "Too many wrong codes for this account. Please try again in 15 minutes.")
            return redirect("login")
        error = "That code isn't right. Use the newest code in your authenticator app."
    return render(request, "registration/login_code.html", {"error": error})


@manager_required
def two_step(request):
    """Turn two-step sign-in on (scan, then confirm one code) or off (needs a current code)."""


    from . import totp
    from .vault import decrypt, encrypt
    profile = request.user.manager
    if request.method == "POST":
        action = request.POST.get("action")
        code = request.POST.get("code", "")
        if action in ("confirm", "disable") and not hit(f"two-step-code:{request.user.pk}", 10, 900):
            # a stolen session can't guess its way to turning two-step sign-in off
            messages.error(request, "Too many codes typed. Wait 15 minutes, then try again.")
            return redirect(_signin_page(request))
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
        return redirect(_signin_page(request))
    if profile.is_lead:  # shift leads can't open Settings: their sign-in gets a page of its own
        return render(request, "tips/signin.html", {"profile": profile, **two_step_setup_context(request)})
    return redirect(_signin_page(request))


def _signin_page(request):
    return reverse("two_step") if request.user.manager.is_lead else f"{reverse('settings')}#signin"


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
@limited("demo", 5, 3600)
def demo_start(request):
    if request.method != "POST":
        return redirect("login")
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
        messages.error(request, "Sample data is only for a brand-new business: this business already has tips (at "
                                "any of its locations) or is connected to real PayPal. Try the demo instead.")
        return redirect("dashboard")
    demo.fill(request.location)
    Change.log(request, "Loaded sample data")
    messages.success(request, "Sample data loaded: a team, tip rules and three weeks of shifts. Yesterday's shift is "
                              "ready to check and pay.")
    return redirect("dashboard")


def cron_tick(request):
    """Called every few minutes by an outside scheduler (and keeps a free server awake):
    runs automatic close-outs, smart alerts and summary emails, and removes week-old demos.
    Needs the key: in an X-Cron-Key header (kept out of web logs), or ?key=CRON_SECRET."""
    import hmac

    from django.http import Http404, JsonResponse

    from . import autoclose, demo, digest, watch
    from . import platform
    from .models import PlatformConfig
    given = request.headers.get("X-Cron-Key") or request.GET.get("key", "")
    if not any(hmac.compare_digest(given, key) for key in platform.cron_secrets()):
        raise Http404()
    PlatformConfig.objects.filter(pk=1).update(last_tick_at=timezone.now())  # shown on Platform settings
    done = autoclose.run()
    alerts = watch.run()
    sent = digest.run(base_url=request.build_absolute_uri("/"))
    removed = demo.cleanup()
    return JsonResponse({"closed": len(done), "alerts": len(alerts), "digests": sent, "demos_removed": removed})


# --- Owner controls ---------------------------------------------------------------------
@require_POST
@owner_required
def owner_controls(request):
    from decimal import Decimal, InvalidOperation
    business = request.business
    raw = request.POST.get("approval_limit", "").strip()
    try:
        limit = Decimal(raw) if raw else None
        if limit is not None and not (limit.is_finite() and 1 <= limit <= 1000000):
            raise InvalidOperation
    except InvalidOperation:
        messages.error(request, "The approval limit must be an amount from 1 to 1,000,000, or empty for no limit.")
        return redirect(f"{reverse('settings')}#owner")
    business.approval_limit = limit.quantize(Decimal("0.01")) if limit is not None else None
    business.smart_alerts = bool(request.POST.get("smart_alerts"))
    if request.POST.get("digest") in Business.Digest.values:
        business.digest = request.POST["digest"]
    business.save(update_fields=["approval_limit", "smart_alerts", "digest"])
    Change.log(request, "Owner controls: " + ", ".join([
        f"approval limit {business.symbol}{business.approval_limit}" if business.approval_limit else "no approval limit",
        "smart alerts " + ("on" if business.smart_alerts else "off"),
        f"summary email {business.get_digest_display().lower()}"]))
    messages.success(request, "Owner controls saved.")
    return redirect(f"{reverse('settings')}#owner")


@require_POST
@owner_required
def freeze_payouts(request):
    """The owner's emergency stop for every payout in every location (tips keep coming in)."""
    from .alerts import email_managers
    business = request.business
    who = request.user.get_full_name() or request.user.get_username()
    if request.POST.get("frozen") == "1":
        business.payouts_frozen_at, business.payouts_frozen_by = timezone.now(), who
        text = f"{who} froze all payouts for {business.name}. Tips still come in; nothing is paid out until they're turned back on."
    else:
        business.payouts_frozen_at, business.payouts_frozen_by = None, ""
        text = f"{who} turned payouts back on for {business.name}."
    business.save(update_fields=["payouts_frozen_at", "payouts_frozen_by"])
    Change.log(request, text)
    email_managers(business, "Payouts frozen" if business.payouts_frozen else "Payouts are back on", text,
                   page="dashboard")
    messages.warning(request, text) if business.payouts_frozen else messages.success(request, text)
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get("next", "")
    safe = nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure())
    return redirect(nxt if safe else f"{reverse('settings')}#owner")


@require_POST
@owner_required
def manager_locations(request, pk):
    manager = get_object_or_404(Manager, pk=pk, business=request.business, is_owner=False)
    picked = Location.objects.filter(business=request.business, archived=False,
                                     pk__in=[p for p in request.POST.getlist("locations") if p.isdigit()])
    manager.locations.set(picked)
    name = manager.user.get_full_name() or manager.user.get_username()
    where = ", ".join(loc.display_name for loc in picked) or "all locations"
    Change.log(request, f"{name} now runs: {where}")
    messages.success(request, f"{name} now runs {where}.")
    return redirect(f"{reverse('settings')}#managers")


# --- Change your password: prove it's you with a code sent to your email -----------------
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to mix up
CODE_MINUTES = 5
CODE_TRIES = 5


def _code_hash(user, code):
    import hashlib
    import hmac
    return hmac.new(settings.SECRET_KEY.encode(), f"pw-change:{user.pk}:{code}".encode(), hashlib.sha256).hexdigest()


def _masked(email):
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 2)}@{domain}"


def change_password(request):
    """Three steps: we email a 6-character code, you type it, then you choose the new password.
    Without email on the server, the current password is asked instead (nobody gets locked out)."""
    import secrets
    import time

    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
    from django.contrib.auth.views import redirect_to_login

    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    user, session, now = request.user, request.session, int(time.time())
    profile = getattr(user, "manager", None)
    if profile is not None and profile.business.is_demo:
        return render(request, "registration/password_change_form.html", {"step": "demo"})

    def finish(form):
        form.save()
        update_session_auth_hash(request, form.user)  # stay signed in here; other devices are signed out
        for key in ("pw_code", "pw_ok_at"):
            session.pop(key, None)
        Change.log(request, "Changed their password")
        if user.email:
            _password_changed_email(user, "was just changed.")
        return redirect("password_change_done")

    # no way to send a code: the classic form with the current password
    from . import platform
    if not (platform.email_enabled() and user.email):
        form = PasswordChangeForm(user, request.POST or None)
        if request.method == "POST" and form.is_valid():
            return finish(form)
        return render(request, "registration/password_change_form.html", {"step": "fallback", "form": form})

    pending = session.get("pw_code")
    if pending and now - pending["at"] > CODE_MINUTES * 60:
        session.pop("pw_code", None)
        pending = None
    verified = now - session.get("pw_ok_at", 0) <= CODE_MINUTES * 60
    action = request.POST.get("action") if request.method == "POST" else None
    ctx = {"email": _masked(user.email), "minutes": CODE_MINUTES}

    if action == "send":
        if not hit(f"pw-code:{user.pk}", 3, 15 * 60):
            messages.error(request, "Too many codes asked for. Wait 15 minutes, then try again.")
            return redirect("password_change")
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(6))
        session["pw_code"] = {"hash": _code_hash(user, code), "at": now, "tries": 0}
        session.pop("pw_ok_at", None)
        emails.send(user.email, f"Your TipPool code: {code}", eyebrow="Password change", heading="Your code",
                    greeting=f"Hi {user.get_full_name() or user.get_username()},",
                    paragraphs=["Type this code in TipPool to change your password."], code=code,
                    preheader=f"{code} is your code. It works for {CODE_MINUTES} minutes.",
                    notes=[f"It works for {CODE_MINUTES} minutes.",
                           "Didn't ask to change your password? Ignore this email: nothing changes without the code."])
        messages.success(request, f"We sent a 6-character code to {_masked(user.email)}.")
        if (settings.DEBUG and platform.email_settings() is None
                and settings.EMAIL_FALLBACK_BACKEND.endswith("console.EmailBackend")):
            # local development: emails only print in the server's terminal, so show the code here too
            messages.info(request, f"Testing on your computer: no email service is connected, so the email was "
                                   f"printed in the server's terminal. Your code is {code}.")
        return redirect("password_change")

    if action == "verify":
        if not pending:
            messages.error(request, "That code has expired. Send a new one.")
            return redirect("password_change")
        typed = "".join(ch for ch in request.POST.get("code", "").upper() if ch.isalnum())[:6]
        pending["tries"] += 1
        session["pw_code"] = pending
        import hmac
        if len(typed) == 6 and hmac.compare_digest(_code_hash(user, typed), pending["hash"]):
            session.pop("pw_code", None)
            session["pw_ok_at"] = now
            return redirect("password_change")
        if pending["tries"] >= CODE_TRIES:
            session.pop("pw_code", None)
            messages.error(request, "Too many wrong codes. Send a new one.")
        else:
            left = CODE_TRIES - pending["tries"]
            messages.error(request, f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.")
        return redirect("password_change")

    if verified:
        form = SetPasswordForm(user, request.POST if action == "set" else None)
        if action == "set" and form.is_valid():
            return finish(form)
        return render(request, "registration/password_change_form.html", {**ctx, "step": "new", "form": form})
    if action == "set":
        messages.error(request, "Confirm it's you with a code first.")
        return redirect("password_change")
    if action == "cancel":
        session.pop("pw_code", None)
        return redirect("password_change")
    return render(request, "registration/password_change_form.html", {**ctx, "step": "code" if pending else "start"})


def _password_changed_email(user, how):
    emails.send(user.email, "Your TipPool password was changed", eyebrow="Security", heading="Password changed",
                greeting=f"Hi {user.get_full_name() or user.get_username()},",
                paragraphs=[f"The password for your TipPool account ({user.email}) {how}"],
                details=[("When", timezone.localtime().strftime("%b %d, %I:%M %p"))],
                notes=["Was this you? Then there's nothing to do.",
                       "If it wasn't you, reset your password right away and tell the owner of your business."],
                button=("Reset my password", emails.absolute(reverse("password_reset"))))


from django.contrib.auth.forms import PasswordResetForm  # noqa: E402


class BrandedResetForm(PasswordResetForm):
    """The reset link email in TipPool's own look."""

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email,
                  html_email_template_name=None):
        path = reverse("password_reset_confirm", kwargs={"uidb64": context["uid"], "token": context["token"]})
        link = f"{context['protocol']}://{context['domain']}{path}"
        emails.send(to_email, "Reset your TipPool password", eyebrow="Password reset",
                    heading="Choose a new password",
                    paragraphs=[f"Someone asked to reset the password for your TipPool account ({to_email})."],
                    button=("Choose a new password", link), links=[("Or copy this link", link)],
                    notes=["The link works once, for a few days.",
                           "If it wasn't you, ignore this email: your password stays the same."])


class ResetConfirmView(__import__("django.contrib.auth.views", fromlist=["PasswordResetConfirmView"]).PasswordResetConfirmView):
    """Choosing a new password from the emailed link; the account's owner is told it changed."""

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        if user.email:
            _password_changed_email(user, "was just reset with the emailed link.")
        Change.log(user.get_full_name() or user.get_username(), "Reset their password with the emailed link")
        return response
