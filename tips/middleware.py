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


def pick_location(request):
    """Managers work in one of their own business's locations (picked one kept in the session).
    Public pages name the location in the link (?at=<slug>)."""
    business = request.business
    if business is not None:
        own = Location.objects.filter(business=business, archived=False)
        slug = request.GET.get("at")
        location_id = request.session.get("location_id")
        return ((own.filter(slug=slug).first() if slug else None)
                or (own.filter(pk=location_id).first() if location_id else None) or own.first())
    return public_location(request)


def public_location(request):
    """For guest pages: the location named in the link, whoever is looking. None when the link doesn't
    name a real location: a tip must never land in some other business's PayPal."""
    slug = request.GET.get("at")
    return Location.objects.filter(slug=slug, archived=False).first() if slug else None


class LocationMiddleware:
    """Set request.location, show times in its time zone, and run due auto close-outs
    (at most once a minute) while a manager is using the app."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            request.business = manager_business(request)
            request.location = pick_location(request)
        except Exception:  # e.g. before the first migration
            request.business, request.location = None, None
        if request.location is not None:
            timezone.activate(autoclose.location_tz(request.location))
        if (settings.AUTOCLOSE_IN_REQUESTS and getattr(request, "user", None) and request.user.is_authenticated
                and cache.add("autoclose-check", 1, 60)):
            try:
                autoclose.run()
            except Exception:
                pass
        response = self.get_response(request)
        timezone.deactivate()
        return response
