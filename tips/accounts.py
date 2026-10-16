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
    """Sign in with your email (or your username, for older logins)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        user = (User.objects.filter(email__iexact=username.strip()).order_by("pk").first()
                or User.objects.filter(username__iexact=username.strip()).first())
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
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
    invite = Invite.objects.create(business=request.business, email=email, invited_by=request.user.get_username())
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
def cancel_invite(request, pk):
    invite = get_object_or_404(Invite, pk=pk, business=request.business, accepted_at__isnull=True)
    invite.delete()
    return redirect(f"{reverse('settings')}#managers")


def accept_invite(request, token):
    invite = get_object_or_404(Invite, token=token)
    if not invite.usable:
        return render(request, "registration/invite.html", {"invite": invite, "dead": True})
    existing = User.objects.filter(email__iexact=invite.email).first()
    errors = []
    if request.method == "POST":
        if request.user.is_authenticated:
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
                Manager.objects.get_or_create(user=user, defaults={"business": invite.business})
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
