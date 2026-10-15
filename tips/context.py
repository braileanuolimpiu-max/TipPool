from django.conf import settings


def app_context(request):
    return {"restaurant_name": settings.RESTAURANT_NAME}
