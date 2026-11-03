"""Rows for the AG Grid tables (Reports, Past shifts, Activity log). The page still renders a plain table, which
works without JavaScript; static/js/grids.js swaps it for a grid built from these rows.

Every value is plain data (numbers to sort by, text to show): the grid puts text on the page as text, never as
HTML, and links are built here from our own URLs."""
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format


def _money(value):
    return float(value) if value is not None else None


def _shift_link(night, batch=None):
    """Where a shift row leads, with the words for it."""
    if batch is not None or night.status in ("paid", "paying"):
        return reverse("payout_status", args=[night.pk]), "Payout details"
    if night.status == "closed":
        return reverse("close_night_pk", args=[night.pk]), "Add who worked"
    return reverse("review", args=[night.pk]), "Check and pay"


def _status(night, batch=None):
    """(key, label) for a shift's status pill, the same words as the plain table."""
    if night.status == "paid" and batch is not None and (batch.paypal_batch_id or "").startswith("DEMO"):
        return "sample", "Sample"
    return {"paid": ("ok", "Paid"), "paying": ("wait", "Paying"), "closed": ("wait", "Needs who worked")}.get(
        night.status, ("live", "Ready to approve"))


def people_rows(report):
    return [{"name": p["name"], "roles": p["roles"], "shifts": p["shifts"], "hours": float(p["hours"]),
             "pool": _money(p["pool"]), "personal": _money(p["personal"]) or None, "total": _money(p["total"]),
             "perHour": _money(p["per_hour"]) if p["per_hour"] else None} for p in report["people"]]


def shift_rows(report):
    rows = []
    for s in report["shifts"]:
        url, action = _shift_link(s["night"])
        rows.append({"date": s["date"].isoformat(), "label": date_format(s["date"], "D, M j"), "short": date_format(s["date"], "M j"),
                     "location": s["location"],
                     "total": _money(s["total"]), "people": s["people"], "status": s["status"], "url": url,
                     "action": action})
    return rows


def history_rows(rows):
    out = []
    for row in rows:
        night, batch = row["night"], row["batch"]
        url, action = _shift_link(night, batch)
        key, label = _status(night, batch)
        out.append({"date": night.date.isoformat(), "label": date_format(night.date, "D, M j"), "short": date_format(night.date, "M j"),
                    "total": _money(row["total"]), "people": row["people"], "statusKey": key, "status": label,
                    "url": url, "action": action})
    return out


def activity_rows(changes, several_locations):
    rows = []
    for c in changes:
        at = timezone.localtime(c.at)
        rows.append({"at": at.isoformat(), "when": date_format(at, "M j, g:i A"), "short": date_format(at, "M j, g:i"),
                     "who": c.who, "what": c.what,
                     **({"location": c.location.display_name if c.location else ""} if several_locations else {})})
    return rows
