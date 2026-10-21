import io
import re
from collections import Counter
import json
import logging
from decimal import Decimal, InvalidOperation

import qrcode
import qrcode.image.svg
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai, autoclose, guest_i18n, hours as hours_from, insights, platform, roles as role_presets, services
from .models import (Alert, Change, CloseTime, Location, Membership, Night, PayoutBatch, PayoutItem, Role, Share,
                     Shift, StaffMember, Tip, TipRuleSet, WebhookEvent)
from .accounts import manager_required, managers_only, two_step_setup_context
from .check import blocking as check_blocking, live as check_live
from .middleware import public_location
from .ratelimit import hit, limited
from .paypal import PayPalError, checkout_config, get_client, webhook_id_for

log = logging.getLogger("tips.views")


# --- Manager dashboard -------------------------------------------------------
def _sync_tips(night):
    try:
        services.sync_pending_tips(night)
    except Exception:  # never break the page because PayPal is slow
        log.exception("Tip sync failed")


@manager_required
def dashboard(request):
    location = request.location
    night = Night.current(location)
    _sync_tips(night)
    tips = list(night.tips.filter(status=Tip.Status.COMPLETED).select_related("staff"))
    guest_tips = [t for t in tips if t.source == Tip.Source.PAYPAL]
    total = night.tip_total
    staff_count = Membership.objects.filter(location=location, active=True).count()
    rule_set = TipRuleSet.active(location)
    any_tip_ever = Tip.objects.filter(night__location=location, status=Tip.Status.COMPLETED).exists()
    return render(request, "tips/dashboard.html", {
        "night": night,
        "tips": tips[:40],
        "tip_count": len(guest_tips),
        "pace": insights.pace(location),
        "total": total,
        "average": (sum((t.amount for t in guest_tips), Decimal("0")) / len(guest_tips)).quantize(Decimal("0.01"))
                   if guest_tips else None,
        "unfinished": Night.objects.filter(location=location, status__in=[
            Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).first(),
        "next_close": autoclose.next_close(location),
        "roster_count": night.shifts.count(),
        "on_clock": list(night.clock_entries.filter(ended_at__isnull=True).select_related("staff")),
        "recent": _recent_shifts(location),
        "waiting": list(Night.objects.filter(location=location, status__in=[
            Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).order_by("date")[:5]),
        "alert_count": Alert.objects.filter(location=location, resolved_at__isnull=True).count(),
        "rule_set": rule_set,
        "staff_count": staff_count,
        "setup": [
            {"done": bool(location.name), "text": "Add your business name", "url": reverse("settings")},
            {"done": staff_count > 0, "text": "Add your team and their PayPal emails", "url": reverse("staff_list")},
            {"done": bool(rule_set), "text": "Describe how your house shares tips", "url": reverse("rules")},
            {"done": location.business.paypal_connected or location.business.is_demo,
             "text": "Connect your PayPal, so tips go to your account", "url": f"{reverse('settings')}#payments"},
            {"done": any_tip_ever, "text": "Print your QR code so guests can tip", "url": reverse("qr_page")},
        ],
        "ai_mode": ai.mode(),
        "can_load_sample": not any_tip_ever and not request.business.paypal_live and not request.business.is_demo
                           and not Tip.objects.filter(night__location__business=request.business).exists(),
        "setup_done": staff_count > 0 and bool(rule_set) and any_tip_ever and bool(location.name),
    })


def _recent_shifts(location, count=8):
    """The last closed shifts, oldest first, with bar heights for the little chart."""
    nights = list(Night.objects.filter(location=location).exclude(status=Night.Status.OPEN)
                  .order_by("-date", "-created_at")[:count])[::-1]
    totals = [n.tip_total for n in nights]
    top = max(totals, default=Decimal("0"))
    latest = len(nights) - 1
    return [{"night": n, "total": t, "height": int(t / top * 100) if top else 0,
             "label": (t == top and top > 0) or i == latest} for i, (n, t) in enumerate(zip(nights, totals))]


@manager_required
@require_POST
def clock_out_for(request, pk):
    """A manager clocks out someone who forgot."""
    from .models import ClockEntry
    entry = get_object_or_404(ClockEntry, pk=pk, night__location=request.location, ended_at__isnull=True)
    hours_from.clock_out(entry.staff)
    Change.log(request, f"Clocked out {entry.staff.name} (forgot to clock out)")
    messages.success(request, f"{entry.staff.name} is clocked out.")
    return redirect("dashboard")


@manager_required
def history(request):
    from django.db.models import Count, OuterRef, Subquery, Sum
    # the tips total in its own subquery: joining tips and shares in one query would count each tip once per share
    tips_total = (Tip.objects.filter(night=OuterRef("pk"), status=Tip.Status.COMPLETED).values("night")
                  .annotate(t=Sum("amount")).values("t"))
    nights = (Night.objects.filter(location=request.location).exclude(status=Night.Status.OPEN)
              .annotate(total=Subquery(tips_total),
                        paid_people=Count("shares", filter=Q(shares__amount__gt=0), distinct=True))
              .order_by("-date", "-pk").prefetch_related("payout_batches")[:60])
    rows = []
    for n in nights:
        batches = [b for b in n.payout_batches.all() if not b.is_retry]
        rows.append({"night": n, "total": (n.total or Decimal("0")).quantize(Decimal("0.01")),
                     "people": n.paid_people, "batch": batches[0] if batches else None})
    return render(request, "tips/history.html", {"rows": rows})


def _id(value):
    """A database id from a form or link, or None (never a crash on "abc")."""
    value = str(value or "")
    return int(value) if value.isascii() and value.isdigit() and len(value) < 12 else None


# --- Customer tip page (public) -----------------------------------------------
def tip_page(request):
    location = public_location(request)
    if location is None:
        raise Http404("This tip code doesn't point to a business. Ask the staff for a fresh one.")
    people = []
    if location.tip_mode != Location.TipMode.POOL:
        # who's working right now first, then the rest of the team
        night = Night.current(location)
        working = set(night.clock_entries.filter(ended_at__isnull=True).values_list("staff_id", flat=True))
        working |= set(night.shifts.values_list("staff_id", flat=True))
        no_tips = set(Role.objects.filter(location=location, no_tips=True).values_list("key", flat=True))
        members = [m for m in Membership.objects.filter(location=location, active=True).select_related("staff")
                   if m.default_role not in no_tips]
        people = sorted(members, key=lambda m: (m.staff_id not in working, m.staff.name))
    has_team = Membership.objects.filter(location=location, active=True).exists()
    to = request.GET.get("to", "")
    style, values = location.tip_buttons
    lang = guest_i18n.pick(request)
    for m in people:
        m.guest_role = guest_i18n.role(m.role_name, lang)
    with translation.override(lang):  # month names in the guest's language too
        response = render(request, "tips/tip_page.html", {
            **_guest_language(request, lang),
            "location": location, "restaurant_name": location.display_name, "restaurant_named": bool(location.name),
            "sandbox_buyer": platform.demo_buyer() if location.business.is_demo else None,
            "at": location.slug,
            "tip_style": style,
            # personal tips only, and nobody to pick: tipping can't work right now
            "nobody": not has_team or (location.tip_mode == Location.TipMode.PERSONAL and not people),
            "presets": values,
            "default_pick": values[1] if len(values) > 1 else values[0],
            "tip_message": location.tip_message,
            "tip_mode": location.tip_mode,
            "people": people,
            "to": int(to) if to.isascii() and to.isdigit() and any(m.pk == int(to) for m in people) else None,
            "cancelled": "cancelled" in request.GET,
            "table": request.GET.get("table", "")[:20],
            "paypal_client_id": checkout_config(location.business)[0],
            "paypal_sdk_url": checkout_config(location.business)[1],
            "currency": location.business.currency,
            "cur": location.business.symbol,
        })
    return guest_i18n.remember(response, request, lang)


LEGAL_UPDATED = "October 21, 2026"


def legal_page(request, page):
    return render(request, f"legal/{page}.html", {"page": page, "updated": LEGAL_UPDATED,
                                                  "contact_email": platform.contact_email()})


def _guest_language(request, lang):
    """The guest page's words, plus links to the same page in each other language."""
    links = []
    for code, name in guest_i18n.LANGS.items():
        query = request.GET.copy()
        query["lang"] = code
        query.pop("cancelled", None)
        links.append({"code": code, "name": name, "url": "?" + query.urlencode(), "current": code == lang})
    t = guest_i18n.strings(lang)
    return {"lang": lang, "t": t, "lang_links": links, "paypal_locale": guest_i18n.PAYPAL_LOCALES[lang], "js_words": {k: t[k] for k in guest_i18n.JS_KEYS}}


def tip_thanks(request):
    # "order" comes from our page; "token" is what PayPal adds when it redirects back here
    order_id = request.GET.get("order") or request.GET.get("token") or ""
    tip = Tip.objects.filter(paypal_order_id=order_id).first()
    not_paid = False
    if tip and tip.status != Tip.Status.COMPLETED:
        try:
            tip = services.capture_tip(order_id)
        except PayPalError as exc:
            issues = [d.get("issue") for d in (exc.details or {}).get("details") or [] if isinstance(d, dict)]
            not_paid = "ORDER_NOT_APPROVED" in issues  # the guest never finished paying in PayPal
            log.warning("Capture on return page failed for %s", order_id)
    if tip:  # show the business the tip went to, even without ?at= in the return link
        request.location = tip.night.location
    elif request.GET.get("at"):
        request.location = public_location(request)
    if request.location is None:
        raise Http404("We couldn't find that tip.")
    timezone.activate(autoclose.location_tz(request.location))
    lang = guest_i18n.pick(request)
    t = guest_i18n.strings(lang)
    cur = request.location.business.symbol
    with translation.override(lang):
        return render(request, "tips/tip_thanks.html", {
            **_guest_language(request, lang),
            "tip": tip, "table": tip.table if tip else "", "location": request.location, "cur": cur, "not_paid": not_paid,
            "done_text": t["done_personal"].format(name=tip.staff.name) if tip and tip.staff_id else t["done_note"],
            "confirming_text": t["confirming"].format(amount=f"{cur}{tip.amount}") if tip else "",
            "restaurant_name": request.location.display_name, "restaurant_named": bool(request.location.name),
        })


@require_POST
@limited("orders", 20, 600, json=True)
def api_create_order(request):
    try:
        data = json.loads(request.body or "{}")
        if not isinstance(data, dict):
            raise ValueError("bad body")
        amount = Decimal(str(data.get("amount", "")))
        table = str(data.get("table", ""))[:20]
        location = Location.objects.filter(slug=str(data.get("at", ""))[:40], archived=False).first()
        if location is None:
            return JsonResponse({"error": "This tip page doesn't belong to a business."}, status=404)
        if not hit(f"orders-loc:{location.pk}", 300, 3600):
            return JsonResponse({"error": "Too many tips at once. Please try again in a minute."}, status=429)
        for_staff = None
        to = str(data.get("to") or "")
        if to:
            member = (Membership.objects.filter(pk=int(to), location=location, active=True).select_related("staff")
                      .first() if to.isascii() and to.isdigit() else None)
            no_tips = member is not None and Role.objects.filter(location=location, key=member.default_role,
                                                                no_tips=True).exists()
            if member is None or no_tips:
                return JsonResponse({"error": "That person isn't taking tips here right now."}, status=400)
            for_staff = member.staff
        order_id = services.create_tip_order(
            location, amount, table, for_staff=for_staff,
            return_url=request.build_absolute_uri(reverse("tip_thanks")),
            cancel_url=request.build_absolute_uri(reverse("tip_page")) + f"?at={location.slug}&cancelled=1",
        )
    except (InvalidOperation, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Please choose a valid tip amount."}, status=400)
    except services.TipPoolError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except PayPalError as exc:
        return JsonResponse({"error": "PayPal could not create the payment.", "debug_id": exc.debug_id}, status=502)
    return JsonResponse({"orderId": order_id})


@require_POST
@limited("capture", 40, 600, json=True)
def api_capture_order(request, order_id):
    try:
        tip = services.capture_tip(order_id)
    except Tip.DoesNotExist:
        return JsonResponse({"error": "Unknown order."}, status=404)
    except PayPalError as exc:
        issue = (exc.details.get("details") or [{}])[0].get("issue")
        if issue == "INSTRUMENT_DECLINED":
            return JsonResponse({"error": "declined", "message": "Your payment method was declined. Please try another."},
                                status=422)
        return JsonResponse({"error": "PayPal could not complete the payment.", "debug_id": exc.debug_id}, status=502)
    return JsonResponse({"status": tip.status, "amount": str(tip.amount),
                         "redirect": reverse("tip_thanks") + f"?order={order_id}"})


# --- QR codes ----------------------------------------------------------------------
@manager_required
def qr_page(request):
    restaurant = request.location
    base = request.build_absolute_uri(reverse("tip_page")) + f"?at={restaurant.slug}"
    return render(request, "tips/qr_page.html", {
        "restaurant": restaurant,
        "house_url": base,
        "people": [(m.staff.name, m.role_name, f"{base}&to={m.pk}", m.pk)
                   for m in Membership.objects.filter(location=restaurant, active=True).select_related("staff")
                   ] if restaurant.tip_mode != Location.TipMode.POOL else [],
    })


def qr_show(request):
    """Full-screen QR for a server's phone or a counter tablet. No login, it only links to the tip page."""
    location = public_location(request) if request.GET.get("at") else request.location
    if location is None:
        raise Http404("Unknown business.")
    table = request.GET.get("table", "")[:20]
    to = request.GET.get("to", "")
    member = (Membership.objects.filter(pk=int(to), location=location).select_related("staff").first()
              if to.isascii() and to.isdigit() else None)
    url = request.build_absolute_uri(reverse("tip_page")) + f"?at={location.slug}" + (
        f"&table={table}" if table else "") + (f"&to={member.pk}" if member else "")
    label = f"Tip {member.staff.name}" if member else (
        (f"Table {table}" if table.isdigit() else table) if table else "Leave a tip")
    return render(request, "tips/qr_show.html", {"url": url, "label": label, "table": table})


def qr_svg(request):
    url = request.GET.get("url", "")
    if not url.startswith(request.build_absolute_uri("/")) or len(url) > 300:
        return HttpResponseBadRequest("Only links to this site.")
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type="image/svg+xml")
    if request.GET.get("download"):
        response["Content-Disposition"] = 'attachment; filename="tippool-qr.svg"'
    return response


def qr_png(request):
    """A big PNG of a tip code, for menus, posters or design tools."""
    url = request.GET.get("url", "")
    if not url.startswith(request.build_absolute_uri("/")) or len(url) > 300:
        return HttpResponseBadRequest("Only links to this site.")
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=24, border=4)
    code.add_data(url)
    code.make(fit=True)
    buffer = io.BytesIO()
    code.make_image(fill_color="#141414", back_color="white").save(buffer, format="PNG")
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    response["Content-Disposition"] = 'attachment; filename="tippool-qr.png"'
    return response


QR_LAYOUTS = {
    # key: (label, per page, what it is)
    "tent": ("Table tents", 2, "Fold in half: it stands up and reads from both sides"),
    "card": ("Cards", 4, "For the check presenter, the counter or a frame"),
    "six": ("Small cards", 6, "Business-card size, for check presenters, the till or handing out"),
    "sticker": ("Stickers", 12, "For tables, menus, the till or name badges"),
    "poster": ("Poster", 1, "One big code for the door, the wall or the counter"),
}


@manager_required
def qr_print(request):
    """Print studio: pick a layout, paper, copies and text, see the real pages, then print or save as PDF."""
    location = request.location
    layout = request.GET.get("layout") if request.GET.get("layout") in QR_LAYOUTS else "tent"
    paper = "letter" if request.GET.get("paper") == "letter" else "a4"
    which = request.GET.get("codes", "shared")
    try:
        copies = max(1, min(60, int(request.GET.get("copies", QR_LAYOUTS[layout][1]))))
    except ValueError:
        copies = QR_LAYOUTS[layout][1]
    headline = (request.GET.get("headline") or "Leave a tip").strip()[:40]
    footer = (request.GET.get("footer") or "").strip()[:80]

    base = request.build_absolute_uri(reverse("tip_page")) + f"?at={location.slug}"
    shared = {"title": headline, "url": base, "who": ""}
    people = [{"title": f"Tip {m.staff.name}", "url": f"{base}&to={m.pk}", "who": m.role_name}
              for m in Membership.objects.filter(location=location, active=True).select_related("staff")
              ] if location.tip_mode != Location.TipMode.POOL else []
    if which == "people" and people:
        items = [p for p in people for _ in range(copies)]
    else:
        which = "shared"
        items = [shared] * copies
    per_page = QR_LAYOUTS[layout][1]
    pages = [items[i:i + per_page] for i in range(0, len(items), per_page)]
    note = footer or ("Goes to this person in full" if which == "people" else
                      "Shared with the whole team" if location.tip_mode == Location.TipMode.POOL else
                      "Goes to the person you choose")
    return render(request, "tips/qr_print.html", {
        "layouts": QR_LAYOUTS, "layout": layout, "paper": paper, "copies": copies, "which": which,
        "headline": headline, "footer": footer, "note": note, "pages": pages, "has_people": bool(people),
        "house_url": base, "sheet_count": len(pages),
    })


# --- Staff ---------------------------------------------------------------------
@manager_required
@managers_only
def staff_list(request):
    location = request.location
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())[:80]
        email = request.POST.get("paypal_email", "").strip().lower()[:254]
        role = request.POST.get("default_role", "")
        if not (name and email and Role.objects.filter(location=location, key=role).exists()):
            messages.error(request, "Name, PayPal email and role are required.")
            return redirect("staff_list")
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, f"\"{email}\" isn't a valid email. Use the email of their PayPal account.")
            return redirect("staff_list")
        clash = _name_taken(request.business, name, email)
        if clash:
            messages.error(request, clash)
            return redirect("staff_list")
        person = _add_person(request, location, name, email, role)
        others = person.memberships.exclude(location=location).count()
        messages.success(request, f"{person.name} added." + (
            f" They also work at {others} other location{'s' if others > 1 else ''}, so their tips page "
            "shows everything." if others else ""))
        return redirect("staff_list")
    roles = list(Role.objects.filter(location=location))
    memberships = list(Membership.objects.filter(location=location).select_related("staff"))
    counts = Counter(m.default_role for m in memberships if m.active)
    return render(request, "tips/staff_list.html", {
        "staff": memberships, "roles": roles,
        "role_rows": [{"role": r, "count": counts[r.key]} for r in roles],
        "presets": [(k, p["label"]) for k, p in role_presets.PRESETS.items()],
        "business_type": location.business_type,
        "base_url": request.build_absolute_uri("/").rstrip("/"),
    })


