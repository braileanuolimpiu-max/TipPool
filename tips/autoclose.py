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
from .models import Change, CloseTime, Location, Night, Tip, TipRuleSet
from .paypal import PayPalError

log = logging.getLogger("tips.autoclose")


def location_tz(location):
    try:
        return ZoneInfo(location.timezone)
    except Exception:
        return ZoneInfo("UTC")


def service_window(location, at=None):
    """(service day, closing time) of the shift that is running at `at`.

    A shift belongs to the next closing time after it started: opened at 23:30 on Friday after a
    23:00 close, it is Saturday's shift; opened on a day with no close time, it runs to the next one.
    Without any close times: today's date and no closing time."""
    tz = location_tz(location)
    at = (at or timezone.now()).astimezone(tz)
    times = {c.weekday: c for c in CloseTime.objects.filter(location=location, enabled=True)}
    for offset in range(-1, 8):
        service_day = at.date() + timedelta(days=offset)
        close = times.get(service_day.weekday())
        if not close:
            continue
        day = service_day + timedelta(days=1) if close.after_midnight else service_day
        closes = datetime.combine(day, close.time, tzinfo=tz)
        if closes > at:
            return service_day, closes
    return at.date(), None


def close_due_at(night, tz=None):
    """When this shift closes automatically. Its date is its service day: that day's closing time, or,
    if that day has none (closed on Mondays), the next day that has one. None without any close times."""
    tz = tz or location_tz(night.location)
    times = {c.weekday: c for c in CloseTime.objects.filter(location_id=night.location_id, enabled=True)}
    for offset in range(0, 7):
        service_day = night.date + timedelta(days=offset)
        close = times.get(service_day.weekday())
        if close:
            day = service_day + timedelta(days=1) if close.after_midnight else service_day
            return datetime.combine(day, close.time, tzinfo=tz)
    return None


def next_close(location, now=None):
    """The next scheduled close-out at this location (for the settings page)."""
    if not location.auto_close_enabled:
        return None
    return service_window(location, now)[1]


def run(now=None):
    """Close every open shift whose closing time has passed, at every location. Returns what happened."""
    now = now or timezone.now()
    results = []
    for location in Location.objects.filter(auto_close_enabled=True, archived=False):
        try:  # one location's problem must never stop the others
            done = _run_location(location, now)
        except Exception:
            log.exception("Automatic close-out failed at %s", location)
            continue
        for what in done:
            Change.log("Automatic close-out", what, location=location)
        results += done
    return results


def _run_location(location, now):
    tz = location_tz(location)
    done = []
    for night in Night.objects.filter(location=location, status=Night.Status.OPEN):
        due = close_due_at(night, tz)
        if due and now >= due:
            done.append(close_night(night, location))
    # a tip that arrived late sent an automatic shift back to "closed": split it again
    for night in Night.objects.filter(location=location, status=Night.Status.CLOSED, auto_closed=True, hold="",
                                      shifts__isnull=False).distinct():
        if night.tip_total > 0:
            done.append(split_and_pay(night, location, needs_check=False))
    done += pay_after_review(location, now)
    return done


def pay_after_review(location, now):
    """Automatic payouts that waited for the review window: pay them unless someone flagged a problem."""
    if location.after_close != Location.AfterClose.PAY or not location.review_minutes:
        return []
    results = []
    waiting = Night.objects.filter(location=location, status=Night.Status.REVIEW, auto_closed=True, hold="",
                                   closed_at__lte=now - timedelta(minutes=location.review_minutes))
    if location.business.payouts_frozen:
        return []  # the owner stopped payouts: they wait, and go out by themselves once unfrozen
    for night in waiting:
        stops = check.blocking(night)
        if stops:
            hold(night, stops[0]["text"])
            results.append(f"{night}: held, {stops[0]['text']}")
            continue
        try:
            services.pay_night(night)
            results.append(f"{night}: paid after the review window")
        except services.PayoutsFrozen:
            break
        except services.NeedsOwner as exc:
            results.append(f"{night}: {exc}")
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
    try:
        services.sync_pending_tips(night)  # tips a guest approved just before closing still count
    except Exception:
        log.warning("Could not check pending tips before closing %s", night)
    with transaction.atomic():
        night = Night.objects.select_for_update().get(pk=night.pk)
        nothing = (night.status == Night.Status.OPEN and not night.tips.exists() and not night.shifts.exists()
                   and not night.clock_entries.exists())
        if nothing:
            night.delete()
            return f"{night}: nothing happened, removed"
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
    if night.tip_total <= 0 and not night.tips.filter(status=Tip.Status.CREATED).exists():
        # no tips at all: nothing to share, so the shift is simply done (hours stay on record)
        Night.objects.filter(pk=night.pk).update(status=Night.Status.PAID)
        return f"{night}: closed with no tips, nothing to pay"
    if not night.shifts.exists():
        log.info("%s closed automatically; waiting for who worked", night)
        return f"{night}: closed, waiting for who worked"
    if night.tip_total <= 0:
        return f"{night}: closed, a guest's tip is still being confirmed"
    return split_and_pay(night, location, needs_check)


def split_and_pay(night, location, needs_check):
    """After an automatic close: split with the location's rules, check it, and pay when the location
    pays automatically (right away, or after its review window)."""
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
        except services.PayoutsFrozen:
            return f"{night}: closed and split; payouts are frozen, so it waits"
        except services.NeedsOwner as exc:
            return f"{night}: closed and split; {exc}"
        except (services.TipPoolError, PayPalError) as exc:
            log.warning("Auto payout failed for %s: %s", night, exc)
            hold(night, f"The automatic payout didn't go out: {exc}")
            return f"{night}: closed and split, payout needs attention ({exc})"
    return f"{night}: closed, split ready to approve"
