def app_context(request):
    from .models import Location
    location = getattr(request, "location", None)
    business = getattr(request, "business", None)
    try:
        locations = list(Location.objects.filter(business=business, archived=False)) if business else []
    except Exception:  # e.g. before the first migration
        locations = []
    open_messages = 0
    if locations and location:
        from django.db.models import Q

        from .models import Alert, Message
        open_messages = (Message.objects.filter(Q(location=location) | Q(kind="payout", staff__business=business),
                                                resolved_at__isnull=True).distinct().count()
                         + Alert.objects.filter(Q(location=location) | Q(location__isnull=True), business=business,
                                                resolved_at__isnull=True).count())
    owner = business or (location.business if location is not None else None)
    from django.conf import settings
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
        "email_enabled": settings.EMAIL_ENABLED,
        "cur": owner.symbol if owner else "$",
        "business": business,
        "open_messages": open_messages,
        "location": location,
        "locations": locations,
        "restaurant_name": location.display_name if location else "Your business",
        "restaurant_named": bool(location and location.name),
    }
