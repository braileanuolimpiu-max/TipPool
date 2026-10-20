"""Branded error pages. Our own 404 messages are written for people; Django's internal ones are not shown."""
from django.shortcuts import render


def not_found(request, exception):
    message = exception.args[0] if exception and exception.args else ""
    if not isinstance(message, str) or "matches the given query" in message or not message.endswith("."):
        message = ""
    return render(request, "404.html", {"exception": message}, status=404)


def csrf_failure(request, reason=""):
    return render(request, "403_csrf.html", status=403)
