"""How a location is doing compared with a normal day: used by the live board, smart alerts and the digest.

"Normal" is the median of the same weekday over the last 8 weeks, measured at the same time of day
(so a Friday at 9 pm is compared with other Fridays at 9 pm, not with whole Fridays).
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Sum
from django.utils import timezone

from .autoclose import location_tz
from .models import ClockEntry, Night, Shift, Tip

WEEKS = 8
MIN_SAMPLES = 3
CENT = Decimal("0.01")


def _midnight(day, tz):
    return datetime.combine(day, time(0), tzinfo=tz)


def _hours(entries, now):
    total = timedelta()
    for e in entries:
        total += (e.ended_at or now) - e.started_at
    return Decimal(total.total_seconds() / 3600).quantize(CENT)


def pace(location, now=None):
    """The open shift so far against a normal one at this time of day."""
    now = now or timezone.now()
    tz = location_tz(location)
    night = Night.objects.filter(location=location, status=Night.Status.OPEN).order_by("-date").first()
    day = night.date if night else now.astimezone(tz).date()
    since_midnight = now - _midnight(day, tz)

    out = {"night": night, "now": Decimal("0.00"), "guests": 0, "labor_hours": None, "per_hour": None,
           "normal_now": None, "normal_guests": None, "normal_day": None, "normal_per_hour": None,
           "ratio": None, "status": "new", "samples": 0, "meter_max": Decimal("1")}
    if night:
        tips = list(night.tips.filter(status=Tip.Status.COMPLETED).values_list("amount", "source"))
        out["now"] = sum((a for a, _ in tips), Decimal("0.00"))
        out["guests"] = sum(1 for _, s in tips if s == Tip.Source.PAYPAL)
        entries = list(ClockEntry.objects.filter(night=night))
        if entries:
            out["labor_hours"] = _hours(entries, now)
            if out["labor_hours"] > 0:
                out["per_hour"] = (out["now"] / out["labor_hours"]).quantize(CENT)

    past = list(Night.objects.filter(location=location, date__in=[day - timedelta(weeks=k) for k in range(1, WEEKS + 1)])
                .exclude(status=Night.Status.OPEN))
    if len(past) < MIN_SAMPLES:
        return out
    by_night = {n.pk: [] for n in past}
    for night_id, amount, source, created in Tip.objects.filter(night__in=past, status=Tip.Status.COMPLETED).values_list(
            "night_id", "amount", "source", "created_at"):
        by_night[night_id].append((amount, source, created))
    hours = dict(Shift.objects.filter(night__in=past).values("night_id").annotate(h=Sum("hours"))
                 .values_list("night_id", "h"))
    so_far, guests, totals, rates = [], [], [], []
    for n in past:
        cutoff = _midnight(n.date, tz) + since_midnight
        rows = by_night[n.pk]
        so_far.append(sum((a for a, _, c in rows if c <= cutoff), Decimal("0")))
        guests.append(sum(1 for _, s, c in rows if s == Tip.Source.PAYPAL and c <= cutoff))
        total = sum((a for a, _, _ in rows), Decimal("0"))
        totals.append(total)
        if hours.get(n.pk):
            rates.append(total / hours[n.pk])
    out["samples"] = len(past)
    out["normal_now"] = Decimal(median(so_far)).quantize(CENT)
    out["normal_guests"] = int(median(guests))
    out["normal_day"] = Decimal(median(totals)).quantize(CENT)
    out["normal_per_hour"] = Decimal(median(rates)).quantize(CENT) if rates else None
    out["meter_max"] = max(out["normal_day"], out["now"], Decimal("1"))
    if out["normal_now"] <= 0:
        out["status"] = "early"  # usually nothing yet at this hour
        return out
    ratio = out["now"] / out["normal_now"]
    out["ratio"] = ratio
    out["percent"] = int((ratio - 1) * 100)
    out["status"] = ("above" if ratio >= Decimal("1.15") else "normal" if ratio >= Decimal("0.75")
                     else "below" if ratio >= Decimal("0.4") else "low")
    if out["guests"] == 0 and out["normal_guests"] >= 3:
        out["status"] = "quiet"
    return out


def week_so_far(location, today=None):
    """This week's tips so far and the same days last week."""
    today = today or timezone.now().astimezone(location_tz(location)).date()
    start = today - timedelta(days=today.weekday())

    def total(first, last):
        return (Tip.objects.filter(night__location=location, night__date__gte=first, night__date__lte=last,
                                   status=Tip.Status.COMPLETED).aggregate(s=Sum("amount"))["s"] or Decimal("0"))
    now, before = total(start, today), total(start - timedelta(weeks=1), today - timedelta(weeks=1))
    change = int((now - before) / before * 100) if before else None
    return {"now": now.quantize(CENT), "before": before.quantize(CENT), "change": change}
