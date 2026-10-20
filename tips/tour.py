"""The guided tour on demo businesses: the six things worth seeing, each ticked off once it's done."""
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

SEEN = "tour_seen"
# pages that count as "seen" for a step (url name, and an optional query parameter that must be there)
TRACKED = {"review": None, "messages_page": None, "tip_page": None, "reports": "q", "rules": None}


def record(request):
    """Remember which tour pages this manager has opened (cheap: only writes when something is new)."""
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else None
    if name not in TRACKED or (TRACKED[name] and not request.GET.get(TRACKED[name])):
        return
    seen = request.session.get(SEEN, [])
    if name not in seen:
        request.session[SEEN] = seen + [name]


def steps(request, business, location):
    from .autoclose import location_tz
    from .models import Message, Night
    if not (business and business.is_demo and location):
        return None
    seen = set(request.session.get(SEEN, []))
    today = timezone.now().astimezone(location_tz(location)).date()
    yesterday = Night.objects.filter(location=location, date__lt=today).order_by("-date").first()
    review_url = reverse("review", args=[yesterday.pk]) if yesterday else reverse("history")
    paid = bool(yesterday and yesterday.status in (Night.Status.PAYING, Night.Status.PAID))
    answered = not Message.objects.filter(location=location, kind="message", resolved_at__isnull=True).exists()
    items = [
        {"title": "Check yesterday's split", "url": review_url, "done": "review" in seen or paid,
         "text": "AI turned the house rules into exact shares and explains each one. The close-out check looks "
                 "for anything odd before money moves."},
        {"title": "Answer the team", "url": reverse("messages_page"), "done": answered,
         "text": "John says he stayed late. A flagged shift is never paid automatically."},
        {"title": "Pay everyone in one click", "url": review_url + "#pay-form", "done": paid,
         "text": "Approve the split: one PayPal Payouts batch pays the whole team (sandbox money)."},
        {"title": "Tip as a guest", "url": reverse("tip_page") + "?" + urlencode({"at": location.slug}),
         "done": "tip_page" in seen, "new_tab": True,
         "text": "The page your QR code opens, with PayPal and Venmo. Try another language at the bottom."},
        {"title": "Ask about your tips", "done": "reports" in seen,
         "url": reverse("reports") + "?" + urlencode({"q": "Who earned the most last week?"}),
         "text": "Plain questions, answered from your numbers. It can also act, after you confirm."},
        {"title": "Change the rules in plain words", "url": reverse("rules"), "done": "rules" in seen,
         "text": "Write a rule like you'd say it. AI reads it, and warns about anything that breaks tip law."},
    ]
    nxt = next((i for i, s in enumerate(items) if not s["done"]), None)
    if nxt is not None:
        items[nxt]["next"] = True
    return {"steps": items, "done": sum(s["done"] for s in items), "total": len(items)}
