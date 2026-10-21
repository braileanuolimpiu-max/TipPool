"""Every email TipPool sends, in one look: the app's receipt style (white paper, black ink, yellow marker),
with a plain-text copy for clients that don't show HTML.

Codes sit in a big box that selects in one tap (and stay in the subject, so phones can offer to copy them);
links get a button and a box with the full address, to copy."""
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from . import platform


def site_url():
    return platform.site_url()


def absolute(path):
    """A full link to a page of the site, or "" when the site's address isn't known yet."""
    base = site_url()
    return urljoin(base, path) if base and path else ""


def _text(parts):
    """The plain-text copy of the same email."""
    lines = [parts["heading"], ""]
    if parts.get("greeting"):
        lines += [parts["greeting"], ""]
    for p in parts.get("paragraphs", ()):
        lines += [p, ""]
    if parts.get("quote"):
        lines += ["    " + parts["quote"].replace("\n", "\n    "), ""]
    if parts.get("code"):
        lines += [f"    {parts['code']}", ""]
    for label, value in parts.get("details", ()):
        lines.append(f"{label}: {value}")
    if parts.get("details"):
        lines.append("")
    if parts.get("button"):
        lines += [f"{parts['button'][0]}: {parts['button'][1]}", ""]
    for label, url in parts.get("links", ()):
        lines += [f"{label}:", url, ""]
    lines += list(parts.get("notes", ()))
    return "\n".join(lines).strip() + "\n\n-- \nTipPool\n"


def build(to, subject, *, heading, eyebrow="", greeting="", paragraphs=(), quote="", code="", code_note="",
          details=(), button=None, links=(), notes=(), preheader="", footer="", reply_to=None):
    """One email message (not sent yet). `button` and `links` are (label, url); a button without a known
    site address is left out (its link is still in `links` when it matters)."""
    if button and not button[1]:
        button = None
    parts = {"subject": subject, "heading": heading, "eyebrow": eyebrow, "greeting": greeting,
             "paragraphs": [p for p in paragraphs if p], "quote": quote, "code": code, "code_note": code_note,
             "details": [(k, v) for k, v in details if v not in ("", None)], "button": button,
             "links": [(k, v) for k, v in links if v], "notes": [n for n in notes if n],
             "preheader": preheader or (paragraphs[0] if paragraphs else ""), "footer": footer}
    base = site_url()
    html = render_to_string("emails/base.html", {**parts, "site_url": base,
                                                 "site_host": urlparse(base).netloc if base else ""})
    message = EmailMultiAlternatives(subject, _text(parts), settings.DEFAULT_FROM_EMAIL,
                                     [to] if isinstance(to, str) else list(to), reply_to=reply_to)
    message.attach_alternative(html, "text/html")
    return message


def payout_reminder(item, link):
    """Remind someone to accept a payout waiting for them (UNCLAIMED). `link`: their private tips page."""
    staff, night = item.share.staff, item.share.night
    symbol = night.location.business.symbol
    return send(staff.paypal_email, f"{symbol}{item.share.amount} in tips is waiting for you",
                eyebrow=night.location.display_name, heading=f"{symbol}{item.share.amount} is waiting for you",
                greeting=f"Hi {staff.name},",
                paragraphs=[f"Your tips for the {night} were sent with PayPal, but nobody has accepted them yet. "
                            "Open PayPal (or sign up with that email) to accept them."],
                details=[("Sent to", item.receiver), ("Shift", str(night)), ("Amount", f"{symbol}{item.share.amount}")],
                button=("Open PayPal", "https://www.paypal.com/"),
                links=[("Wrong address? Change it on your tips page", link)])


def send(to, subject, fail_silently=True, **parts):
    """Build and send one email. Returns 1 when sent."""
    try:
        return build(to, subject, **parts).send(fail_silently=fail_silently)
    except Exception:
        if not fail_silently:
            raise
        return 0
