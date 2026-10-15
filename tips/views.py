import io
import json
import logging
from decimal import Decimal, InvalidOperation

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai, autoclose, services
from .models import (CloseTime, Night, PayoutBatch, RestaurantSettings, Role, Shift, StaffMember, Tip,
                     TipRuleSet, WebhookEvent)
from .paypal import PayPalError, get_client

log = logging.getLogger("tips.views")

TIP_PRESETS = [5, 10, 15, 20]


# --- Manager dashboard -------------------------------------------------------
def _sync_tips(night):
    try:
        services.sync_pending_tips(night)
    except Exception:  # never break the page because PayPal is slow
        log.exception("Tip sync failed")


@login_required
def dashboard(request):
    night = Night.current()
    _sync_tips(night)
    tips = list(night.tips.filter(status=Tip.Status.COMPLETED))
    total = night.tip_total
    staff_count = StaffMember.objects.filter(active=True).count()
    rule_set = TipRuleSet.active()
    any_tip_ever = Tip.objects.filter(status=Tip.Status.COMPLETED).exists()
    return render(request, "tips/dashboard.html", {
        "night": night,
        "tips": tips[:40],
        "tip_count": len(tips),
        "total": total,
        "average": (total / len(tips)).quantize(Decimal("0.01")) if tips else None,
        "unfinished": Night.objects.filter(status__in=[Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).first(),
        "next_close": autoclose.next_close(),
        "roster_count": night.shifts.count(),
        "rule_set": rule_set,
        "staff_count": staff_count,
        "setup": [
            {"done": bool(RestaurantSettings.load().name), "text": "Add your restaurant's name", "url": reverse("settings")},
            {"done": staff_count > 0, "text": "Add your team and their PayPal emails", "url": reverse("staff_list")},
            {"done": bool(rule_set), "text": "Describe how your house shares tips", "url": reverse("rules")},
            {"done": any_tip_ever, "text": "Print table QR codes so guests can tip", "url": reverse("qr_page")},
        ],
        "ai_mode": ai.mode(),
        "setup_done": staff_count > 0 and bool(rule_set) and any_tip_ever and bool(RestaurantSettings.load().name),
    })


@login_required
def history(request):
    nights = (Night.objects.exclude(status=Night.Status.OPEN)
              .prefetch_related("shares", "tips").select_related("payout_batch")[:60])
    rows = []
    for n in nights:
        rows.append({"night": n, "total": n.tip_total, "people": sum(1 for s in n.shares.all() if s.amount > 0),
                     "batch": getattr(n, "payout_batch", None)})
    return render(request, "tips/history.html", {"rows": rows})


# --- Customer tip page (public) -----------------------------------------------
def tip_page(request):
    return render(request, "tips/tip_page.html", {
        "cancelled": "cancelled" in request.GET,
        "presets": TIP_PRESETS,
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
    return render(request, "tips/tip_thanks.html", {"tip": tip, "table": tip.table if tip else ""})


@require_POST
def api_create_order(request):
    try:
        data = json.loads(request.body or "{}")
        amount = Decimal(str(data.get("amount", "")))
        table = str(data.get("table", ""))
        order_id = services.create_tip_order(
            amount, table,
            return_url=request.build_absolute_uri(reverse("tip_thanks")),
            cancel_url=request.build_absolute_uri(reverse("tip_page")) + "?cancelled=1",
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
@login_required
def qr_page(request):
    restaurant = RestaurantSettings.load()
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
        messages.success(request, "Table codes updated.")
        return redirect("qr_page")
    base = request.build_absolute_uri(reverse("tip_page"))
    return render(request, "tips/qr_page.html", {
        "restaurant": restaurant,
        "house_url": base,
        "tables": [(f"Table {label}" if label.isdigit() else label, f"{base}?table={label}")
                   for label in restaurant.table_labels],
        "suggestions": RestaurantSettings.SPOT_SUGGESTIONS,
        "custom_spots": [sp for sp in restaurant.spots if sp not in RestaurantSettings.SPOT_SUGGESTIONS],
    })


def qr_svg(request):
    url = request.GET.get("url", "")
    if not url.startswith(request.build_absolute_uri("/")):
        return HttpResponseBadRequest("Only links to this site.")
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return HttpResponse(buffer.getvalue(), content_type="image/svg+xml")


# --- Staff -------------------------------------------------------------------
@login_required
def staff_list(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("paypal_email", "").strip()
        role = request.POST.get("default_role", Role.SERVER)
        if name and email and role in Role.values:
            StaffMember.objects.create(name=name, paypal_email=email, default_role=role)
            messages.success(request, f"{name} added.")
        else:
            messages.error(request, "Name, PayPal email and role are required.")
        return redirect("staff_list")
    return render(request, "tips/staff_list.html", {
        "staff": StaffMember.objects.all(), "roles": Role.choices,
    })


@login_required
@require_POST
def staff_toggle(request, pk):
    member = get_object_or_404(StaffMember, pk=pk)
    member.active = not member.active
    member.save()
    return redirect("staff_list")


# --- Rules -------------------------------------------------------------------
@login_required
def rules_page(request):
    preview = None
    text = ""
    if request.method == "POST" and "save" in request.POST:
        preview = request.session.pop("rules_preview", None)
        if not preview:
            messages.error(request, "Nothing to save, please describe the rules again.")
            return redirect("rules")
        TipRuleSet.objects.filter(is_active=True).update(is_active=False)
        TipRuleSet.objects.create(text=preview["text"], rules=preview["rules"], summary=preview["summary"],
                                  warnings=preview["warnings"])
        messages.success(request, "Tip rules saved.")
        return redirect("rules")
    if request.method == "POST":
        text = request.POST.get("text", "")
        try:
            parsed = ai.parse_rules(text)
            preview = {"text": text, **parsed}
            request.session["rules_preview"] = preview
        except ai.AIError as exc:
            messages.error(request, str(exc))
    active = TipRuleSet.active()
    return render(request, "tips/rules.html", {
        "active": active, "preview": preview, "text": text or (active.text if active else ""),
        "ai_mode": ai.mode(),
        "example": "Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours worked.",
    })


# --- Close the night: who worked -> split -> review -> pay ---------------------
@login_required
def close_night(request, pk=None):
    night = get_object_or_404(Night, pk=pk) if pk else Night.current()
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        return redirect("payout_status", pk=night.pk)
    if night.status == Night.Status.OPEN:
        _sync_tips(night)
    staff = list(StaffMember.objects.filter(active=True))
    existing = {s.staff_id: s for s in night.shifts.all()}
    prefill = {}
    ai_notes = []

    if request.method == "POST" and request.FILES.get("photo"):
        photo = request.FILES["photo"]
        if photo.size > 8 * 1024 * 1024 or photo.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            messages.error(request, "Please upload a JPG, PNG, WEBP or GIF photo under 8 MB.")
        else:
            try:
                result = ai.read_schedule(photo.read(), photo.content_type, [m.name for m in staff])
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
        rule_set = TipRuleSet.active()
        saving_only = "save_roster" in request.POST
        if not rule_set and not saving_only:
            messages.error(request, "Set your tip rules first.")
            return redirect("rules")
        night.shifts.all().delete()
        for member in staff:
            if not request.POST.get(f"worked_{member.pk}"):
                continue
            try:
                hours = Decimal(request.POST.get(f"hours_{member.pk}", "0") or "0")
            except InvalidOperation:
                hours = Decimal("0")
            role = request.POST.get(f"role_{member.pk}", member.default_role)
            if hours > 0 and role in Role.values:
                Shift.objects.create(night=night, staff=member, role=role, hours=hours)
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

    rows = []
    for member in staff:
        shift = existing.get(member.pk)
        guess = prefill.get(member.pk)
        rows.append({
            "member": member,
            # first visit: everyone on (managers switch off who was absent); afterwards keep their choices
            "worked": bool(shift or guess) or (not existing and not prefill),
            "role": (guess or {}).get("role") or (shift.role if shift else member.default_role),
            "hours": (guess or {}).get("hours") or (shift.hours if shift else ""),
        })
    return render(request, "tips/close_night.html", {
        "night": night, "rows": rows, "tip_count": night.tips.filter(status=Tip.Status.COMPLETED).count(), "roles": Role.choices, "rule_set": TipRuleSet.active(),
        "ai_notes": ai_notes, "ai_mode": ai.mode(),
    })


@login_required
def review(request, pk):
    night = get_object_or_404(Night, pk=pk)
    if night.status in (Night.Status.OPEN, Night.Status.CLOSED):
        return redirect("close_night_pk", pk=night.pk)
    shares = list(night.shares.select_related("staff"))
    return render(request, "tips/review.html", {
        "night": night, "shares": shares, "total": night.tip_total,
        "paid_total": sum((s.amount for s in shares), Decimal("0")),
        "paying_count": sum(1 for s in shares if s.amount > 0),
        "rule_set": night.rule_set,
    })


@login_required
@require_POST
def pay(request, pk):
    night = get_object_or_404(Night, pk=pk)
    try:
        services.pay_night(night)
        messages.success(request, "Payout sent to PayPal.")
    except services.TipPoolError as exc:
        messages.error(request, str(exc))
        return redirect("review", pk=pk)
    except PayPalError as exc:
        messages.error(request, f"PayPal refused the payout: {exc} (debug id {exc.debug_id})")
        return redirect("review", pk=pk)
    return redirect("payout_status", pk=pk)


@login_required
def payout_status(request, pk):
    night = get_object_or_404(Night, pk=pk)
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


@login_required
def payout_status_json(request, pk):
    night = get_object_or_404(Night, pk=pk)
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


@login_required
def settings_page(request):
    from datetime import time as dtime
    restaurant = RestaurantSettings.load()
    if request.method == "POST":
        restaurant.name = request.POST.get("name", "").strip()[:80]
        restaurant.auto_close_enabled = bool(request.POST.get("auto_close_enabled"))
        if request.POST.get("after_close") in RestaurantSettings.AfterClose.values:
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
                CloseTime.objects.update_or_create(weekday=day, defaults={"time": value, "enabled": enabled})
            else:
                CloseTime.objects.filter(weekday=day).update(enabled=False)
        messages.success(request, "Close-out settings saved.")
        return redirect("settings")
    existing = {c.weekday: c for c in CloseTime.objects.all()}
    days = [{"n": d, "name": CloseTime.WEEKDAYS[d], "close": existing.get(d)} for d in range(7)]
    return render(request, "tips/settings.html", {
        "restaurant": restaurant, "days": days, "timezones": TIMEZONES,
        "after_choices": RestaurantSettings.AfterClose.choices, "next_close": autoclose.next_close(),
    })


# --- Staff's own page (secret link, no login) ----------------------------------
def my_tips(request, token):
    member = get_object_or_404(StaffMember, access_token=token)
    shares = member.share_set.select_related("night").order_by("-night__date")[:20]
    return render(request, "tips/my_tips.html", {"member": member, "shares": shares})


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
