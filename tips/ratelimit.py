"""Simple rate limits (counted in the shared cache) against password guessing, sign-up spam,
card testing through the tip page, and email flooding."""
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse


def client_ip(request):
    """The visitor's address. Behind our hosting proxy it's the entry the proxy added (the last one):
    anything earlier in X-Forwarded-For was sent by the visitor and can be made up."""
    proxies = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    if proxies and len(forwarded) >= proxies:
        return forwarded[-proxies]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def hit(key, limit, seconds):
    """Count one attempt; True while under the limit."""
    if not getattr(settings, "RATELIMIT_ENABLED", True):
        return True
    cache_key = f"rl:{key}"
    if cache.add(cache_key, 1, seconds):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:  # expired between add and incr
        cache.add(cache_key, 1, seconds)
        return True
    return count <= limit


def over(key, limit):
    """True when this key already used up its limit (without counting a new attempt)."""
    if not getattr(settings, "RATELIMIT_ENABLED", True):
        return False
    return (cache.get(f"rl:{key}") or 0) >= limit


def limited(name, limit, seconds, methods=("POST",), json=False, key=None):
    """Decorator: at most `limit` requests every `seconds` for this view (only for `methods`), per IP,
    or per whatever `key(request, *args, **kwargs)` returns."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            who = key(request, *args, **kwargs) if key else client_ip(request)
            if request.method in methods and not hit(f"{name}:{who}", limit, seconds):
                message = "Too many tries. Please wait a few minutes and try again."
                if json:
                    return JsonResponse({"error": message}, status=429)
                return HttpResponse(message, status=429, content_type="text/plain; charset=utf-8")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