def _name_taken(business, name, email, exclude=None):
    """A message when another person in the business already has this name (same email = same person)."""
    others = StaffMember.objects.filter(business=business, name__iexact=name).exclude(paypal_email__iexact=email)
    if exclude is not None:
        others = others.exclude(pk=exclude.pk)
    if others.exists():
        return SAME_NAME.format(name=name, first=name.split()[0])
    return ""


SAME_NAME = ("Someone on your team is already called {name}. Add a last initial or a nickname (like {first} L.) "
             "so guests, reports and the team can tell them apart.")


def _add_person(request, location, name, email, role):
    """Put someone on this location's team. The same person at another location (same PayPal email)
    keeps one tips page for everything."""
    person = StaffMember.objects.filter(business=request.business, paypal_email__iexact=email).first()
    if person is None:
        person = StaffMember.objects.create(business=request.business, name=name, paypal_email=email)
    membership, made = Membership.objects.get_or_create(staff=person, location=location,
                                                        defaults={"default_role": role})
    if not made:
        membership.active, membership.default_role = True, role
        membership.save()
    Change.log(request, f"Added {person.name} ({Role.label(role, location.pk)}) to the team")
    return person


BULK_MAX = 200
LINK_EMAILS_PER_DAY = 300  # protects the site's email account (and everyone else's codes and invites)


