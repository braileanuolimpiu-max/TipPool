"""Who worked and for how long, without the manager typing it.

Two sources, best first:
1. clock-ins: staff tap "Clock in" / "Clock out" on their own tips page
2. the usual week: each person's normal hours per weekday (Team page)

Hours the manager saved on the close-out page always win over both.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import ClockEntry, Membership, Night, Shift, StaffMember


class ClockError(Exception):
    pass


def running_entry(staff):
    return ClockEntry.objects.filter(staff=staff, ended_at__isnull=True).select_related("night__location").first()


def clock_in(staff, location):
    if not Membership.objects.filter(staff=staff, location=location, active=True).exists():
        raise ClockError("You're not on this location's team.")
    with transaction.atomic():
        # locked: two taps at once can't both start an entry (and count the hours twice)
        StaffMember.objects.select_for_update().get(pk=staff.pk)
        running = running_entry(staff)
        if running:
            raise ClockError(f"You're already clocked in at {running.night.location.display_name}.")
        return ClockEntry.objects.create(staff=staff, night=Night.current(location))


def clock_out(staff, at=None):
    entry = running_entry(staff)
    if entry is None:
        raise ClockError("You're not clocked in.")
    entry.ended_at = at or timezone.now()
    entry.save(update_fields=["ended_at"])
    return entry


def clock_hours(night, until=None):
    """{staff_id: {"hours": Decimal, "running": bool, "auto_ended": bool}} from this shift's clock-ins."""
    out = defaultdict(lambda: {"hours": Decimal("0"), "running": False, "auto_ended": False})
    for entry in night.clock_entries.all():
        row = out[entry.staff_id]
        row["hours"] += entry.hours(until)
        row["running"] |= entry.running
        row["auto_ended"] |= entry.auto_ended
    return dict(out)


def suggested_roster(night):
    """Best guess of who worked this shift and for how long, per staff id:
    {"hours": Decimal, "role": key, "source": "clock" | "usual", "running": bool, "auto_ended": bool}."""
    memberships = {m.staff_id: m for m in Membership.objects.filter(location_id=night.location_id, active=True)}
    clocked = clock_hours(night)
    weekday = night.date.weekday()
    out = {}
    for staff_id, member in memberships.items():
        clock = clocked.get(staff_id)
        if clock and (clock["hours"] > 0 or clock["running"]):
            out[staff_id] = {"hours": clock["hours"], "role": member.default_role, "source": "clock",
                             "running": clock["running"], "auto_ended": clock["auto_ended"]}
        elif member.usual_hours(weekday) > 0:
            out[staff_id] = {"hours": member.usual_hours(weekday), "role": member.default_role, "source": "usual",
                             "running": False, "auto_ended": False}
    return out


def finish_running_entries(night, at=None, mark_forgotten=True):
    """At close-out: clock out whoever forgot. Returns how many had to be clocked out.
    `mark_forgotten`: flag those hours for a check (not when the manager typed everyone's hours)."""
    at = at or timezone.now()
    forgot = night.clock_entries.filter(ended_at__isnull=True)
    count = forgot.count()
    forgot.update(ended_at=at, auto_ended=mark_forgotten)
    return count


@transaction.atomic
def fill_roster(night, use_usual):
    """For an automatic close-out with nothing saved: build the roster from clock-ins
    (and the usual week, when the location allows it). Returns (people added, needs_check)."""
    if night.shifts.exists():
        return 0, False
    suggestions = suggested_roster(night)
    clocked = {k: v for k, v in suggestions.items() if v["source"] == "clock"}
    chosen = clocked or ({k: v for k, v in suggestions.items() if v["source"] == "usual"} if use_usual else {})
    too_long = False
    for staff_id, row in chosen.items():
        if row["hours"] > 0:
            hours = min(row["hours"], Decimal("24"))  # a clock-in left running for days isn't a shift
            too_long |= hours < row["hours"]
            Shift.objects.create(night=night, staff_id=staff_id, role=row["role"], hours=hours)
    needs_check = too_long or any(row["auto_ended"] or row["running"] for row in chosen.values())
    return night.shifts.count(), needs_check
