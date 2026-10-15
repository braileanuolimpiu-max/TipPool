from django.conf import settings


def app_context(request):
    from .models import RestaurantSettings
    try:
        name = RestaurantSettings.load().name
    except Exception:  # e.g. before the first migration
        name = ""
    return {
        "restaurant_name": name or settings.RESTAURANT_NAME or "Your restaurant",
        "restaurant_named": bool(name or settings.RESTAURANT_NAME),
    }