@manager_required
@require_POST
@managers_only
def staff_bulk(request):
    """Add many people at once: one per line "Name, PayPal email, role" (pasted or a CSV file)."""
    import csv
    location = request.location
    roles = list(Role.objects.filter(location=location))
    fallback = request.POST.get("default_role", "")
    if not any(r.key == fallback for r in roles):
        fallback = None
    text = request.POST.get("people", "")
    upload = request.FILES.get("file")
    if upload:
        if upload.size > 200 * 1024:
            messages.error(request, "That file is too big. Up to 200 people at a time.")
            return redirect("staff_list")
        raw = upload.read()
        if raw[:2] == b"PK" or raw[:4] == b"\xd0\xcf\x11\xe0":  # an Excel file, not a CSV
            messages.error(request, "That's an Excel file. In Excel choose File → Save As → CSV, then upload that, "
                                    "or copy the columns and paste them into the box.")
            return redirect("staff_list")
        text = raw.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        messages.error(request, "Paste one person per line, or choose a CSV file.")
        return redirect("staff_list")
    if len(lines) > BULK_MAX:
        messages.error(request, f"Up to {BULK_MAX} people at a time, please.")
        return redirect("staff_list")
    send_links = bool(request.POST.get("send_links")) and not request.business.is_demo
    added, skipped, mailed, outgoing, capped = [], [], 0, [], False
    seen, updated = {}, []
    for n, line in enumerate(lines, 1):
        sep = "\t" if "\t" in line else (";" if line.count(";") > line.count(",") else ",")
        cells = [c.strip() for c in next(csv.reader([line], delimiter=sep))] + ["", "", ""]
        name, email, role_text = " ".join(cells[0].split())[:80], cells[1].lower()[:254], cells[2]
        if n == 1 and name.lower() in ("name", "names", "full name") and "@" not in email:
            continue  # a header row
        if not name:
            skipped.append(f"line {n}: no name")
            continue
        try:
            validate_email(email)
        except ValidationError:
            skipped.append(f"line {n} ({name}): \"{email or 'no email'}\" isn't an email")
            continue
        found = ai._roles_in(role_text, roles) if role_text else []
        role = found[0].key if len(found) == 1 else (fallback if not role_text else None)
        if role is None:
            skipped.append(f"line {n} ({name}): " + (f"no role called \"{role_text}\"" if role_text else "no role"))
            continue
        if email in seen:
            skipped.append(f"line {n} ({name}): same email as line {seen[email]}")
            continue
        seen[email] = n
        existing = StaffMember.objects.filter(business=request.business, paypal_email__iexact=email).first()
        if existing is not None and Membership.objects.filter(staff=existing, location=location, active=True).exists():
            Membership.objects.filter(staff=existing, location=location).update(default_role=role)
            updated.append(existing.name)
            continue
        if _name_taken(request.business, name, email):
            skipped.append(f"line {n} ({name}): someone on the team already has that name")
            continue
        person = _add_person(request, location, name, email, role)
        added.append(person)
        if send_links and platform.email_enabled():
            if not hit(f"link-mail:{request.business.pk}", LINK_EMAILS_PER_DAY, 24 * 3600):
                capped = True
                continue
            from . import emails
            link = request.build_absolute_uri(reverse("my_tips", args=[person.access_token]))
            outgoing.append(emails.build(
                person.paypal_email, f"Your tips at {location.display_name}", eyebrow=location.display_name,
                heading="Your tips, in one link", greeting=f"Hi {person.name},",
                paragraphs=[f"{location.display_name} pays your share of the tips with TipPool. This is your private "
                            "link: your tips, how each share was worked out, and clocking in and out."],
                button=("Open my tips", link), links=[("Your private link", link)],
                notes=["Keep it to yourself, and add it to your phone's home screen."]))
    if outgoing:  # one connection for all of them
        from django.core.mail import get_connection
        try:
            mailed = get_connection(fail_silently=True).send_messages(outgoing) or 0
        except Exception:
            mailed = 0
    if capped:
        messages.warning(request, f"Up to {LINK_EMAILS_PER_DAY} private links can be emailed per day. The rest were "
                                  "added; send their links from the Team page later.")
    if updated:
        messages.info(request, "Already on the team (usual role updated): " + ", ".join(updated[:12]) +
                      ("…" if len(updated) > 12 else "") + ".")
    if added:
        note = f"Added {len(added)} {'person' if len(added) == 1 else 'people'} to the team."
        if send_links and platform.email_enabled():
            note += f" Emailed {mailed} private link{'s' if mailed != 1 else ''}."
        messages.success(request, note)
    if skipped:
        messages.warning(request, "Skipped " + "; ".join(skipped[:12]) + ("; and more." if len(skipped) > 12 else "."))
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def staff_role(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    role = request.POST.get("default_role", "")
    if Role.objects.filter(location=request.location, key=role).exists():
        member.default_role = role
        member.save(update_fields=["default_role"])
        Change.log(request, f"Changed {member.staff.name}'s usual role to {member.role_name}")
        messages.success(request, f"{member.staff.name} is now {member.role_name.lower()}.")
    else:
        messages.error(request, "Pick one of this location's roles.")
    return redirect("staff_list")


def _works_elsewhere(request, person):
    """A manager limited to some locations can't change a person who also works at one they don't run
    (their email, link and data are shared by every location). Says so and returns True."""
    if request.user.manager.all_access:
        return False
    if person.memberships.filter(active=True).exclude(location__in=request.allowed_locations).exists():
        messages.error(request, f"{person.name} also works at a location you don't run, so only the owner (or a "
                                "manager of every location) can change this.")
        return True
    return False


@manager_required
@require_POST
@managers_only
def staff_new_link(request, pk):
    """The old private link stops working (e.g. it was sent to the wrong person)."""
    from .models import new_access_token
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    if _works_elsewhere(request, member.staff):
        return redirect("staff_list")
    member.staff.access_token = new_access_token()
    member.staff.save(update_fields=["access_token"])
    Change.log(request, f"Made a new tips link for {member.staff.name} (the old one no longer works)")
    messages.success(request, f"New link made for {member.staff.name}. Copy it and send it to them: "
                              "the old one no longer works.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def staff_edit(request, pk):
    """Fix someone's name or PayPal email. A new email has to be confirmed by them again."""
    member = get_object_or_404(Membership.objects.select_related("staff"), pk=pk, location=request.location)
    person = member.staff
    if _works_elsewhere(request, person):
        return redirect("staff_list")
    name = " ".join(request.POST.get("name", "").split())[:80]
    email = request.POST.get("paypal_email", "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid PayPal email.")
        return redirect("staff_list")
    if StaffMember.objects.filter(business=request.business, paypal_email__iexact=email).exclude(pk=person.pk).exists():
        messages.error(request, "Someone else on your team already uses that email.")
        return redirect("staff_list")
    changes = []
    clash = _name_taken(request.business, name, "", exclude=person) if name and name != person.name else ""
    if clash:
        messages.error(request, clash)
        return redirect("staff_list")
    if name and name != person.name:
        changes.append(f"name {person.name} → {name}")
        person.name = name
    if email != person.paypal_email.lower():
        changes.append(f"PayPal email {person.paypal_email} → {email}")
        person.paypal_email = email
        person.payout_method = StaffMember.PayoutMethod.PAYPAL
        person.payout_confirmed_at = None  # they confirm the new address themselves
    if not name:
        messages.warning(request, "The name can't be empty, so it wasn't changed.")
    if changes:
        person.save()
        Change.log(request, f"Edited {person.name}: " + "; ".join(changes))
        messages.success(request, f"Saved. {person.name} confirms the new email on their tips page before the "
                                  "next payout." if any("email" in c for c in changes) else "Saved.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def staff_erase(request, pk):
    """Erase a person's personal data on request. The amounts stay for the business's records."""
    from .models import Message, new_access_token
    member = get_object_or_404(Membership.objects.select_related("staff"), pk=pk, location=request.location)
    person = member.staff
    if _works_elsewhere(request, person):
        return redirect("staff_list")
    if Share.objects.filter(staff=person, night__status__in=[Night.Status.REVIEW, Night.Status.PAYING]).exists():
        messages.error(request, f"{person.name} still has a payout waiting. Pay or recalculate it first.")
        return redirect("staff_list")
    old_name = person.name
    person.name = f"Former team member {person.pk}"
    person.paypal_email = f"erased-{person.pk}@invalid.example"
    person.venmo_phone = ""
    person.payout_confirmed_at = None
    person.access_token = new_access_token()
    person.erased_at = timezone.now()
    person.save()
    person.memberships.update(active=False, schedule={})
    Message.objects.filter(staff=person).update(text="[erased]", payload={})
    Change.log(request, f"Erased the personal data of {old_name} (kept the amounts for the records)")
    messages.success(request, f"{old_name}'s name, email and link are erased. Their amounts stay in your reports.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def staff_schedule(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    week, dropped = {}, []
    for day in range(7):
        try:
            value = Decimal(request.POST.get(f"h_{day}", "") or "0")
        except InvalidOperation:
            value = Decimal("-1")
        if not value.is_finite():
            value = Decimal("-1")
        if 0 < value <= 24:
            week[str(day)] = float(value.quantize(Decimal("0.01")))
        elif value != 0:
            dropped.append(CloseTime.WEEKDAYS[day])
    member.schedule = week
    member.save(update_fields=["schedule"])
    Change.log(request, f"Set {member.staff.name}'s usual week: {member.schedule_summary or 'no fixed hours'}")
    messages.success(request, f"{member.staff.name}'s usual week saved: {member.schedule_summary or 'no fixed hours'}.")
    if dropped:
        messages.warning(request, f"Left out {', '.join(dropped)}: hours must be a number from 0 to 24.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def staff_toggle(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    member.active = not member.active
    member.save()
    Change.log(request, f"{'Brought back' if member.active else 'Removed'} {member.staff.name}")
    messages.success(request, f"{member.staff.name} is back on the team." if member.active else
                     f"{member.staff.name} is off the team. Their past shifts and pay stay in the records.")
    return redirect("staff_list")


# --- Roles: each location has its own ----------------------------------------------
@manager_required
@require_POST
@managers_only
def role_add(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Type a role name, e.g. Stylist or Housekeeper.")
        return redirect("staff_list")
    role, made = role_presets.add_role(request.location, name, request.POST.get("aliases", "").strip(),
                                       no_tips=bool(request.POST.get("no_tips")))
    if made:
        Change.log(request, f"Added the role {role.name}" + (" (never gets tips)" if role.no_tips else ""))
        messages.success(request, f"Role added: {role.name}. Mention it in your tip rules so it gets a share.")
    else:
        messages.error(request, f"You already have the role {role.name}.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def role_remove(request, pk):
    role = get_object_or_404(Role, pk=pk, location=request.location)
    if role.key in role_presets.roles_in_use(request.location):
        messages.error(request, f"Someone on your team is a {role.name.lower()}. Change their role first.")
    else:
        role.delete()
        Change.log(request, f"Removed the role {role.name}")
        messages.success(request, f"Role removed: {role.name}. Past shifts keep it.")
    return redirect("staff_list")


@manager_required
@require_POST
@managers_only
def role_preset(request):
    business_type = request.POST.get("business_type", "")
    if business_type not in role_presets.PRESETS:
        return redirect("staff_list")
    kept = role_presets.apply_preset(request.location, business_type)
    label = role_presets.PRESETS[business_type]["label"]
    Change.log(request, f"Switched roles to the {label.lower()} preset")
    msg = f"Roles set up for a {label.lower()}."
    if kept:
        msg += f" Kept {', '.join(kept)} because someone on your team still has it."
    messages.success(request, msg + " Check your tip rules: they should use these roles.")
    return redirect("staff_list")


# --- Rules -------------------------------------------------------------------
@manager_required
@managers_only
def rules_page(request):
    preview = None
    text = ""
    if request.method == "POST" and "save" in request.POST:
        preview = request.session.pop("rules_preview", None)
        if preview and preview.get("location") != request.location.pk:
            preview = None
        if not preview:
            messages.error(request, "Nothing to save, please describe the rules again.")
            return redirect("rules")
        TipRuleSet.objects.filter(location=request.location, is_active=True).update(is_active=False)
        TipRuleSet.objects.create(location=request.location, text=preview["text"], rules=preview["rules"], summary=preview["summary"],
                                  warnings=preview["warnings"])
        Change.log(request, f"Changed the tip rules: {preview['summary']}")
        messages.success(request, "Tip rules saved.")
        return redirect("rules")
    if request.method == "POST":
        text = request.POST.get("text", "")
        try:
            parsed = ai.parse_rules(text, request.location)
            preview = {"text": text, "location": request.location.pk, **parsed}
            request.session["rules_preview"] = preview
        except ai.AIError as exc:
            messages.error(request, str(exc))
    active = TipRuleSet.active(request.location)
    return render(request, "tips/rules.html", {
        "active": active, "preview": preview, "text": text or (active.text if active else ""),
        "ai_mode": ai.mode(),
        "examples": role_presets.examples_for(request.location.business_type),
        "roles": Role.objects.filter(location=request.location),
    })


# --- Close the night: who worked -> split -> review -> pay ---------------------
@manager_required
def close_night(request, pk=None):
    night = get_object_or_404(Night, pk=pk, location=request.location) if pk else Night.current(request.location)
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        return redirect("payout_status", pk=night.pk)
    if night.status == Night.Status.OPEN:
        _sync_tips(night)
    location = night.location
    existing = {s.staff_id: s for s in night.shifts.select_related("staff")}
    staff = [m.staff for m in Membership.objects.filter(location=location, active=True).select_related("staff")]
    # someone removed from the team after working this shift stays on it
    staff += sorted((s.staff for s in existing.values() if s.staff not in staff), key=lambda p: p.name)
    usual_role = {m.staff_id: m.default_role for m in Membership.objects.filter(location=location)}
    prefill = {}
    ai_notes = []

    if request.method == "POST" and request.FILES.get("photo"):
        photo = request.FILES["photo"]
        if photo.size > 8 * 1024 * 1024 or photo.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            messages.error(request, "Please upload a JPG, PNG, WEBP or GIF photo under 8 MB.")
        else:
            try:
                result = ai.read_schedule(photo.read(), photo.content_type, [m.name for m in staff], location)
                by_name = {m.name.lower(): m for m in staff}
                for row in result["shifts"]:
                    member = by_name.get(row["name"].lower())
                    if member:
                        prefill[member.pk] = {"role": row["role"], "hours": row["hours"]}
                    else:
                        ai_notes.append(f"'{row['name']}' is on the photo but not in your staff list.")
                ai_notes += result["notes"]
                messages.success(request, f"AI read {len(prefill)} shifts from the photo. Check them below.")
            except ai.AIError as exc:
                messages.error(request, str(exc))

    elif request.method == "POST":
        rule_set = TipRuleSet.active(location)
        saving_only = "save_roster" in request.POST
        if not rule_set and not saving_only and night.pool_total > 0:
            messages.error(request, "Set your tip rules first.")
            return redirect("rules")
        role_keys = set(Role.objects.filter(location=location).values_list("key", flat=True))
        role_keys |= {s.role for s in existing.values()}  # a role deleted since keeps working for this shift
        roster, problems = [], []
        for member in staff:
            if not request.POST.get(f"worked_{member.pk}"):
                continue
            try:
                hours = Decimal(request.POST.get(f"hours_{member.pk}", "0") or "0")
            except InvalidOperation:
                hours = Decimal("0")
            role = request.POST.get(f"role_{member.pk}", usual_role.get(member.pk))
            if not (hours.is_finite() and 0 < hours <= 24):
                problems.append(f"{member.name} (hours must be more than 0 and at most 24)")
            elif role not in role_keys:
                problems.append(f"{member.name} (pick a role)")
            else:
                roster.append((member, role, hours.quantize(Decimal("0.01"))))
        if problems:
            messages.error(request, "Nothing saved yet. Check: " + "; ".join(problems) + ".")
            return redirect("close_night_pk", pk=night.pk)
        with transaction.atomic():
            locked = Night.objects.select_for_update().get(pk=night.pk)
            if locked.status in (Night.Status.PAYING, Night.Status.PAID):
                messages.error(request, "This shift was paid in the meantime, so nothing was changed.")
                return redirect("payout_status", pk=night.pk)
            if not saving_only:
                hours_from.finish_running_entries(locked, mark_forgotten=False)  # the manager typed the hours
            locked.shifts.all().delete()
            for member, role, hours in roster:
                Shift.objects.create(night=locked, staff=member, role=role, hours=hours)
            if locked.status == Night.Status.REVIEW:  # the people changed: the old split no longer fits
                locked.shares.all().delete()
                locked.status = Night.Status.CLOSED
                locked.save(update_fields=["status"])
        night.refresh_from_db()
        roster = ", ".join(f"{s.staff.name} {s.hours.normalize():f}h" for s in night.shifts.select_related("staff"))
        Change.log(request, f"{'Saved' if saving_only else 'Closed out with'} who worked on the {night}: {roster or 'nobody'}")
        if saving_only:
            messages.success(request, f"Saved {night.shifts.count()} people on tonight's roster. "
                                      "Update the hours any time before closing.")
            return redirect("close_night_pk", pk=night.pk)
        if night.tip_total <= 0 and not night.tips.filter(status=Tip.Status.CREATED).exists():
            # nothing to share: the shift is done (the hours stay on record)
            Night.objects.filter(pk=night.pk).update(status=Night.Status.PAID, hold="",
                                                     closed_at=night.closed_at or timezone.now())
            Change.log(request, f"Closed the {night} with no tips: nothing to pay")
            messages.success(request, f"The {night} is closed. There were no tips, so there's nothing to pay.")
            return redirect("history")
        try:
            services.build_split(night, rule_set)
        except services.TipPoolError as exc:
            messages.error(request, str(exc))
            return redirect("close_night_pk", pk=night.pk)
        return redirect("review", pk=night.pk)

    # hours nobody typed: clock-ins first, then each person's usual week
    suggested = {} if existing else hours_from.suggested_roster(night)
    rows = []
    for member in staff:
        shift = existing.get(member.pk)
        guess = prefill.get(member.pk)
        auto = suggested.get(member.pk) if not guess else None
        source = "photo" if guess else ("saved" if shift else (auto or {}).get("source", ""))
        rows.append({
            "member": member,
            # first visit with nothing to go on: everyone on (managers switch off who was absent)
            "worked": bool(shift or guess or auto) or (not existing and not prefill and not suggested),
            "role": (guess or {}).get("role") or (shift.role if shift else usual_role.get(member.pk)),
            "hours": (guess or {}).get("hours") or (shift.hours if shift else (auto["hours"] if auto else "")),
            "source": source,
            "running": bool(auto and auto["running"]),
        })
    return render(request, "tips/close_night.html", {
        "night": night, "rows": rows, "tip_count": night.tips.filter(status=Tip.Status.COMPLETED).count(), "roles": Role.objects.filter(location=location), "rule_set": TipRuleSet.active(location),
        "ai_notes": ai_notes, "ai_mode": ai.mode(),
        "manual_tips": list(night.tips.exclude(source=Tip.Source.PAYPAL).select_related("staff")),
        "paypal_total": Night._sum(night.tips.filter(status=Tip.Status.COMPLETED, source=Tip.Source.PAYPAL)),
        "personal_total": night.tip_total - night.pool_total,
        "team": staff, "tip_mode": location.tip_mode,
    })


@manager_required
@require_POST
def night_tip_add(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    staff = None
    if _id(request.POST.get("for")):
        member = Membership.objects.filter(location=night.location, staff_id=_id(request.POST["for"])).first()
        staff = member.staff if member else None
    try:
        tip = services.add_manual_tip(night, request.POST.get("amount") or "0", request.POST.get("source", ""),
                                      staff=staff, note=request.POST.get("note", "").strip())
        Change.log(request, f"Added {request.business.symbol}{tip.amount} {tip.get_source_display().lower()} tips to the {night}"
                            + (f" for {staff.name}" if staff else ""))
        messages.success(request, f"Added {request.business.symbol}{tip.amount} {tip.get_source_display().lower()} tips"
                                  f"{f' for {staff.name}' if staff else ' to the shared pool'}.")
    except (services.TipPoolError, InvalidOperation) as exc:
        messages.error(request, str(exc) if isinstance(exc, services.TipPoolError) else "Enter a valid amount.")
    return redirect("close_night_pk", pk=night.pk)


@manager_required
@require_POST
def night_tip_remove(request, pk):
    tip = get_object_or_404(Tip, pk=pk, night__location=request.location)
    night_pk = tip.night_id
    try:
        services.remove_manual_tip(tip)
        Change.log(request, f"Removed {request.business.symbol}{tip.amount} {tip.get_source_display().lower()} tips from the {tip.night}")
        messages.success(request, "Removed.")
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
    return redirect("close_night_pk", pk=night_pk)


@manager_required
def review(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    if night.status in (Night.Status.OPEN, Night.Status.CLOSED):
        return redirect("close_night_pk", pk=night.pk)
    shares = list(night.shares.select_related("staff", "night"))
    business = request.business
    shared_fees, personal_fees = services.fees_to_share(night)
    passed_on = (shared_fees + sum(personal_fees.values(), Decimal("0"))).quantize(Decimal("0.01"))
    balance = None
    if night.status == Night.Status.REVIEW:
        try:
            balance = services.get_client(business).available_balance(business.currency)
        except PayPalError:
            balance = None
    paid_total = sum((s.amount for s in shares), Decimal("0"))
    return render(request, "tips/review.html", {
        "night": night, "shares": shares, "total": night.tip_total,
        "fees": night.fee_total, "fees_passed_on": passed_on, "to_share": night.tip_total - passed_on,
        "fee_mode": business.fee_mode, "balance": balance,
        "balance_short": balance is not None and balance < paid_total,
        "unconfirmed": services.unconfirmed(shares),
        "check": check_live(night), "stops": check_blocking(night) if night.status == Night.Status.REVIEW else [],
        "paid_total": paid_total,
        "paying_count": sum(1 for s in shares if s.amount > 0),
        "flags": night.messages.filter(resolved_at__isnull=True).select_related("staff"),
        "rule_set": night.rule_set,
    })


@manager_required
@require_POST
@managers_only
def pay(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    from . import check
    stops = check.blocking(night)
    allow = bool(request.POST.get("checked"))
    if stops and not allow:
        messages.error(request, "The close-out check found things to look at. Tick \"I checked these\" to pay anyway.")
        return redirect("review", pk=pk)
    try:
        services.pay_night(night, allow_unconfirmed=allow, by_owner=request.user.manager.is_owner)
        paid = sum((s.amount for s in night.shares.all()), Decimal("0"))
        Change.log(request, f"Paid the {night}: {request.business.symbol}{paid} to "
                            f"{night.shares.filter(amount__gt=0).count()} people"
                            + (" after checking: " + "; ".join(f["text"] for f in stops)[:150] if stops else ""))
        messages.success(request, "Payout sent to PayPal.")
    except services.NeedsOwner as exc:
        Change.log(request, f"Asked the owner to approve paying the {night}")
        messages.warning(request, str(exc))
        return redirect("review", pk=pk)
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
        night.refresh_from_db()
        return redirect("payout_status" if night.status == Night.Status.PAYING else "review", pk=pk)
    except PayPalError as exc:
        messages.error(request, _payout_refusal(exc, night))
        return redirect("review", pk=pk)
    return redirect("payout_status", pk=pk)


def _payout_refusal(exc, night):
    """PayPal's reason in words, naming the person whose payout address it didn't accept."""
    details = (exc.details or {}).get("details") or []
    paid = [s for s in night.shares.select_related("staff") if s.amount > 0]
    for d in details if isinstance(details, list) else []:
        m = re.match(r"items\[(\d+)\]\.receiver", str(d.get("field", "")) if isinstance(d, dict) else "")
        if m and int(m.group(1)) < len(paid):
            person = paid[int(m.group(1))].staff
            return (f"PayPal didn't accept where {person.name} is paid ({person.payout_to}). Fix it on the Team page "
                    "(Edit), then pay again. Nobody was paid.")
    debug = f" (PayPal debug id {exc.debug_id})" if getattr(exc, "debug_id", None) else ""
    return f"PayPal refused the payout: {exc}{debug}. Nobody was paid."


@manager_required
def payout_status(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    batch = getattr(night, "payout_batch", None)
    if batch is None:
        return redirect("review", pk=pk)
    if request.method == "POST":
        try:
            for each in night.payout_batches.all():
                if (each.paypal_batch_id or "").startswith("DEMO"):
                    continue  # sample history: nothing to ask PayPal about
                if services.needs_recheck(each):
                    each = services.recheck_unclear(each)
                services.refresh_batch(each)
        except (PayPalError, services.TipPoolError) as exc:
            messages.error(request, f"Could not check with PayPal: {exc}")
        else:
            messages.success(request, "Checked with PayPal: the statuses below are up to date.")
        return redirect("payout_status", pk=pk)
    items = (PayoutItem.objects.filter(batch__night=night).select_related("share__staff", "batch")
             .order_by("superseded", "-share__amount", "-pk"))
    paid = sum((i.share.amount for i in items if i.status == "SUCCESS" and not i.superseded), Decimal("0"))
    return render(request, "tips/payout_status.html", {
        "night": night, "batch": batch, "items": items, "paid": paid,
        "sample": (batch.paypal_batch_id or "").startswith("DEMO"),
        "unclear": any(services.needs_recheck(b) for b in night.payout_batches.all()),
        "payout_fees": sum((i.fee for i in items), Decimal("0")),
    })


@manager_required
@require_POST
@managers_only
def payout_action(request, pk):
    """Send a failed payout again, cancel an unclaimed one, or remind the person to accept it."""
    item = get_object_or_404(PayoutItem.objects.select_related("share__staff", "share__night"), pk=pk,
                             share__night__location=request.location)
    night, staff, action = item.share.night, item.share.staff, request.POST.get("action")
    try:
        if action == "resend":
            new = services.resend_payout(item)
            Change.log(request, f"Sent {staff.name}'s payout for the {night} again, to {new.receiver}")
            messages.success(request, f"Sent again to {new.receiver}.")
        elif action == "cancel":
            services.cancel_unclaimed(item)
            Change.log(request, f"Took back {staff.name}'s unclaimed payout for the {night}")
            messages.success(request, "Cancelled. The money is back in your PayPal account. Fix where they're paid, "
                                      "then send it again.")
        elif action == "remind":
            from django.core.cache import cache
            if item.status != "UNCLAIMED" or item.superseded:
                messages.error(request, "Only a payout that's waiting to be accepted can be reminded about.")
                return redirect("payout_status", night.pk)
            if request.business.is_demo:
                messages.info(request, "The demo doesn't send emails. In your own account this emails the person.")
                return redirect("payout_status", night.pk)
            if not cache.add(f"payout-remind:{item.pk}", 1, 86400):
                messages.info(request, f"{staff.name} was already reminded today.")
                return redirect("payout_status", night.pk)
            link = request.build_absolute_uri(reverse("my_tips", args=[staff.access_token]))
            from . import emails
            emails.send(staff.paypal_email, f"{request.business.symbol}{item.share.amount} in tips is waiting for you",
                        eyebrow=night.location.display_name,
                        heading=f"{request.business.symbol}{item.share.amount} is waiting for you",
                        greeting=f"Hi {staff.name},",
                        paragraphs=[f"Your tips for the {night} were sent with PayPal, but nobody has accepted them "
                                    "yet. Open PayPal (or sign up with that email) to accept them."],
                        details=[("Sent to", item.receiver), ("Shift", str(night)),
                                 ("Amount", f"{request.business.symbol}{item.share.amount}")],
                        button=("Open PayPal", "https://www.paypal.com/"),
                        links=[("Wrong address? Change it on your tips page", link)])
            if platform.email_enabled():
                messages.success(request, f"Reminder emailed to {staff.paypal_email}.")
            else:
                messages.warning(request, "TipPool's email isn't switched on, so no reminder could be sent. Tell "
                                          f"{staff.name} to accept the payout in PayPal ({item.receiver}).")
        else:
            messages.error(request, "Unknown action.")
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
    except PayPalError as exc:
        if exc.status is None or exc.status >= 500:
            messages.error(request, "PayPal had a problem on their side and nothing changed. Try again in a minute.")
        else:
            debug = f" (PayPal debug id {exc.debug_id})" if getattr(exc, "debug_id", None) else ""
            messages.error(request, f"PayPal said no: {exc}{debug}")
    return redirect("payout_status", pk=night.pk)


@manager_required
def payout_status_json(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    batch = getattr(night, "payout_batch", None)
    if batch is None:
        raise Http404
    for each in night.payout_batches.all():
        try:
            services.refresh_batch(each)
        except (PayPalError, services.TipPoolError):
            pass
    night.refresh_from_db()
    current = PayoutItem.objects.filter(batch__night=night, superseded=False)
    return JsonResponse({
        "batch_status": batch.status, "night_status": night.status,
        "items": {str(i.pk): i.status for i in current},
        "settled": all(i.status in services.ITEM_DONE | services.ITEM_FAILED for i in current),
    })


# --- Settings: automatic close-out --------------------------------------------
from .timezones import TIMEZONES  # noqa: E402


REVIEW_CHOICES = [(0, "Don't wait: pay right away"), (30, "30 minutes"), (60, "1 hour"), (120, "2 hours"),
                  (720, "12 hours"), (1440, "24 hours")]


def _locations_overview(business, only=None):
    rows = list(Location.objects.filter(business=business).order_by("archived", "created_at").annotate(team_count=Count("memberships", filter=Q(memberships__active=True))))
    if only is not None:
        rows = [r for r in rows if r.pk in only]
    for loc in rows:
        loc.type_label = role_presets.PRESETS.get(loc.business_type, {}).get("label", "Business")
    return rows


@manager_required
@managers_only
def settings_page(request):
    from datetime import time as dtime
    restaurant = request.location
    if request.method == "POST":
        business_name = " ".join(request.POST.get("business_name", "").split())[:80]
        if business_name and request.user.manager.is_owner and business_name != request.business.name:
            Change.log(request, f"Renamed the business from {request.business.name} to {business_name}")
            request.business.name = business_name
            request.business.save(update_fields=["name"])
        new_name = " ".join(request.POST.get("name", "").split())[:80]
        if new_name and Location.objects.filter(business=request.business, name__iexact=new_name, archived=False) \
                .exclude(pk=restaurant.pk).exists():
            messages.error(request, f"Another location is already called {new_name}. Pick a name that tells them apart.")
            return redirect(f"{reverse('settings')}#locations")
        restaurant.name = new_name
        restaurant.auto_close_enabled = bool(request.POST.get("auto_close_enabled"))
        restaurant.use_usual_schedule = bool(request.POST.get("use_usual_schedule"))
        if request.POST.get("review_minutes", "").isdigit() and int(request.POST["review_minutes"]) in dict(REVIEW_CHOICES):
            restaurant.review_minutes = int(request.POST["review_minutes"])
        restaurant.tip_message = request.POST.get("tip_message", "").strip()[:160]
        style = request.POST.get("tip_style", "")
        values = []
        for i in range(4):
            try:
                value = Decimal(request.POST.get(f"tip_value_{i}", "") or "0")
                values.append(int(value) if value.is_finite() and abs(value) < 100000 else 0)
            except InvalidOperation:
                values.append(0)
        limit = 100 if style == Location.TipStyle.PERCENT else 500
        ignored = []
        if style in Location.TipStyle.values and all(0 < v <= limit for v in values):
            restaurant.tip_style, restaurant.tip_values = style, sorted(values)
        elif style == "":
            restaurant.tip_style, restaurant.tip_values = "", []
        elif style in Location.TipStyle.values:
            ignored.append(f"the tip buttons (each must be between 1 and {limit})")
        if request.POST.get("tip_mode") in Location.TipMode.values:
            restaurant.tip_mode = request.POST["tip_mode"]
        if request.POST.get("after_close") in Location.AfterClose.values:
            restaurant.after_close = request.POST["after_close"]
        if request.POST.get("timezone") in dict(TIMEZONES):
            restaurant.timezone = request.POST["timezone"]
        restaurant.save()
        for day in range(7):
            raw = request.POST.get(f"time_{day}", "")
            enabled = bool(request.POST.get(f"on_{day}"))
            try:
                hour, minute = (int(x) for x in raw.split(":")[:2])
                value = dtime(hour, minute)
            except ValueError:
                value = None
            if value:
                CloseTime.objects.update_or_create(location=restaurant, weekday=day,
                                                   defaults={"time": value, "enabled": enabled})
            else:
                CloseTime.objects.filter(location=restaurant, weekday=day).update(enabled=False)
                if enabled:
                    ignored.append(f"{CloseTime.WEEKDAYS[day]} (it has no closing time)")
        Change.log(request, "Changed settings: " + ", ".join([
            restaurant.get_tip_mode_display().lower(),
            ("closes automatically" if restaurant.auto_close_enabled else "closes by hand"),
            restaurant.get_after_close_display().lower(),
        ]))
        if ignored:
            messages.warning(request, "Saved, except " + " and ".join(ignored) + ".")
        else:
            messages.success(request, "Settings saved.")
        return redirect("settings")
    existing = {c.weekday: c for c in CloseTime.objects.filter(location=restaurant)}
    days = [{"n": d, "name": CloseTime.WEEKDAYS[d], "close": existing.get(d)} for d in range(7)]
    return render(request, "tips/settings.html", {
        "restaurant": restaurant, "days": days, "timezones": TIMEZONES,
        "tip_modes": Location.TipMode.choices,
        "review_choices": REVIEW_CHOICES,
        "tip_styles": Location.TipStyle.choices,
        "tip_style_now": restaurant.tip_buttons[0], "tip_values_now": restaurant.tip_buttons[1],
        "usual_tips": role_presets.tip_buttons_for(restaurant.business_type),
        "type_label": role_presets.PRESETS.get(restaurant.business_type, {}).get("label", "business").lower(),
        "changes": Change.objects.filter(location=restaurant)[:200 if request.GET.get("all") else 12],
        "show_all_changes": bool(request.GET.get("all")),
        "after_choices": Location.AfterClose.choices, "next_close": autoclose.next_close(restaurant),
        # managers of some locations only see (and can't add or archive) those
        "all_locations": _locations_overview(request.business, None if request.user.manager.all_access
                                             else set(request.allowed_locations.values_list("pk", flat=True))),
        "all_access": request.user.manager.all_access,
        "active_locations": list(Location.objects.filter(business=request.business, archived=False)),
        "digest_choices": request.business.Digest.choices,
        "managers": request.business.managers.select_related("user").order_by("-is_owner", "user__first_name"),
        # invite links are as good as a key: only the owner sees them
        "invites": [(i, request.build_absolute_uri(reverse("accept_invite", args=[i.token]))
                     if request.user.manager.is_owner else "")
                    for i in request.business.invites.filter(accepted_at__isnull=True) if i.usable],
        "is_owner": request.user.manager.is_owner,
        "profile": request.user.manager,
        **two_step_setup_context(request),
        "currencies": list(request.business.CURRENCIES.items()),
        "fee_modes": request.business.FeeMode.choices,
        "has_tips": Tip.objects.filter(night__location__business=request.business).exists(),
        "webhook_url": request.build_absolute_uri(reverse("paypal_webhook")),
        "presets": [(k, p["label"]) for k, p in role_presets.PRESETS.items()],
    })


# --- All locations at a glance ------------------------------------------------------
@manager_required
def overview(request):
    from datetime import timedelta

    from .models import Message
    week_start = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
    rows = []
    for loc in request.allowed_locations:
        night = Night.objects.filter(location=loc, status=Night.Status.OPEN).first()
        waiting = list(Night.objects.filter(location=loc, status__in=[
            Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).order_by("date"))
        week = Night._sum(Tip.objects.filter(night__location=loc, night__date__gte=week_start,
                                             status=Tip.Status.COMPLETED))
        rows.append({
            "location": loc,
            "now": night.tip_total if night else Decimal("0.00"),
            "tips_now": night.tips.filter(status=Tip.Status.COMPLETED).count() if night else 0,
            "on_clock": night.clock_entries.filter(ended_at__isnull=True).count() if night else 0,
            "week": week,
            "waiting": waiting,
            "held": [n for n in waiting if n.hold],
            "messages": Message.objects.filter(location=loc, resolved_at__isnull=True).count(),
            "alerts": Alert.objects.filter(location=loc, resolved_at__isnull=True).count(),
            "pace": insights.pace(loc),
            "trend": insights.week_so_far(loc),
        })
    template = "tips/_board.html" if request.GET.get("partial") else "tips/overview.html"
    return render(request, template, {
        "rows": rows,
        "total_now": sum((r["now"] for r in rows), Decimal("0.00")),
        "total_week": sum((r["week"] for r in rows), Decimal("0.00")),
        "attention": sum(len(r["waiting"]) + r["messages"] + r["alerts"] for r in rows),
    })


# --- Reports -------------------------------------------------------------------
@manager_required
@managers_only
def reports_page(request):
    from datetime import date
    from . import reports
    period = request.GET.get("period", "this_month")
    scope = "all" if request.GET.get("scope") == "all" else "here"

    def parse(value):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    today = timezone.localdate()
    first, last = reports.period_range(period, today, parse(request.GET.get("from")), parse(request.GET.get("to")))
    report = reports.build(reports.scope_locations(request, scope), first, last)
    export = request.GET.get("export")
    if export in ("people", "shifts"):
        body = reports.people_csv(report) if export == "people" else reports.shifts_csv(report)
        response = HttpResponse(body, content_type="text/csv")
        name = f"tips-{export}-{first.isoformat()}-to-{last.isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response
    from . import ask
    question = request.GET.get("q", "").strip()
    asked = ask.answer(question, reports.scope_locations(request, scope), today,
                       location=request.location, user=request.user,
                       named_from=list(request.allowed_locations)) if question else None
    params = request.GET.copy()
    params.pop("q", None)
    return render(request, "tips/reports.html", {
        "report": report, "periods": reports.PERIODS, "period": period, "scope": scope,
        "query": params.urlencode(), "question": question, "asked": asked, "examples": ask.EXAMPLES,
        "ai_mode": ai.mode(),
    })


@manager_required
@require_POST
@managers_only
def ask_confirm(request):
    from . import actions
    try:
        messages.success(request, actions.execute(request.POST.get("token", ""), request))
    except actions.ActionError as exc:
        messages.error(request, str(exc))
    return redirect("reports")


# --- Messages from staff ------------------------------------------------------
@manager_required
def messages_page(request):
    from .models import Message
    business = request.business
    if request.method == "POST":
        if request.POST.get("alert"):
            alert = get_object_or_404(Alert.objects.filter(Q(location__in=request.allowed_locations) |
                                                           Q(location__isnull=True)),
                                      pk=_id(request.POST["alert"]), business=business)
            if alert.key.startswith("owner-ok:") and not request.user.manager.is_owner:
                messages.error(request, "This one waits for the owner. It closes by itself once the shift is paid.")
                return redirect("messages_page")
            alert.resolved_at = timezone.now()
            alert.resolved_by = request.user.get_full_name() or request.user.get_username()
            alert.save(update_fields=["resolved_at", "resolved_by"])
            Change.log(request, f"Marked as handled: {alert.text[:120]}")
            return redirect("messages_page")
        # a message about a shift belongs to that shift's location; a payout-address request to the person,
        # so any manager of a location they work at may answer it
        msg = get_object_or_404(Message.objects.filter(
            Q(location__in=request.allowed_locations) |
            Q(kind=Message.Kind.PAYOUT_CHANGE, staff__memberships__location__in=request.allowed_locations))
            .distinct(), pk=_id(request.POST.get("message")), staff__business=business)
        if msg.kind == Message.Kind.PAYOUT_CHANGE and request.user.manager.is_lead:
            messages.error(request, "Only a manager can approve where someone is paid.")
            return redirect("messages_page")
        if msg.kind == Message.Kind.PAYOUT_CHANGE:
            if not msg.is_open:
                return redirect("messages_page")
            approved = request.POST.get("decision") == "approve"
            if approved:
                before = msg.staff.payout_to
                try:
                    services.apply_payout_change(msg.staff, msg.payload)
                except services.TipPoolError as exc:
                    messages.error(request, str(exc))
                    return redirect("messages_page")
                Change.log(request, f"Approved: {msg.staff.name} is now paid with {msg.staff.payout_to} (was {before})")
            else:
                Change.log(request, f"Declined {msg.staff.name}'s request to change where they're paid")
            msg.reply = "Approved." if approved else (request.POST.get("reply", "").strip()[:1000] or "Declined.")
        else:
            msg.reply = request.POST.get("reply", "").strip()[:1000]
            Change.log(request, f"Resolved {msg.staff.name}'s message" + (f" about the {msg.night}" if msg.night else ""))
        msg.resolved_at = timezone.now()
        msg.resolved_by = request.user.get_full_name() or request.user.get_username()
        msg.save()
        night = msg.night
        if (night and night.status == Night.Status.REVIEW and night.hold.endswith("flagged a problem.")
                and not night.messages.filter(resolved_at__isnull=True).exists()):
            night.hold = ""  # the flag that held it is answered: automatic payout can go ahead
            night.save(update_fields=["hold"])
        messages.success(request, f"Done. {msg.staff.name} sees your answer on their tips page.")
        return redirect("messages_page")
    qs = list(Message.objects.filter(Q(location=request.location) | Q(kind=Message.Kind.PAYOUT_CHANGE,
                                                                      staff__business=business,
                                                                      staff__memberships__location__in=request.allowed_locations))
              .select_related("staff", "night").distinct())
    alerts = Alert.objects.filter(business=business).filter(Q(location=request.location) | Q(location__isnull=True))
    return render(request, "tips/messages.html", {
        "payout_requests": [m for m in qs if m.is_open and m.kind == Message.Kind.PAYOUT_CHANGE],
        "open": [m for m in qs if m.is_open and m.kind == Message.Kind.MESSAGE],
        "resolved": [m for m in qs if not m.is_open][:40],
        "alerts": alerts.filter(resolved_at__isnull=True).select_related("night")[:50],
    })


# --- Locations ----------------------------------------------------------------
@manager_required
@require_POST
def location_switch(request):
    location = get_object_or_404(Location, pk=_id(request.POST.get("location")), business=request.business)
    if not location.archived and not request.allowed_locations.filter(pk=location.pk).exists():
        raise Http404("You don't manage that location.")
    if location.archived:
        messages.error(request, "That location is archived. Restore it in Settings to work there.")
        return redirect("dashboard")
    request.session["location_id"] = location.pk
    nxt = request.POST.get("next", "")
    safe = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return redirect(nxt if safe and nxt.startswith("/") else "dashboard")


@manager_required
@require_POST
@managers_only
def location_archive(request, pk):
    if not request.user.manager.all_access:
        messages.error(request, "Only the owner, or a manager of every location, can archive locations.")
        return redirect("settings")
    location = get_object_or_404(Location, pk=pk, business=request.business)
    restore = request.POST.get("restore") == "1"
    if not restore:
        if not Location.objects.filter(business=request.business, archived=False).exclude(pk=location.pk).exists():
            messages.error(request, "You can't archive your only location.")
            return redirect(f"{reverse('settings')}#locations")
        if Night.objects.filter(location=location, status__in=[Night.Status.CLOSED, Night.Status.REVIEW,
                                                              Night.Status.PAYING]).exists():
            messages.error(request, f"{location.display_name} has shifts waiting to be paid. Finish them first.")
            return redirect(f"{reverse('settings')}#locations")
    location.archived = not restore
    location.save(update_fields=["archived"])
    if not restore and request.session.get("location_id") == location.pk:
        request.session.pop("location_id", None)
    Change.log(request, f"{'Restored' if restore else 'Archived'} the location {location.display_name}", location=location)
    messages.success(request, f"{location.display_name} is {'back' if restore else 'archived: its tip codes no longer work'}.")
    return redirect(f"{reverse('settings')}#locations")


@manager_required
@require_POST
@managers_only
def location_add(request):
    if not request.user.manager.all_access:
        messages.error(request, "Only the owner, or a manager of every location, can add locations.")
        return redirect("settings")
    name = " ".join(request.POST.get("name", "").split())[:80]
    if not name:
        messages.error(request, "Give the new location a name.")
        return redirect("settings")
    if Location.objects.filter(business=request.business, name__iexact=name, archived=False).exists():
        messages.error(request, f"You already have a location called {name}. Pick a name that tells them apart "
                                "(e.g. the street or neighborhood).")
        return redirect(f"{reverse('settings')}#locations")
    business_type = request.POST.get("business_type", "restaurant")
    if business_type not in role_presets.PRESETS:
        business_type = "restaurant"
    location = Location.objects.create(business=request.business, name=name, slug=Location.new_slug(name),
                                       business_type=business_type, timezone=request.location.timezone,
                                       tip_mode=role_presets.default_tip_mode(business_type))
    role_presets.apply_preset(location, business_type)
    Change.log(request, f"Added the location {name}", location=location)
    request.session["location_id"] = location.pk
    messages.success(request, f"{name} added and selected. Add its team, tip rules and QR codes next.")
    return redirect("dashboard")


# --- Staff's own page (secret link, no login) ----------------------------------
@limited("staff-page", 30, 600, key=lambda request, token: token)
def my_tips(request, token):
    member = get_object_or_404(StaffMember, access_token=token)
    home = member.memberships.filter(active=True).select_related("location").first() or member.memberships.first()
    if home is not None:
        timezone.activate(autoclose.location_tz(home.location))
    if request.method == "POST" and request.POST.get("text"):
        return _staff_message(request, member, token)
    if request.method == "POST" and request.POST.get("payout"):
        return _staff_payout(request, member, token)
    if request.method == "POST":
        try:
            if request.POST.get("clock") == "in":
                location = Location.objects.filter(pk=_id(request.POST.get("location")), archived=False,
                                                   memberships__staff=member, memberships__active=True).first()
                if location is None:
                    raise hours_from.ClockError("You're not on the team there any more. Ask your manager.")
                hours_from.clock_in(member, location)
                messages.success(request, f"Clocked in at {location.display_name}. Have a good shift!")
            elif request.POST.get("clock") == "out":
                entry = hours_from.clock_out(member)
                minutes = int(round(float(entry.hours()) * 60))
                spent = (f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else
                         f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else "less than a minute")
                messages.success(request, f"Clocked out. You worked {spent} this time.")
        except hours_from.ClockError as exc:
            messages.error(request, str(exc))
        return redirect("my_tips", token=token)
    running = hours_from.running_entry(member)
    totals = _staff_totals(member)
    shares = member.share_set.select_related("night__location").order_by("-night__date", "-night__created_at")[:30]
    locations = [m.location for m in member.memberships.filter(active=True).select_related("location")]
    shares = list(shares)
    by_night = {}
    for msg in member.messages.filter(night__isnull=False):
        by_night.setdefault(msg.night_id, []).append(msg)
    for share in shares:
        share.night_messages = by_night.get(share.night_id, [])
        share.paid = share.night.status == Night.Status.PAID
    return render(request, "tips/my_tips.html", {
        "cur": member.business.symbol, "business_name": member.business.name,
        "member": member, "shares": shares, "member_locations": locations, "several": len(locations) > 1,
        "running": running, "totals": totals,
        "general_messages": member.messages.filter(night__isnull=True)[:10],
        "payout_request": member.messages.filter(kind="payout", resolved_at__isnull=True).first(),
    })


def _staff_totals(member):
    """This week / month / year for one person (their share of every shift, all locations)."""
    from datetime import timedelta
    today = timezone.localdate()
    starts = [("This week", today - timedelta(days=today.weekday())), ("This month", today.replace(day=1)),
              ("This year", today.replace(month=1, day=1))]
    shares = member.share_set.filter(night__date__gte=starts[-1][1]).values_list("night__date", "amount")
    return [(label, sum((a for d, a in shares if d >= start), Decimal("0.00"))) for label, start in starts]


def staff_manifest(request, token):
    """Lets a worker add their tips page to their phone's home screen, like an app."""
    member = get_object_or_404(StaffMember, access_token=token)
    start = reverse("my_tips", args=[token])
    data = {
        "name": f"My tips: {member.business.name}", "short_name": "My tips",
        "start_url": start, "scope": start, "display": "standalone",
        "background_color": "#e7e8e4", "theme_color": "#141414",
        "icons": [{"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                  {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    }
    response = JsonResponse(data, content_type="application/manifest+json")
    response["Cache-Control"] = "private, max-age=3600"
    return response


PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{8,18}[0-9]$")


def _staff_payout(request, member, token):
    """The worker confirms where they're paid, or asks to change it (a manager approves changes,
    so someone who got hold of the link can't send the money elsewhere)."""
    from .alerts import email_managers
    from .models import Message
    if request.POST["payout"] == "confirm":
        services.confirm_payout(member)
        messages.success(request, f"Thanks! Your tips go to {member.payout_to}.")
        return redirect("my_tips", token=token)
    method = request.POST.get("method", "paypal")
    payload = {"method": "venmo" if method == "venmo" else "paypal"}
    if payload["method"] == "venmo":
        phone = request.POST.get("phone", "").strip()
        if not PHONE_RE.match(phone):
            messages.error(request, "Enter the phone number on your Venmo account, e.g. +1 555 123 4567.")
            return redirect("my_tips", token=token)
        payload["phone"] = phone
        wanted = f"Venmo {phone}"
    else:
        email = request.POST.get("email", "").strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Enter the email of your PayPal account.")
            return redirect("my_tips", token=token)
        if StaffMember.objects.filter(business=member.business, paypal_email__iexact=email).exclude(pk=member.pk).exists():
            messages.error(request, "Someone else on your team is already paid at that email. Use your own PayPal.")
            return redirect("my_tips", token=token)
        payload["email"] = email
        wanted = f"PayPal {email}"
    location = member.memberships.filter(active=True).select_related("location").first()
    if location is None:
        messages.error(request, "You're not on an active team right now, so there's no manager to approve this. "
                                "Ask your manager.")
        return redirect("my_tips", token=token)
    member.messages.filter(kind="payout", resolved_at__isnull=True).delete()  # only the latest request counts
    Message.objects.create(staff=member, location=location.location, kind=Message.Kind.PAYOUT_CHANGE,
                           payload=payload, text=f"Please pay me with {wanted} (now: {member.payout_to}).")
    email_managers(member.business, f"{member.name} wants to be paid somewhere else",
                   f"{member.name} asked to be paid with {wanted} instead of {member.payout_to}.\n\n"
                   "Until you approve it, they're still paid the old way.", location=location.location, leads=False,
                   button="Approve or decline")
    messages.success(request, f"Sent to your manager. Until they approve it, you're still paid with {member.payout_to}.")
    return redirect("my_tips", token=token)


def _staff_message(request, member, token):
    from .models import Message
    text = request.POST.get("text", "").strip()[:1000]
    night = None
    if request.POST.get("flag_night"):
        night = Night.objects.filter(pk=_id(request.POST["flag_night"]), shares__staff=member).first()
        location = night.location if night else None
    else:
        location = Location.objects.filter(pk=_id(request.POST.get("location")), memberships__staff=member).first()
    if not text:
        messages.error(request, "Please write a message first.")
        return redirect("my_tips", token=token)
    if location is None:
        messages.error(request, "Pick where you work, then send it again.")
        return redirect("my_tips", token=token)
    Message.objects.create(staff=member, location=location, night=night, text=text)
    from .alerts import email_managers
    email_managers(member.business, f"{member.name} {'flagged a problem' if night else 'sent you a message'}",
                   f"{member.name} wrote" + (f" about the {night}" if night else "") + ":", location=location,
                   quote=text, button="Answer in Messages",
                   heading=f"{member.name} {'flagged a problem' if night else 'sent you a message'}")
    if night and night.status == Night.Status.REVIEW and not night.hold:
        night.hold = f"{member.name} flagged a problem."
        night.save(update_fields=["hold"])
    messages.success(request, "Sent. Your manager will see it" + (" before the money goes out." if night else "."))
    return redirect("my_tips", token=token)


# --- PayPal webhooks ----------------------------------------------------------
@csrf_exempt
@require_POST
@limited("webhook", 120, 60)
def paypal_webhook(request):
    """PayPal tells us something happened. A verified (signed) event is applied as is. An unverified one
    is never trusted: it is only a hint to ask PayPal's API what really happened."""
    try:
        event = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("bad json")
    if not isinstance(event, dict):
        return HttpResponseBadRequest("bad event")
    event_id = str(event.get("id", ""))[:64]
    if not event_id or WebhookEvent.objects.filter(event_id=event_id).exists():
        return HttpResponse("ok")  # duplicate or junk: acknowledge so PayPal stops retrying

    business = _webhook_business(event)
    webhook_id = webhook_id_for(business)
    if not webhook_id:
        _check_with_paypal(event, business)
        return HttpResponse("ok")
    try:
        verified = get_client(business).verify_webhook(request.headers, event, webhook_id)
    except PayPalError:
        verified = False
    if not verified:
        log.warning("Rejected unverified webhook %s", event_id)
        return HttpResponse(status=400)

    WebhookEvent.objects.create(event_id=event_id, event_type=str(event.get("event_type", ""))[:80],
                                verified=True, payload=event)
    event_type = event.get("event_type", "")
    resource = _as_dict(event.get("resource"))
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        order_id = _dig(resource, "supplementary_data", "related_ids", "order_id")
        if order_id:
            services.mark_capture_completed(order_id, resource.get("id", ""))
    elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        capture_id = _capture_id_of(resource)
        if capture_id:
            total = _dig(resource, "seller_payable_breakdown", "total_refunded_amount", "value")
            services.tip_given_back(capture_id, "refund" if event_type.endswith("REFUNDED") else "reversal",
                                    refunded_total=total)
    elif event_type == "CUSTOMER.DISPUTE.CREATED":
        for row in _as_list(resource.get("disputed_transactions")):
            if isinstance(row, dict) and row.get("seller_transaction_id"):
                services.tip_given_back(row["seller_transaction_id"], "dispute")
    elif event_type.startswith("PAYMENT.PAYOUTS-ITEM."):
        services.apply_payout_item_event(resource)
    elif event_type.startswith("PAYMENT.PAYOUTSBATCH."):
        batch_id = _dig(resource, "batch_header", "payout_batch_id")
        batch = PayoutBatch.objects.filter(paypal_batch_id=batch_id).first()
        if batch:
            try:
                services.refresh_batch(batch)
            except PayPalError:
                log.exception("Could not refresh batch %s", batch_id)
    return HttpResponse("ok")


def _check_with_paypal(event, business):
    """No webhook id to verify with: look the thing up at PayPal ourselves. Nothing in the event's
    body is believed, so a forged event can at most make us ask PayPal a question."""
    event_type = str(event.get("event_type", ""))
    resource = _as_dict(event.get("resource"))
    try:
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            order_id = _dig(resource, "supplementary_data", "related_ids", "order_id")
            if order_id and Tip.objects.filter(paypal_order_id=order_id).exists():
                services.sync_tip(order_id)
        elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
            capture_id = _capture_id_of(resource)
            if capture_id and Tip.objects.filter(paypal_capture_id=capture_id).exists():
                services.sync_capture(capture_id)
        elif event_type.startswith("PAYMENT.PAYOUTS"):
            sender_item_id = _dig(resource, "payout_item", "sender_item_id")
            item = PayoutItem.objects.filter(sender_item_id=sender_item_id).first() if sender_item_id else None
            batch = item.batch if item else PayoutBatch.objects.filter(
                paypal_batch_id=_dig(resource, "batch_header", "payout_batch_id") or "-").first()
            if batch:
                services.refresh_batch(batch)
        # disputes need a signed event: without one we don't raise alarms from unverified data
    except (PayPalError, services.TipPoolError):
        log.warning("Could not check unverified webhook %s with PayPal", event.get("id"))


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _dig(obj, *keys):
    """obj[k1][k2]... or None, whatever shape a (possibly forged) webhook body has."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj if isinstance(obj, (str, int)) else None


def _capture_id_of(resource):
    """REFUNDED events describe the refund; the tip's capture is its "up" link."""
    for link in _as_list(resource.get("links")):
        if isinstance(link, dict) and link.get("rel") == "up" and "/captures/" in str(link.get("href", "")):
            return str(link["href"]).rstrip("/").rsplit("/", 1)[-1]
    return resource.get("id") if resource.get("status") in ("REVERSED", "COMPLETED") else None


def _webhook_business(event):
    """Which business an event is about, so it's checked against that business's own PayPal account."""
    resource = _as_dict(event.get("resource"))
    tip = None
    order_id = _dig(resource, "supplementary_data", "related_ids", "order_id")
    if order_id:
        tip = Tip.objects.filter(paypal_order_id=order_id).first()
    if tip is None:
        capture_ids = [_capture_id_of(resource)] + [row.get("seller_transaction_id")
                                                   for row in _as_list(resource.get("disputed_transactions"))
                                                   if isinstance(row, dict)]
        tip = Tip.objects.filter(paypal_capture_id__in=[c for c in capture_ids if c]).first()
    if tip is not None:
        return tip.night.location.business
    sender_item_id = _dig(resource, "payout_item", "sender_item_id")
    batch_id = _dig(resource, "batch_header", "payout_batch_id") or resource.get("payout_batch_id")
    item = PayoutItem.objects.filter(sender_item_id=sender_item_id).first() if sender_item_id else None
    batch = item.batch if item else (PayoutBatch.objects.filter(paypal_batch_id=batch_id).first() if batch_id else None)
    return batch.night.location.business if batch else None


@managers_only
def digest_preview(request):
    """The owner's summary email, shown in the app (also handy before email is set up)."""
    from . import digest
    kind = request.GET.get("kind") if request.GET.get("kind") in ("daily", "weekly") else (
        request.business.digest if request.business.digest != "off" else "weekly")
    today = timezone.now().astimezone(autoclose.location_tz(request.location)).date()
    data = digest.build(request.business, list(request.allowed_locations), today, kind=kind)
    return render(request, "tips/digest_preview.html", {**data, "kind": kind, "base_url": ""})


@managers_only
def activity(request):
    """Who did what, across the locations you manage: filter by person, location, words and dates; CSV export."""
    import csv
    from datetime import date

    from .reports import _cell
    allowed = list(request.allowed_locations)
    rows = Change.objects.filter(location__in=allowed).select_related("location")
    where = _id(request.GET.get("location"))
    if where and any(loc.pk == where for loc in allowed):
        rows = rows.filter(location_id=where)
    who = request.GET.get("who", "")[:150]
    if who:
        rows = rows.filter(who=who)
    words = request.GET.get("q", "").strip()[:100]
    if words:
        rows = rows.filter(what__icontains=words)

    def day(value):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    first, last = day(request.GET.get("from")), day(request.GET.get("to"))
    if first and last and first > last:
        first, last = last, first
    if first:
        rows = rows.filter(at__date__gte=first)
    if last:
        rows = rows.filter(at__date__lte=last)
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="tippool-activity.csv"'
        out = csv.writer(response)
        out.writerow(["When", "Location", "Who", "What"])
        for c in rows[:20000]:
            out.writerow([_cell(timezone.localtime(c.at).strftime("%Y-%m-%d %H:%M")), _cell(c.location.display_name if c.location else ""),
                          _cell(c.who), _cell(c.what)])
        return response
    query = request.GET.copy()
    query.pop("export", None)
    return render(request, "tips/activity.html", {
        "changes": rows[:300], "count": rows.count(), "locations": allowed, "where": where, "who": who, "q": words,
        "first": first, "last": last, "people": sorted(set(Change.objects.filter(location__in=allowed)
                                                          .values_list("who", flat=True))),
        "query": query.urlencode(),
    })
