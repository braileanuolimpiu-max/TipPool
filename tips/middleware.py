from django.core.cache import cache
from django.utils import timezone

from . import autoclose
from .models import Location


def pick_location(request):
    """Public pages name the location in the link (?at=<slug>); managers pick one (kept in the session)."""
    slug = request.GET.get("at")
    if slug:
        location = Location.objects.filter(slug=slug).first()
        if location:
            return location
    location_id = request.session.get("location_id") if hasattr(request, "session") else None
    if location_id:
        location = Location.objects.filter(pk=location_id).first()
        if location:
            return location
    return Location.default()


class LocationMiddleware:
    """Set request.location, show times in its time zone, and run due auto close-outs
    (at most once a minute) while a manager is using the app."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            request.location = pick_location(request)
            timezone.activate(autoclose.location_tz(request.location))
        except Exception:  # e.g. before the first migration
            request.location = None
        if getattr(request, "user", None) and request.user.is_authenticated and cache.add("autoclose-check", 1, 60):
            try:
                autoclose.run()
            except Exception:
                pass
        response = self.get_response(request)
        timezone.deactivate()
        return response
