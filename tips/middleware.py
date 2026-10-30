from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import autoclose
from .models import Location


def manager_business(request):
    """The business of the signed-in manager, or None (guests, staff pages, logins without a business)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return None
    profile = getattr(user, "manager", None)
    return profile.business if profile else None


def allowed_locations(request):
    """The locations this manager runs: all of them for owners and unrestricted managers."""
    profile = getattr(getattr(request, "user", None), "manager", None)
    return profile.allowed_locations() if profile is not None else Location.objects.none()


def pick_location(request):
    """Managers work in one of the locations they run (the picked one is kept in the session).
    Public pages name the location in the link (?at=<slug>)."""
    business = request.business
    if business is not None:
        own = request.allowed_locations
        slug = request.GET.get("at")
        named = own.filter(slug=slug).first() if slug else None
        if named is not None:  # e.g. an alert email's link: stay at that location afterwards too
            request.session["location_id"] = named.pk
            return named
        location_id = request.session.get("location_id")
        return (own.filter(pk=location_id).first() if location_id else None) or own.first()
    return public_location(request)


def public_location(request):
    """For guest pages: the location named in the link, whoever is looking. None when the link doesn't
    name a real location: a tip must never land in some other business's PayPal."""
    slug = request.GET.get("at")
    return Location.objects.filter(slug=slug, archived=False).first() if slug else None


MAX_REQUEST_BYTES = 12 * 1024 * 1024  # the largest upload is an 8 MB schedule photo


class UpMiddleware:
    """/up/ answers at once, without the database: Render's health check and the keep-awake ping use it,
    so the free database can still sleep between real visits."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/up/":
            from django.http import HttpResponse
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)


class SizeLimitMiddleware:
    """Refuse a request body bigger than any form needs, before it's read or saved to disk."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import HttpResponse
        try:
            size = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        if size > MAX_REQUEST_BYTES:
            return HttpResponse("That file is too big (12 MB at most).", status=413,
                                content_type="text/plain; charset=utf-8")
        return self.get_response(request)


PAYPAL = "https://*.paypal.com https://*.paypalobjects.com"
TURNSTILE = "https://challenges.cloudflare.com"


def content_security_policy(nonce, inline_styles=False):
    """Only our own scripts and styles (each page's inline ones carry this request's nonce), PayPal's SDK and
    Cloudflare's bot check run. No 'unsafe-inline' anywhere: pages have no style attributes (static/css/inline.css
    and data-css instead). `inline_styles`: only for the page that previews an email (emails need them)."""
    styles = "'self' 'unsafe-inline'" if inline_styles else f"'self' 'nonce-{nonce}'"
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {PAYPAL} {TURNSTILE}",
        f"style-src {styles} https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        f"img-src 'self' data: {PAYPAL}",
        "media-src 'self' blob:",  # the front page's film: its clips play from blobs the page fetched itself
        f"connect-src 'self' {PAYPAL} {TURNSTILE}",
        f"frame-src {PAYPAL} https://*.venmo.com {TURNSTILE}",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ])


class ContentSecurityPolicyMiddleware:
    """A fresh nonce per request (templates put it on every <script>), and the policy on every page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import secrets
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        if "Content-Security-Policy" not in response and response.get("Content-Type", "").startswith("text/html"):
            if request.path.startswith(admin_prefix()):
                # Django's own admin has inline styles and scripts we can't change; only the operator (signed in,
                # two-step on) ever gets this far, everyone else is shown "not found" first
                response["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                       "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
            else:
                response["Content-Security-Policy"] = content_security_policy(request.csp_nonce)
        return response


def admin_prefix():
    return "/" + settings.ADMIN_PATH


class HiddenAdminMiddleware:
    """The Django admin doesn't exist for anyone but the site's operator (signed in, two-step on): to everyone
    else its address is a plain "not found". It isn't at /admin/ either, where scanners and guessers look."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(admin_prefix()) or request.path.rstrip("/") == admin_prefix().rstrip("/"):
            from . import platform
            user = getattr(request, "user", None)
            if not (platform.is_operator(user) and not platform.needs_two_step(user)):
                from django.http import Http404
                from .errors import not_found
                return not_found(request, Http404())
        return self.get_response(request)


class LocationMiddleware:
    """Set request.location, show times in its time zone, and run due auto close-outs
    (at most once a minute) while a manager is using the app."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            request.business = manager_business(request)
            request.allowed_locations = allowed_locations(request) if request.business else Location.objects.none()
            request.location = pick_location(request)
        except Exception:  # e.g. before the first migration
            request.business, request.location = None, None
            request.allowed_locations = Location.objects.none()
        if request.location is not None:
            timezone.activate(autoclose.location_tz(request.location))
        from . import ai
        ai.for_business(request.business)  # its AI use counts against its daily allowance
        try:  # the address the site is opened at (the host is already checked against ALLOWED_HOSTS)
            if settings.DEBUG or request.get_host().split(":")[0] not in ("localhost", "127.0.0.1"):
                cache.add("site-url", request.build_absolute_uri("/"), 24 * 3600)
        except Exception:
            pass
        if (settings.AUTOCLOSE_IN_REQUESTS and getattr(request, "user", None) and request.user.is_authenticated
                and cache.add("autoclose-check", 1, 60)):
            try:
                autoclose.run()
                from . import watch
                watch.run()
            except Exception:
                pass
        try:
            response = self.get_response(request)
        finally:
            ai.for_business(None)
        timezone.deactivate()
        return response
