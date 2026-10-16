import io
from collections import Counter
import json
import logging
from decimal import Decimal, InvalidOperation

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai, autoclose, hours as hours_from, roles as role_presets, services
from .models import (Change, CloseTime, Location, Membership, Night, PayoutBatch, Role, Shift, StaffMember, Tip,
                     TipRuleSet, WebhookEvent)
from .accounts import manager_required
from .middleware import public_location
from .paypal import PayPalError, get_client

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
    total = night.tip_total
    staff_count = Membership.objects.filter(location=location, active=True).count()
    rule_set = TipRuleSet.active(location)
    any_tip_ever = Tip.objects.filter(night__location=location, status=Tip.Status.COMPLETED).exists()
    return render(request, "tips/dashboard.html", {
        "night": night,
        "tips": tips[:40],
        "tip_count": len(tips),
        "total": total,
        "average": (total / len(tips)).quantize(Decimal("0.01")) if tips else None,
        "unfinished": Night.objects.filter(location=location, status__in=[
            Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).first(),
        "next_close": autoclose.next_close(location),
        "roster_count": night.shifts.count(),
        "on_clock": list(night.clock_entries.filter(ended_at__isnull=True).select_related("staff")),
        "rule_set": rule_set,
        "staff_count": staff_count,
        "setup": [
            {"done": bool(location.name), "text": "Add your business name", "url": reverse("settings")},
            {"done": staff_count > 0, "text": "Add your team and their PayPal emails", "url": reverse("staff_list")},
            {"done": bool(rule_set), "text": "Describe how your house shares tips", "url": reverse("rules")},
            {"done": any_tip_ever, "text": "Print table QR codes so guests can tip", "url": reverse("qr_page")},
        ],
        "ai_mode": ai.mode(),
        "setup_done": staff_count > 0 and bool(rule_set) and any_tip_ever and bool(location.name),
    })


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
    nights = (Night.objects.filter(location=request.location).exclude(status=Night.Status.OPEN)
              .prefetch_related("shares", "tips").select_related("payout_batch")[:60])
    rows = []
    for n in nights:
        rows.append({"night": n, "total": n.tip_total, "people": sum(1 for s in n.shares.all() if s.amount > 0),
                     "batch": getattr(n, "payout_batch", None)})
    return render(request, "tips/history.html", {"rows": rows})


# --- Customer tip page (public) -----------------------------------------------
def tip_page(request):
    location = public_location(request)
    people = []
    if location.tip_mode != Location.TipMode.POOL:
        # who's working right now first, then the rest of the team
        working = set(Night.current(location).clock_entries.filter(ended_at__isnull=True).values_list("staff_id", flat=True))
        working |= set(Night.current(location).shifts.values_list("staff_id", flat=True))
        members = Membership.objects.filter(location=location, active=True).select_related("staff")
        members = [m for m in members if not Role.objects.filter(location=location, key=m.default_role, no_tips=True).exists()]
        people = sorted(members, key=lambda m: (m.staff_id not in working, m.staff.name))
    to = request.GET.get("to", "")
    style, values = location.tip_buttons
    return render(request, "tips/tip_page.html", {
        "location": location, "restaurant_name": location.display_name, "restaurant_named": bool(location.name),
        "at": location.slug,
        "tip_style": style,
        "presets": values,
        "default_pick": values[1] if len(values) > 1 else values[0],
        "tip_message": location.tip_message,
        "tip_mode": location.tip_mode,
        "people": people,
        "to": int(to) if to.isdigit() and any(m.pk == int(to) for m in people) else None,
        "cancelled": "cancelled" in request.GET,
        "table": request.GET.get("table", "")[:20],
        "paypal_client_id": settings.PAYPAL_CLIENT_ID,
        "paypal_sdk_url": settings.PAYPAL_SDK_URL,
    })


def tip_thanks(request):
    # "order" comes from our page; "token" is what PayPal adds when it redirects back here
    order_id = request.GET.get("order") or request.GET.get("token") or ""
    tip = Tip.objects.filter(paypal_order_id=order_id).first()
    if tip and tip.status != Tip.Status.COMPLETED:
        try:
            tip = services.capture_tip(order_id)
        except PayPalError:
            log.warning("Capture on return page failed for %s", order_id)
    if tip:  # show the business the tip went to, even without ?at= in the return link
        request.location = tip.night.location
    return render(request, "tips/tip_thanks.html", {
        "tip": tip, "table": tip.table if tip else "", "location": request.location,
        "restaurant_name": request.location.display_name, "restaurant_named": bool(request.location.name),
    })


@require_POST
def api_create_order(request):
    try:
        data = json.loads(request.body or "{}")
        amount = Decimal(str(data.get("amount", "")))
        table = str(data.get("table", ""))
        location = Location.objects.filter(slug=str(data.get("at", ""))).first() or Location.default()
        for_staff = None
        if data.get("to"):
            member = Membership.objects.filter(pk=int(data["to"]), location=location, active=True).first()
            if member is None:
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


# --- QR code for tables -------------------------------------------------------
@manager_required
def qr_page(request):
    restaurant = request.location
    if request.method == "POST":
        try:
            restaurant.table_count = max(0, min(200, int(request.POST.get("table_count", 0))))
        except ValueError:
            pass
        spots = [sp.strip()[:20] for sp in request.POST.getlist("spots") if sp.strip()]
        extra = request.POST.get("new_spot", "").strip()[:20]
        if extra:
            spots.append(extra)
        restaurant.spots = list(dict.fromkeys(spots))[:24]  # keep order, drop duplicates
        restaurant.save()
        Change.log(request, f"Table codes: {restaurant.table_count} tables" + (f", {', '.join(restaurant.spots)}" if restaurant.spots else ""))
        messages.success(request, "Table codes updated.")
        return redirect("qr_page")
    base = request.build_absolute_uri(reverse("tip_page")) + f"?at={restaurant.slug}"
    return render(request, "tips/qr_page.html", {
        "restaurant": restaurant,
        "house_url": base,
        "tables": [(f"Table {label}" if label.isdigit() else label, f"{base}&table={label}", label)
                   for label in restaurant.table_labels],
        "suggestions": Location.SPOT_SUGGESTIONS,
        "custom_spots": [sp for sp in restaurant.spots if sp not in Location.SPOT_SUGGESTIONS],
        "people": [(m.staff.name, m.role_name, f"{base}&to={m.pk}", m.pk)
                   for m in Membership.objects.filter(location=restaurant, active=True).select_related("staff")
                   ] if restaurant.tip_mode != Location.TipMode.POOL else [],
    })


def qr_show(request):
    """Full-screen QR for a server's phone or a counter tablet. No login, it only links to the tip page."""
    table = request.GET.get("table", "")[:20]
    to = request.GET.get("to", "")
    member = Membership.objects.filter(pk=to, location=request.location).select_related("staff").first() if to.isdigit() else None
    url = request.build_absolute_uri(reverse("tip_page")) + f"?at={request.location.slug}" + (
        f"&table={table}" if table else "") + (f"&to={member.pk}" if member else "")
    label = f"Tip {member.staff.name}" if member else (
        (f"Table {table}" if table.isdigit() else table) if table else "Leave a tip")
    return render(request, "tips/qr_show.html", {"url": url, "label": label, "table": table})


def qr_svg(request):
    url = request.GET.get("url", "")
    if not url.startswith(request.build_absolute_uri("/")):
        return HttpResponseBadRequest("Only links to this site.")
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return HttpResponse(buffer.getvalue(), content_type="image/svg+xml")


# --- Staff ---------------------------------------------------------------------
@manager_required
def staff_list(request):
    location = request.location
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("paypal_email", "").strip()
        role = request.POST.get("default_role", "")
        if not (name and email and Role.objects.filter(location=location, key=role).exists()):
            messages.error(request, "Name, PayPal email and role are required.")
            return redirect("staff_list")
        # the same person at another location (same PayPal email) gets one tips page for everything
        person = StaffMember.objects.filter(business=request.business, paypal_email__iexact=email).first()
        if person is None:
            person = StaffMember.objects.create(business=request.business, name=name, paypal_email=email)
        membership, made = Membership.objects.get_or_create(staff=person, location=location,
                                                            defaults={"default_role": role})
        if not made:
            membership.active, membership.default_role = True, role
            membership.save()
        Change.log(request, f"Added {person.name} ({Role.label(role, location.pk)}) to the team")
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


@manager_required
@require_POST
def staff_role(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    role = request.POST.get("default_role", "")
    if Role.objects.filter(location=request.location, key=role).exists():
        member.default_role = role
        member.save(update_fields=["default_role"])
        Change.log(request, f"Changed {member.staff.name}'s usual role to {member.role_name}")
        messages.success(request, f"{member.staff.name} is now {member.role_name.lower()}.")
    return redirect("staff_list")


@manager_required
@require_POST
def staff_new_link(request, pk):
    """The old private link stops working (e.g. it was sent to the wrong person)."""
    from .models import new_access_token
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    member.staff.access_token = new_access_token()
    member.staff.save(update_fields=["access_token"])
    Change.log(request, f"Made a new tips link for {member.staff.name} (the old one no longer works)")
    messages.success(request, f"New link made for {member.staff.name}. Copy it and send it to them: "
                              "the old one no longer works.")
    return redirect("staff_list")


@manager_required
@require_POST
def staff_schedule(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    week = {}
    for day in range(7):
        try:
            value = Decimal(request.POST.get(f"h_{day}", "") or "0")
        except InvalidOperation:
            value = Decimal("0")
        if 0 < value <= 24:
            week[str(day)] = float(value.quantize(Decimal("0.01")))
    member.schedule = week
    member.save(update_fields=["schedule"])
    Change.log(request, f"Set {member.staff.name}'s usual week: {member.schedule_summary or 'no fixed hours'}")
    messages.success(request, f"{member.staff.name}'s usual week saved: {member.schedule_summary or 'no fixed hours'}.")
    return redirect("staff_list")


@manager_required
@require_POST
def staff_toggle(request, pk):
    member = get_object_or_404(Membership, pk=pk, location=request.location)
    member.active = not member.active
    member.save()
    Change.log(request, f"{'Brought back' if member.active else 'Removed'} {member.staff.name}")
    return redirect("staff_list")


# --- Roles: each location has its own ----------------------------------------------
@manager_required
@require_POST
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
def rules_page(request):
    preview = None
    text = ""
    if request.method == "POST" and "save" in request.POST:
        preview = request.session.pop("rules_preview", None)
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
            preview = {"text": text, **parsed}
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
    staff = [m.staff for m in Membership.objects.filter(location=location, active=True).select_related("staff")]
    usual_role = {m.staff_id: m.default_role for m in Membership.objects.filter(location=location)}
    existing = {s.staff_id: s for s in night.shifts.all()}
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
        if not saving_only:
            hours_from.finish_running_entries(night)
        night.shifts.all().delete()
        role_keys = set(Role.objects.filter(location=location).values_list("key", flat=True))
        for member in staff:
            if not request.POST.get(f"worked_{member.pk}"):
                continue
            try:
                hours = Decimal(request.POST.get(f"hours_{member.pk}", "0") or "0")
            except InvalidOperation:
                hours = Decimal("0")
            role = request.POST.get(f"role_{member.pk}", usual_role.get(member.pk))
            if hours > 0 and role in role_keys:
                Shift.objects.create(night=night, staff=member, role=role, hours=hours)
        roster = ", ".join(f"{s.staff.name} {s.hours.normalize():f}h" for s in night.shifts.select_related("staff"))
        Change.log(request, f"{'Saved' if saving_only else 'Closed out with'} who worked on the {night}: {roster or 'nobody'}")
        if saving_only:
            messages.success(request, f"Saved {night.shifts.count()} people on tonight's roster. "
                                      "Update the hours any time before closing.")
            return redirect("close_night_pk", pk=night.pk)
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
    if request.POST.get("for"):
        member = Membership.objects.filter(location=night.location, staff_id=request.POST["for"]).first()
        staff = member.staff if member else None
    try:
        tip = services.add_manual_tip(night, request.POST.get("amount") or "0", request.POST.get("source", ""),
                                      staff=staff, note=request.POST.get("note", "").strip())
        Change.log(request, f"Added ${tip.amount} {tip.get_source_display().lower()} tips to the {night}"
                            + (f" for {staff.name}" if staff else ""))
        messages.success(request, f"Added ${tip.amount} {tip.get_source_display().lower()} tips"
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
        Change.log(request, f"Removed ${tip.amount} {tip.get_source_display().lower()} tips from the {tip.night}")
        messages.success(request, "Removed.")
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
    return redirect("close_night_pk", pk=night_pk)


@manager_required
def review(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    if night.status in (Night.Status.OPEN, Night.Status.CLOSED):
        return redirect("close_night_pk", pk=night.pk)
    shares = list(night.shares.select_related("staff"))
    return render(request, "tips/review.html", {
        "night": night, "shares": shares, "total": night.tip_total,
        "paid_total": sum((s.amount for s in shares), Decimal("0")),
        "paying_count": sum(1 for s in shares if s.amount > 0),
        "flags": night.messages.filter(resolved_at__isnull=True).select_related("staff"),
        "rule_set": night.rule_set,
    })


@manager_required
@require_POST
def pay(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    try:
        services.pay_night(night)
        Change.log(request, f"Paid the {night}: ${night.tip_total} to {night.shares.filter(amount__gt=0).count()} people")
        messages.success(request, "Payout sent to PayPal.")
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
        return redirect("review", pk=pk)
    except PayPalError as exc:
        messages.error(request, f"PayPal refused the payout: {exc} (debug id {exc.debug_id})")
        return redirect("review", pk=pk)
    return redirect("payout_status", pk=pk)


@manager_required
def payout_status(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    batch = getattr(night, "payout_batch", None)
    if batch is None:
        return redirect("review", pk=pk)
    if request.method == "POST":
        try:
            services.refresh_batch(batch)
        except PayPalError as exc:
            messages.error(request, f"Could not refresh from PayPal: {exc}")
        return redirect("payout_status", pk=pk)
    items = batch.items.select_related("share__staff").order_by("-share__amount")
    return render(request, "tips/payout_status.html", {"night": night, "batch": batch, "items": items})


@manager_required
def payout_status_json(request, pk):
    night = get_object_or_404(Night, pk=pk, location=request.location)
    batch = getattr(night, "payout_batch", None)
    if batch is None:
        raise Http404
    try:
        services.refresh_batch(batch)
    except PayPalError:
        pass
    night.refresh_from_db()
    return JsonResponse({
        "batch_status": batch.status, "night_status": night.status,
        "items": {str(i.pk): i.status for i in batch.items.all()},
    })


# --- Settings: automatic close-out --------------------------------------------
TIMEZONES = [
    ("America/New_York", "US Eastern"), ("America/Chicago", "US Central"), ("America/Denver", "US Mountain"),
    ("America/Los_Angeles", "US Pacific"), ("Europe/London", "UK"), ("Europe/Bucharest", "Romania"),
    ("Europe/Berlin", "Central Europe"), ("UTC", "UTC"),
]


REVIEW_CHOICES = [(0, "Don't wait: pay right away"), (30, "30 minutes"), (60, "1 hour"), (120, "2 hours"),
                  (720, "12 hours"), (1440, "24 hours")]


def _locations_overview(business):
    rows = list(Location.objects.filter(business=business).annotate(team_count=Count("memberships", filter=Q(memberships__active=True))))
    for loc in rows:
        loc.type_label = role_presets.PRESETS.get(loc.business_type, {}).get("label", "Business")
    return rows


@manager_required
def settings_page(request):
    from datetime import time as dtime
    restaurant = request.location
    if request.method == "POST":
        restaurant.name = request.POST.get("name", "").strip()[:80]
        restaurant.auto_close_enabled = bool(request.POST.get("auto_close_enabled"))
        restaurant.use_usual_schedule = bool(request.POST.get("use_usual_schedule"))
        if request.POST.get("review_minutes", "").isdigit() and int(request.POST["review_minutes"]) in dict(REVIEW_CHOICES):
            restaurant.review_minutes = int(request.POST["review_minutes"])
        restaurant.tip_message = request.POST.get("tip_message", "").strip()[:160]
        style = request.POST.get("tip_style", "")
        values = []
        for i in range(4):
            try:
                values.append(int(Decimal(request.POST.get(f"tip_value_{i}", "") or "0")))
            except InvalidOperation:
                values.append(0)
        limit = 100 if style == Location.TipStyle.PERCENT else 500
        if style in Location.TipStyle.values and all(0 < v <= limit for v in values):
            restaurant.tip_style, restaurant.tip_values = style, sorted(values)
        elif style == "":
            restaurant.tip_style, restaurant.tip_values = "", []
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
        Change.log(request, "Changed settings: " + ", ".join([
            restaurant.get_tip_mode_display().lower(),
            ("closes automatically" if restaurant.auto_close_enabled else "closes by hand"),
            restaurant.get_after_close_display().lower(),
        ]))
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
        "all_locations": _locations_overview(request.business),
        "managers": request.business.managers.select_related("user").order_by("-is_owner", "user__first_name"),
        "invites": [(i, request.build_absolute_uri(reverse("accept_invite", args=[i.token])))
                    for i in request.business.invites.filter(accepted_at__isnull=True) if i.usable],
        "is_owner": request.user.manager.is_owner,
        "presets": [(k, p["label"]) for k, p in role_presets.PRESETS.items()],
    })


# --- Reports -------------------------------------------------------------------
@manager_required
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
    asked = ask.answer(question, reports.scope_locations(request, scope), today) if question else None
    params = request.GET.copy()
    params.pop("q", None)
    return render(request, "tips/reports.html", {
        "report": report, "periods": reports.PERIODS, "period": period, "scope": scope,
        "query": params.urlencode(), "question": question, "asked": asked, "examples": ask.EXAMPLES,
        "ai_mode": ai.mode(),
    })


# --- Messages from staff ------------------------------------------------------
@manager_required
def messages_page(request):
    from .models import Message
    if request.method == "POST":
        msg = get_object_or_404(Message, pk=request.POST.get("message"), location=request.location)
        msg.reply = request.POST.get("reply", "").strip()[:1000]
        msg.resolved_at = timezone.now()
        msg.resolved_by = request.user.get_username()
        msg.save()
        Change.log(request, f"Resolved {msg.staff.name}'s message" + (f" about the {msg.night}" if msg.night else ""))
        messages.success(request, f"Resolved. {msg.staff.name} sees your reply on their tips page.")
        return redirect("messages_page")
    qs = Message.objects.filter(location=request.location).select_related("staff", "night")
    return render(request, "tips/messages.html", {
        "open": [m for m in qs if m.is_open], "resolved": [m for m in qs if not m.is_open][:40],
    })


# --- Locations ----------------------------------------------------------------
@manager_required
@require_POST
def location_switch(request):
    location = get_object_or_404(Location, pk=request.POST.get("location"), business=request.business)
    request.session["location_id"] = location.pk
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "dashboard")


@manager_required
@require_POST
def location_add(request):
    name = request.POST.get("name", "").strip()[:80]
    if not name:
        messages.error(request, "Give the new location a name.")
        return redirect("settings")
    business_type = request.POST.get("business_type", "restaurant")
    if business_type not in role_presets.PRESETS:
        business_type = "restaurant"
    location = Location.objects.create(business=request.business, name=name, slug=Location.new_slug(name),
                                       business_type=business_type,
                                       timezone=request.location.timezone)
    role_presets.apply_preset(location, business_type)
    Change.log(request, f"Added the location {name}", location=location)
    request.session["location_id"] = location.pk
    messages.success(request, f"{name} added and selected. Add its team, tip rules and QR codes next.")
    return redirect("dashboard")


# --- Staff's own page (secret link, no login) ----------------------------------
def my_tips(request, token):
    member = get_object_or_404(StaffMember, access_token=token)
    if request.method == "POST" and request.POST.get("text"):
        return _staff_message(request, member, token)
    if request.method == "POST":
        try:
            if request.POST.get("clock") == "in":
                location = get_object_or_404(Location, pk=request.POST.get("location"))
                hours_from.clock_in(member, location)
                messages.success(request, f"Clocked in at {location.display_name}. Have a good shift!")
            elif request.POST.get("clock") == "out":
                entry = hours_from.clock_out(member)
                messages.success(request, f"Clocked out. {entry.hours().normalize():f} hours this time.")
        except hours_from.ClockError as exc:
            messages.error(request, str(exc))
        return redirect("my_tips", token=token)
    running = hours_from.running_entry(member)
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
        "member": member, "shares": shares, "member_locations": locations, "several": len(locations) > 1,
        "running": running, "general_messages": member.messages.filter(night__isnull=True)[:10],
    })


def _staff_message(request, member, token):
    from .models import Message
    text = request.POST.get("text", "").strip()[:1000]
    night = None
    if request.POST.get("flag_night"):
        night = Night.objects.filter(pk=request.POST["flag_night"], shares__staff=member).first()
        location = night.location if night else None
    else:
        location = Location.objects.filter(pk=request.POST.get("location"), memberships__staff=member).first()
    if not (text and location):
        messages.error(request, "Please write a message.")
        return redirect("my_tips", token=token)
    Message.objects.create(staff=member, location=location, night=night, text=text)
    if night and night.status == Night.Status.REVIEW and not night.hold:
        night.hold = f"{member.name} flagged a problem."
        night.save(update_fields=["hold"])
    messages.success(request, "Sent. Your manager will see it" + (" before the money goes out." if night else "."))
    return redirect("my_tips", token=token)


# --- PayPal webhooks ----------------------------------------------------------
@csrf_exempt
@require_POST
def paypal_webhook(request):
    try:
        event = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("bad json")
    event_id = event.get("id", "")
    if not event_id or WebhookEvent.objects.filter(event_id=event_id).exists():
        return HttpResponse("ok")  # duplicate or junk: acknowledge so PayPal stops retrying

    verified = False
    if settings.PAYPAL_WEBHOOK_ID:
        try:
            verified = get_client().verify_webhook(request.headers, event, settings.PAYPAL_WEBHOOK_ID)
        except PayPalError:
            verified = False
        if not verified:
            log.warning("Rejected unverified webhook %s", event_id)
            return HttpResponse(status=400)
    elif not settings.DEBUG:
        log.error("PAYPAL_WEBHOOK_ID is not set: refusing webhooks in production")
        return HttpResponse(status=400)
    else:
        log.warning("PAYPAL_WEBHOOK_ID not set: webhook %s accepted WITHOUT verification (dev only)", event_id)

    WebhookEvent.objects.create(event_id=event_id, event_type=event.get("event_type", ""),
                                verified=verified, payload=event)
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        if order_id:
            services.mark_capture_completed(order_id, resource.get("id", ""))
    elif event_type.startswith("PAYMENT.PAYOUTS-ITEM."):
        services.apply_payout_item_event(resource)
    elif event_type.startswith("PAYMENT.PAYOUTSBATCH."):
        batch_id = resource.get("batch_header", {}).get("payout_batch_id")
        batch = PayoutBatch.objects.filter(paypal_batch_id=batch_id).first()
        if batch:
            try:
                services.refresh_batch(batch)
            except PayPalError:
                log.exception("Could not refresh batch %s", batch_id)
    return HttpResponse("ok")
