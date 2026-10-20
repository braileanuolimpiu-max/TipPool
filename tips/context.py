def app_context(request):
    from .models import Location
    location = getattr(request, "location", None)
    business = getattr(request, "business", None)
    try:
        locations = list(request.allowed_locations) if business else []
    except Exception:  # e.g. before the first migration
        locations = []
    seen = {}
    for loc in locations:
        seen[loc.display_name.lower()] = seen.get(loc.display_name.lower(), 0) + 1
    for loc in locations:  # two locations with one name: show their link names to tell them apart
        loc.switch_label = loc.display_name + (f" ({loc.slug})" if seen[loc.display_name.lower()] > 1 else "")
    open_messages = 0
    if locations and location:
        from django.db.models import Q

        from .models import Alert, Message
        open_messages = (Message.objects.filter(Q(location=location) | Q(kind="payout", staff__business=business,
                                                  staff__memberships__location__in=request.allowed_locations),
                                                resolved_at__isnull=True).distinct().count()
                         + Alert.objects.filter(Q(location=location) | Q(location__isnull=True), business=business,
                                                resolved_at__isnull=True).count())
    owner = business or (location.business if location is not None else None)
    from django.conf import settings

    from . import platform
    user = getattr(request, "user", None)
    profile = getattr(user, "manager", None) if user is not None and user.is_authenticated else None
    from . import tour
    demo_tour = None
    if profile is not None and profile.business.is_demo:
        tour.record(request)
        if business is not None:
            demo_tour = tour.steps(request, business, location)
    return {
        "demo_tour": demo_tour,
        "is_lead": bool(profile and profile.is_lead),
        "email_enabled": platform.email_enabled(),
        "email_to_terminal": settings.DEBUG and platform.email_settings() is None
                             and settings.EMAIL_FALLBACK_BACKEND.endswith("console.EmailBackend"),
        "is_operator": platform.is_operator(user),
        "cur": owner.symbol if owner else "$",
        "business": business,
        "open_messages": open_messages,
        "location": location,
        "locations": locations,
        "restaurant_name": location.display_name if location else "Your business",
        "restaurant_named": bool(location and location.name),
    }
