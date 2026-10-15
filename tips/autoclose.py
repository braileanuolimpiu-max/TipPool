"""Automatic close-out at a set time each day.

At closing time the open night stops taking tips (new tips start a fresh night), and:
- if the manager saved who worked: the split is calculated, and paid right away
  when the restaurant chose "pay the team automatically"
- if not: the night waits as "closed, needs who worked" so nobody is paid by guesswork

Runs from `python manage.py autoclose` (a cron job every few minutes when hosted)
and is also checked whenever a manager opens a page.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from . import services
from .models import CloseTime, Night, RestaurantSettings
from .paypal import PayPalError

log = logging.getLogger("tips.autoclose")


def restaurant_tz(settings_obj=None):
    settings_obj = settings_obj or RestaurantSettings.load()
    try:
        return ZoneInfo(settings_obj.timezone)
    except Exception:
        return ZoneInfo("UTC")


def close_due_at(night, tz=None):
    """When this night should close automatically, or None if there's no close time that day."""
    tz = tz or restaurant_tz()
    close = CloseTime.objects.filter(weekday=night.date.weekday(), enabled=True).first()
    if not close:
        return None
    day = night.date + timedelta(days=1) if close.after_midnight else night.date
    return datetime.combine(day, close.time, tzinfo=tz)


def next_close(now=None):
    """The next scheduled close-out (for the settings page)."""
    settings_obj = RestaurantSettings.load()
    if not settings_obj.auto_close_enabled:
        return None
    tz = restaurant_tz(settings_obj)
    now = (now or timezone.now()).astimezone(tz)
    times = {c.weekday: c for c in CloseTime.objects.filter(enabled=True)}
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
    """Close every open night whose closing time has passed. Returns a list of what happened."""
    settings_obj = RestaurantSettings.load()
    if not settings_obj.auto_close_enabled:
        return []
    tz = restaurant_tz(settings_obj)
    now = now or timezone.now()
    results = []
    for night in Night.objects.filter(status=Night.Status.OPEN):
        due = close_due_at(night, tz)
        if due and now >= due:
            results.append(close_night(night, settings_obj))
    return results


def close_night(night, settings_obj):
    with transaction.atomic():
        night = Night.objects.select_for_update().get(pk=night.pk)
        if night.status != Night.Status.OPEN:
            return f"{night}: already closed"
        night.status = Night.Status.CLOSED
        night.closed_at = timezone.now()
        night.auto_closed = True
        night.save()

    if not night.shifts.exists():
        log.info("%s closed automatically; waiting for who worked", night)
        return f"{night}: closed, waiting for who worked"
    if night.tip_total <= 0:
        return f"{night}: closed, no tips"

    from .models import TipRuleSet
    rule_set = TipRuleSet.active()
    if not rule_set:
        return f"{night}: closed, no tip rules yet"
    try:
        services.build_split(night, rule_set)
    except services.TipPoolError as exc:
        log.warning("Auto split failed for %s: %s", night, exc)
        return f"{night}: closed, split needs attention ({exc})"

    if settings_obj.after_close == RestaurantSettings.AfterClose.PAY:
        try:
            services.pay_night(night)
            return f"{night}: closed, split and paid automatically"
        except (services.TipPoolError, PayPalError) as exc:
            log.warning("Auto payout failed for %s: %s", night, exc)
            return f"{night}: closed and split, payout needs attention ({exc})"
    return f"{night}: closed, split ready to approve"
