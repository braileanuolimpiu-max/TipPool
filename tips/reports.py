"""Numbers for the Reports page, the CSV exports and the "Ask about your tips" AI.

Everything here is plain sums over closed shifts. No AI, no guessing.
"""
import csv
import io
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum

from .models import Location, Night, Role, Share, Tip

ZERO = Decimal("0.00")

PERIODS = [
    ("this_week", "This week"), ("last_week", "Last week"), ("this_month", "This month"),
    ("last_month", "Last month"), ("last_30", "Last 30 days"), ("this_year", "This year"), ("custom", "Custom"),
]


def period_range(period, today, start=None, end=None):
    """(first day, last day) for a period name. Weeks start on Monday."""
    if period == "this_week":
        first = today - timedelta(days=today.weekday())
        return first, today
    if period == "last_week":
        first = today - timedelta(days=today.weekday() + 7)
        return first, first + timedelta(days=6)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if period == "this_year":
        return today.replace(month=1, day=1), today
    if period == "custom" and start and end:
        return (start, end) if start <= end else (end, start)
    return today - timedelta(days=29), today  # last 30 days


def build(locations, first, last):
    """All the report numbers for these locations between two dates (by shift date, closed shifts only)."""
    nights = list(Night.objects.filter(location__in=locations, date__gte=first, date__lte=last)
                  .exclude(status=Night.Status.OPEN).select_related("location").order_by("date", "created_at"))
    night_ids = [n.pk for n in nights]
    role_names = {(r.location_id, r.key): r.name for r in Role.objects.filter(location__in=locations)}

    by_source = {s: ZERO for s, _ in Tip.Source.choices}
    for row in (Tip.objects.filter(night_id__in=night_ids, status=Tip.Status.COMPLETED)
                .values("source").annotate(total=Sum("amount"))):
        by_source[row["source"]] = row["total"].quantize(ZERO)

    tips_per_night = defaultdict(lambda: ZERO)
    for row in (Tip.objects.filter(night_id__in=night_ids, status=Tip.Status.COMPLETED)
                .values("night_id").annotate(total=Sum("amount"))):
        tips_per_night[row["night_id"]] = row["total"].quantize(ZERO)

    people = {}
    roles = {}
    shifts_people = defaultdict(int)
    paid_out = ZERO
    for share in Share.objects.filter(night_id__in=night_ids).select_related("staff", "night"):
        p = people.setdefault(share.staff_id, {
            "name": share.staff.name, "roles": set(), "shifts": 0, "hours": ZERO, "pool": ZERO,
            "personal": ZERO, "total": ZERO, "paid": ZERO, "locations": set(),
        })
        personal = sum((Decimal(b["amount"]) for b in share.breakdown if b.get("personal")), ZERO)
        p["personal"] += personal
        p["pool"] += share.amount - personal
        p["total"] += share.amount
        p["hours"] += share.hours
        if share.hours:
            p["shifts"] += 1
        role_name = role_names.get((share.night.location_id, share.role), share.role) if share.role else ""
        if role_name:
            p["roles"].add(role_name)
            r = roles.setdefault(role_name, {"name": role_name, "people": set(), "hours": ZERO, "total": ZERO})
            r["people"].add(share.staff_id)
            r["hours"] += share.hours
            r["total"] += share.amount
        p["locations"].add(share.night.location_id)
        if share.night.status == Night.Status.PAID:
            p["paid"] += share.amount
            paid_out += share.amount
        if share.amount > 0:
            shifts_people[share.night_id] += 1

    people_rows = []
    for p in people.values():
        p["per_hour"] = (p["pool"] / p["hours"]).quantize(ZERO) if p["hours"] else None
        p["roles"] = ", ".join(sorted(p["roles"]))
        people_rows.append(p)
    people_rows.sort(key=lambda p: (-p["total"], p["name"]))

    role_rows = sorted(({**r, "people": len(r["people"]),
                         "per_hour": (r["total"] / r["hours"]).quantize(ZERO) if r["hours"] else None}
                        for r in roles.values()), key=lambda r: -r["total"])

    shift_rows = [{
        "night": n, "date": n.date, "location": n.location.display_name, "total": tips_per_night[n.pk],
        "people": shifts_people[n.pk], "status": n.get_status_display(),
    } for n in nights]

    collected = sum(by_source.values(), ZERO)
    return {
        "first": first, "last": last, "collected": collected, "paid_out": paid_out,
        "by_source": [(label, by_source[key]) for key, label in Tip.Source.choices],
        "shift_count": len(nights),
        "per_shift": (collected / len(nights)).quantize(ZERO) if nights else None,
        "people": people_rows, "shifts": shift_rows, "roles": role_rows,
        "several_locations": len({n.location_id for n in nights}) > 1,
    }


def people_csv(report):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Roles", "Shifts", "Hours", "Shared tips", "Personal tips", "Total", "Paid", "Shared per hour"])
    for p in report["people"]:
        w.writerow([p["name"], p["roles"], p["shifts"], p["hours"], p["pool"], p["personal"], p["total"], p["paid"],
                    p["per_hour"] if p["per_hour"] is not None else ""])
    return out.getvalue()


def shifts_csv(report):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Location", "Tips", "People paid", "Status"])
    for s in report["shifts"]:
        w.writerow([s["date"].isoformat(), s["location"], s["total"], s["people"], s["status"]])
    return out.getvalue()


def scope_locations(request_location, scope):
    return list(Location.objects.all()) if scope == "all" else [request_location]
