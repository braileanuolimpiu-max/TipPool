def app_context(request):
    from .models import Location
    location = getattr(request, "location", None)
    try:
        locations = list(Location.objects.all()) if getattr(request, "user", None) and request.user.is_authenticated else []
    except Exception:  # e.g. before the first migration
        locations = []
    open_messages = 0
    if locations and location:
        from .models import Message
        open_messages = Message.objects.filter(location=location, resolved_at__isnull=True).count()
    return {
        "open_messages": open_messages,
        "location": location,
        "locations": locations,
        "restaurant_name": location.display_name if location else "Your business",
        "restaurant_named": bool(location and location.name),
    }
