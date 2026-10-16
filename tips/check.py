"""The close-out check: before money goes out, look over the shift like a careful manager would.

1. Plain rules find the hard problems (14-hour shift, someone forgot to clock out, an unconfirmed payout
   address, a flagged problem, a refunded tip, a total far off the usual).
2. The AI reads the whole shift, explains what it sees in plain words, suggests fixes, and may add its own
   concerns. It can put the payout on hold. It can never release one: only people and the rules do that.

Automatic payouts only go out when nothing is marked "stop".
"""
import logging
from decimal import Decimal
from statistics import median
from typing import Optional

from pydantic import BaseModel, Field

from . import ai
from .models import Night, Tip

log = logging.getLogger("tips.check")

STOP, WARN = "stop", "warn"
LONG_SHIFT = Decimal("13")


class CheckOut(BaseModel):
    summary: str = Field(description="1-2 plain sentences: is this shift ready to pay, and why or why not")
    suggestions: list[str] = Field(description="Concrete fixes for the manager, short, at most 4")
    extra_concerns: list[str] = Field(description="Anything else odd you notice in the numbers (may be empty)")
    hold: bool = Field(description="True if a careful manager should look before this is paid")
    hold_reason: Optional[str] = Field(default=None, description="Why, in one sentence, when hold is true")


CHECK_SYSTEM = """You check a shift's tip split before the money is paid out to the team.
You get the shift's numbers and the problems simple rules already found. Use ONLY the numbers given.
Write for a busy manager: short, plain, specific (names and amounts). Suggest concrete fixes.
Set hold=true when something looks wrong enough that a person should look before paying
(you can only stop a payout, never approve one). Don't repeat the rules' findings word for word."""


def findings_for(night):
    """The plain-rule findings: [{"level": "stop"|"warn", "text": ...}]."""
    found = []
    business = night.location.business
    cur = business.symbol
    shares = list(night.shares.select_related("staff"))
    shifts = list(night.shifts.select_related("staff"))

    for shift in shifts:
        if shift.hours > LONG_SHIFT:
            found.append({"level": STOP, "text": f"{shift.staff.name} has {shift.hours.normalize():f} hours. "
                                                 "Is that right?"})
    for entry in night.clock_entries.filter(auto_ended=True).select_related("staff"):
        found.append({"level": STOP, "text": f"{entry.staff.name} forgot to clock out, so their hours were "
                                             "cut off at closing time."})
    for msg in night.messages.filter(resolved_at__isnull=True).select_related("staff"):
        found.append({"level": STOP, "text": f"{msg.staff.name} flagged a problem: \"{msg.text[:120]}\""})
    for share in shares:
        if share.amount > 0 and share.staff.payout_confirmed_at is None:
            found.append({"level": STOP, "text": f"{share.staff.name} hasn't confirmed where they're paid "
                                                 f"({share.staff.payout_to})."})
    refunded = night.tips.filter(status=Tip.Status.REFUNDED)
    if refunded.exists():
        found.append({"level": WARN, "text": f"{refunded.count()} tip(s) were refunded to guests and left out."})
    on_roster = {s.staff_id for s in shifts}
    for share in shares:
        if share.amount == 0 and share.hours > 0:
            found.append({"level": WARN, "text": f"{share.staff.name} worked {share.hours.normalize():f}h as "
                                                 f"{share.role_name.lower()} but gets nothing: no rule covers "
                                                 "that role."})
        if share.staff_id not in on_roster and share.amount > 0:
            found.append({"level": WARN, "text": f"{share.staff.name} gets {cur}{share.amount} in personal tips "
                                                 "but isn't on the roster for this shift."})
    paid = [s for s in shares if s.amount > 0]
    total = sum((s.amount for s in paid), Decimal("0"))
    if len(paid) > 2 and total > 0:
        top = max(paid, key=lambda s: s.amount)
        if top.amount / total > Decimal("0.6"):
            found.append({"level": WARN, "text": f"{top.staff.name} gets {cur}{top.amount}, "
                                                 f"{(top.amount / total * 100):.0f}% of everything."})
    usual = _usual_total(night)
    if usual and night.tip_total and (night.tip_total < usual * Decimal("0.35") or night.tip_total > usual * 3):
        found.append({"level": WARN, "text": f"Tips were {cur}{night.tip_total}; a usual shift here is about "
                                             f"{cur}{usual:.2f}. Missing card or cash tips?"})
    return found


def blocking(night):
    """Everything that should stop a payout right now: the rules looked at again (a problem may have been
    flagged since the split), plus any hold the AI asked for."""
    stops = [f for f in findings_for(night) if f["level"] == STOP]
    stops += [f for f in (night.review_check or {}).get("findings", []) if f.get("ai") and f["level"] == STOP]
    return stops


def _usual_total(night):
    from django.db.models import Q, Sum
    previous = (Night.objects.filter(location=night.location, status=Night.Status.PAID).exclude(pk=night.pk)
                .annotate(total=Sum("tips__amount", filter=Q(tips__status=Tip.Status.COMPLETED))).order_by("-date")[:8])
    totals = [n.total for n in previous if n.total and n.total > 0]
    return Decimal(median(totals)) if len(totals) >= 3 else None


def run(night):
    """Check a split shift and store the result on it. Returns the stored dict."""
    found = findings_for(night)
    result = {"findings": found, "suggestions": [], "summary": "", "by": "rules"}
    if ai.mode() == "claude":
        try:
            out = ai._claude_parse(CHECK_SYSTEM, [{"type": "text", "text": _describe(night, found)}], CheckOut,
                                   effort="low")
            result.update(summary=out.summary, suggestions=out.suggestions[:4], by="Claude")
            for concern in out.extra_concerns[:3]:
                found.append({"level": WARN, "text": concern, "ai": True})
            if out.hold:
                found.append({"level": STOP, "text": out.hold_reason or "The AI check wants a person to look first.",
                              "ai": True})
        except ai.AIError as exc:
            log.warning("AI check failed, using the rules only: %s", exc)
    if not result["summary"]:
        stops = [f for f in found if f["level"] == STOP]
        result["summary"] = ("Ready to pay: nothing unusual found." if not found else
                             f"Look at {len(stops)} thing{'s' if len(stops) != 1 else ''} before paying." if stops
                             else "Ready to pay, with a few things worth a look.")
    result["ok"] = not any(f["level"] == STOP for f in found)
    night.review_check = result
    night.save(update_fields=["review_check"])
    return result


def _describe(night, found):
    cur = night.location.business.symbol
    lines = [f"Shift: {night} at {night.location.display_name}. Tips: {cur}{night.tip_total} "
             f"(fees {cur}{night.fee_total}). Rules: {night.rule_set.summary if night.rule_set else 'none'}.",
             "Shares:"]
    hours = {s.staff_id: s.hours for s in night.shifts.all()}
    for s in night.shares.select_related("staff"):
        lines.append(f"- {s.staff.name} ({s.role_name}, {hours.get(s.staff_id, 0)}h): {cur}{s.amount}")
    usual = _usual_total(night)
    if usual:
        lines.append(f"A usual shift here: about {cur}{usual:.2f}.")
    lines.append("Rule findings: " + ("; ".join(f"[{f['level']}] {f['text']}" for f in found) or "none"))
    return "\n".join(lines)
