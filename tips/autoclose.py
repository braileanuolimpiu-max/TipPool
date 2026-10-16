"""Automatic close-out at a set time each day.

At closing time a location's open shift stops taking tips (new tips start a fresh night), and:
- if the manager saved who worked: the split is calculated, and paid right away
  when the location chose "pay the team automatically"
- if not: the night waits as "closed, needs who worked" so nobody is paid by guesswork

Runs from `python manage.py autoclose` (a cron job every few minutes when hosted)
and is also checked whenever a manager opens a page.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from . import check, hours, services
from .alerts import raise_alert
from .models import Change, CloseTime, Location, Night, TipRuleSet
from .paypal import PayPalError

log = logging.getLogger("tips.autoclose")


def location_tz(location):
    try:
        return ZoneInfo(location.timezone)
    except Exception:
        return ZoneInfo("UTC")


def close_due_at(night, tz=None):
    """When this night should close automatically, or None if there's no close time that day."""
    tz = tz or location_tz(night.location)
    close = CloseTime.objects.filter(location_id=night.location_id, weekday=night.date.weekday(), enabled=True).first()
    if not close:
        return None
    day = night.date + timedelta(days=1) if close.after_midnight else night.date
    return datetime.combine(day, close.time, tzinfo=tz)


def next_close(location, now=None):
    """The next scheduled close-out at this location (for the settings page)."""
    if not location.auto_close_enabled:
        return None
    tz = location_tz(location)
    now = (now or timezone.now()).astimezone(tz)
    times = {c.weekday: c for c in CloseTime.objects.filter(location=location, enabled=True)}
    for offset in range(-1, 8):
        service_day = now.date() + timedelta(days=offset)
        close = times.get(service_day.weekday())
        if not close:
            continue
        day = service_day + timedelta(days=1) if close.after_midnight else service_day
        at = datetime.combine(day, close.time, tzinfo=tz)
        if at > now:
            return at
    return None


def run(now=None):
    """Close every open shift whose closing time has passed, at every location. Returns what happened."""
    now = now or timezone.now()
    results = []
    for location in Location.objects.filter(auto_close_enabled=True):
        tz = location_tz(location)
        done = []
        for night in Night.objects.filter(location=location, status=Night.Status.OPEN):
            due = close_due_at(night, tz)
            if due and now >= due:
                done.append(close_night(night, location))
        done += pay_after_review(location, now)
        for what in done:
            Change.log("Automatic close-out", what, location=location)
        results += done
    return results


def pay_after_review(location, now):
    """Automatic payouts that waited for the review window: pay them unless someone flagged a problem."""
    if location.after_close != Location.AfterClose.PAY or not location.review_minutes:
        return []
    results = []
    waiting = Night.objects.filter(location=location, status=Night.Status.REVIEW, auto_closed=True, hold="",
                                   closed_at__lte=now - timedelta(minutes=location.review_minutes))
    for night in waiting:
        stops = check.blocking(night)
        if stops:
            hold(night, stops[0]["text"])
            results.append(f"{night}: held, {stops[0]['text']}")
            continue
        try:
            services.pay_night(night)
            results.append(f"{night}: paid after the review window")
        except (services.TipPoolError, PayPalError) as exc:
            log.warning("Auto payout failed for %s: %s", night, exc)
            hold(night, f"The automatic payout didn't go out: {exc}")
            results.append(f"{night}: payout needs attention ({exc})")
    return results


def hold(night, reason):
    """Stop the automatic payout of a shift and tell the managers why."""
    night.hold = reason[:200]
    night.save(update_fields=["hold"])
    raise_alert(night.location.business, f"The {night} at {night.location.display_name} is waiting for you: {reason}",
                location=night.location, night=night, key=f"hold:{night.pk}")


def close_night(night, location):
    with transaction.atomic():
        night = Night.objects.select_for_update().get(pk=night.pk)
        if night.status != Night.Status.OPEN:
            return f"{night}: already closed"
        night.status = Night.Status.CLOSED
        night.closed_at = timezone.now()
        night.auto_closed = True
        night.save()

    hours.finish_running_entries(night, at=night.closed_at)
    needs_check = False
    if not night.shifts.exists():
        # nothing saved: use clock-ins (and the usual week, if the location allows it)
        added, needs_check = hours.fill_roster(night, location.use_usual_schedule)
        if not added:
            log.info("%s closed automatically; waiting for who worked", night)
            return f"{night}: closed, waiting for who worked"
    if night.tip_total <= 0:
        return f"{night}: closed, no tips"

    rule_set = TipRuleSet.active(location)
    if not rule_set:
        return f"{night}: closed, no tip rules yet"
    try:
        services.build_split(night, rule_set)
    except services.TipPoolError as exc:
        log.warning("Auto split failed for %s: %s", night, exc)
        hold(night, f"The split couldn't be calculated: {exc}")
        return f"{night}: closed, split needs attention ({exc})"

    night.refresh_from_db()
    if not night.review_check.get("ok", True):
        stops = [f["text"] for f in night.review_check.get("findings", []) if f["level"] == check.STOP]
        hold(night, " ".join(stops) or night.review_check.get("summary", "The close-out check found something."))
        return f"{night}: closed and split; the check wants a person to look before paying"
    if needs_check:
        hold(night, "Someone forgot to clock out. Check their hours.")
        return f"{night}: closed and split; someone forgot to clock out, so check the hours before paying"
    if location.after_close == Location.AfterClose.PAY and location.review_minutes:
        return f"{night}: closed and split; pays in {location.review_minutes} minutes unless someone flags a problem"
    if location.after_close == Location.AfterClose.PAY:
        try:
            services.pay_night(night)
            return f"{night}: closed, split and paid automatically"
        except (services.TipPoolError, PayPalError) as exc:
            log.warning("Auto payout failed for %s: %s", night, exc)
            hold(night, f"The automatic payout didn't go out: {exc}")
            return f"{night}: closed and split, payout needs attention ({exc})"
    return f"{night}: closed, split ready to approve"
