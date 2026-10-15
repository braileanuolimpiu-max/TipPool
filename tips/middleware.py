from django.core.cache import cache
from django.utils import timezone

from . import autoclose


class RestaurantMiddleware:
    """Show times in the restaurant's time zone, and run due auto close-outs
    (at most once a minute) while a manager is using the app."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            timezone.activate(autoclose.restaurant_tz())
        except Exception:
            pass
        if getattr(request, "user", None) and request.user.is_authenticated and cache.add("autoclose-check", 1, 60):
            try:
                autoclose.run()
            except Exception:
                pass
        response = self.get_response(request)
        timezone.deactivate()
        return response
