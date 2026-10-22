"""Things you can ask TipPool to do: "pay Friday's shift", "set Maria to Mon-Fri 6h", "add $40 cash tips",
"close out tonight", "fix the payouts that didn't arrive", or several at once ("close yesterday, add $40 cash
and pay everyone"), confirmed once and done in order.

The AI (or the offline reader) only *proposes* an action. The manager sees exactly what will happen and
confirms it. The proposal is signed (it can't be edited in the browser), tied to that manager and location,
and expires after 10 minutes. On confirm everything is checked again, through the same rules as the buttons.
"""
import calendar
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core import signing

from .models import Membership, Night, Tip

SALT = "tippool-ask-action"
MAX_AGE = 600
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


KINDS = ("pay_shift", "set_week", "add_tip", "close_shift", "fix_payouts")
# "close out tonight", "please close yesterday", "..., then close": asked to do it, not "when do we close?"
CLOSE_ASKED = r"(?:^|please\s+|and\s+|then\s+|,\s*)close(?:\s+out)?\b(?!\s+(?:at|time)\b)"
MAX_STEPS = 4


class ActionError(Exception):
    pass


class PartlyDone(Exception):
    """A plan stopped part of the way: the first steps happened (and can't be confirmed again)."""


# --- reading a request in plain words (offline) ---------------------------------------
def parse(question, today):
    """('pay_shift'|'set_week'|'add_tip', params) or None."""
    q = question.lower().replace("–", "-").replace("—", "-")
    if re.search(r"\bpay\b", q) and re.search(r"\b(shift|team|night|everyone)\b", q):
        day = _date_in(q, today)
        return "pay_shift", {"shift_date": day.isoformat() if day else None}
    m = re.search(r"\badd\s+[$€£]?\s*(\d+(?:\.\d{1,2})?)\s*(?:in\s+|of\s+)?(cash|card)", q)
    if m:
        day = _date_in(q, today)
        return "add_tip", {"amount": m.group(1), "source": m.group(2),
                           "shift_date": day.isoformat() if day else None}
    week = _week_in(q)
    m = re.search(r"\b(?:set|put|make|schedule)\s+([a-z][a-z'-]+)", q)
    if week and m:
        return "set_week", {"person": m.group(1), "week": week}
    return None


def parse_plan(question, today):
    """Several things in one request, in the order written: [(kind, params), ...] (2 or more), or None."""
    q = question.lower().replace("–", "-").replace("—", "-")
    day = _date_in(q, today)
    found = []
    m = re.search(CLOSE_ASKED, q.strip())
    if m:
        found.append((m.start(), "close_shift", {"shift_date": day.isoformat() if day else None}))
    m = re.search(r"\badd\s+[$€£]?\s*(\d+(?:\.\d{1,2})?)\s*(?:in\s+|of\s+)?(cash|card)", q)
    if m:
        found.append((m.start(), "add_tip", {"amount": m.group(1), "source": m.group(2),
                                              "shift_date": day.isoformat() if day else None}))
    m = re.search(r"\bpay\b", q)
    if m and not re.search(r"\bpayouts?\b", q[m.start():m.start() + 7]):
        found.append((m.start(), "pay_shift", {"shift_date": day.isoformat() if day else None}))
    m = re.search(r"\b(fix|resend|send again|remind)\b.*\bpayouts?\b|\bpayouts?\b.*\b(didn.?t|not|never) (arrive|go)", q)
    if m:
        found.append((m.start(), "fix_payouts", {}))
    if len(found) < 2:
        return None
    return [(kind, params) for _, kind, params in sorted(found)]


def _date_in(q, today):
    if "today" in q or "tonight" in q:
        return today
    if "yesterday" in q or "last night" in q:
        return today - timedelta(days=1)
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", q)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    months = {n.lower(): i for i, n in enumerate(calendar.month_abbr) if n}
    m = re.search(r"\b(" + "|".join(months) + r")[a-z]*\.?\s+(\d{1,2})\b", q) or \
        re.search(r"\b(\d{1,2})\s+(" + "|".join(months) + r")[a-z]*\b", q)
    if m:
        a, b = m.groups()
        month, day = (months[a[:3]], int(b)) if a[:3] in months else (months[b[:3]], int(a))
        year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for i, name in enumerate(calendar.day_name):
        if re.search(rf"\b{name.lower()}|\b{DAYS[i]}\b", q):
            return today - timedelta(days=(today.weekday() - i) % 7)
    return None


def _week_in(q):
    """'mon-fri 6h, sat 8h' -> {"0": 6, ..., "4": 6, "5": 8}"""
    week = {}
    day = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*"
    for m in re.finditer(rf"\b{day}(?:\s*(?:-|to|through)\s*{day})?\s*(?:at\s+|for\s+)?(\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?)\b", q):
        first, last, hours = DAYS.index(m.group(1)), DAYS.index(m.group(2) or m.group(1)), float(m.group(3))
        if not 0 < hours <= 24:
            continue
        span = range(first, last + 1) if first <= last else list(range(first, 7)) + list(range(0, last + 1))
        for d in span:
            week[str(d)] = hours
    return week


# --- proposing and doing --------------------------------------------------------------
def propose(kind, params, location, user):
    """Check the action can be done and describe it. Returns {"text", "token"}; raises ActionError."""
    cur = location.business.symbol
    if kind == "pay_shift":
        night = _night_to_pay(location, params.get("shift_date"))
        people = night.shares.filter(amount__gt=0).count()
        total = sum((s.amount for s in night.shares.all()), Decimal("0"))
        from .check import blocking
        stops = blocking(night)
        if stops:
            raise ActionError(f"The {night} isn't ready to pay: {stops[0]['text']} Open it to check.")
        business = location.business
        if business.payouts_frozen:
            raise ActionError(f"Payouts are frozen by {business.payouts_frozen_by or 'the owner'}, so I can't pay the "
                              f"{night} now.")
        limit = business.approval_limit
        is_owner = getattr(getattr(user, "manager", None), "is_owner", False)
        if limit is not None and total > limit and not is_owner:
            raise ActionError(f"The {night} is {cur}{total}, over the owner's {cur}{limit} limit, so the owner has to "
                              "pay it. Open it to ask them.")
        text = f"Pay the {night}: {cur}{total} to {people} {'person' if people == 1 else 'people'} through PayPal."
        params = {"night": night.pk}
    elif kind == "set_week":
        member = _member(location, params.get("person"))
        week = {k: float(v) for k, v in (params.get("week") or {}).items() if k in {str(i) for i in range(7)}
                and 0 < float(v) <= 24}
        if not week:
            raise ActionError("Tell me the days and hours, e.g. \"set Maria to Mon-Fri 6h\".")
        member.schedule = week
        text = f"Set {member.staff.name}'s usual week to {member.schedule_summary}."
        params = {"membership": member.pk, "week": week}
    elif kind == "add_tip":
        try:
            amount = Decimal(str(params.get("amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise ActionError("How much? e.g. \"add $40 cash tips\".")
        if amount <= 0:
            raise ActionError("The amount has to be above 0.")
        source = params.get("source") if params.get("source") in (Tip.Source.CARD, Tip.Source.CASH) else None
        if source is None:
            raise ActionError("Card or cash?")
        night = _night_for_tips(location, params.get("shift_date"))
        text = f"Add {cur}{amount} {'card machine' if source == 'card' else 'cash'} tips to the {night}, shared by the team."
        params = {"night": night.pk, "amount": str(amount), "source": source}
    elif kind == "close_shift":
        night = _night_to_close(location, params.get("shift_date"))
        tips = night.tip_total
        text = (f"Close out the {night}: who worked from clock-ins{' and usual hours' if location.use_usual_schedule else ''}"
                f", then split {cur}{tips} by your rules and run the close-out check. Nothing is paid yet.")
        params = {"night": night.pk}
    elif kind == "fix_payouts":
        failed, unclaimed = _payouts_to_fix(location)
        if not failed and not unclaimed:
            raise ActionError("Every payout here arrived. There's nothing to fix.")
        parts = []
        if failed:
            parts.append("send again " + ", ".join(f"{i.share.staff.name}'s {cur}{i.share.amount}" for i in failed)
                         + " to where they're paid now")
        if unclaimed:
            parts.append("remind " + ", ".join(i.share.staff.name for i in unclaimed) + " to accept their payout")
        text = "Fix payouts: " + "; ".join(parts) + "."
        params = {"failed": [i.pk for i in failed], "unclaimed": [i.pk for i in unclaimed]}
    else:
        raise ActionError("I can't do that one.")
    token = signing.dumps({"k": kind, "p": params, "u": user.pk, "l": location.pk, "n": secrets.token_hex(16)},
                          salt=SALT, compress=True)
    return {"text": text, "token": token}


def propose_plan(steps, location, user):
    """Several actions confirmed once, done in order. Each step is described now and checked again when it
    runs; a step that depends on an earlier one (pay after close) is checked when its turn comes.
    Returns {"text", "steps", "token"}; raises ActionError when the plan can't start."""
    steps = [(k, p or {}) for k, p in steps if k in KINDS][:MAX_STEPS]
    if len(steps) < 2:
        raise ActionError("That's one thing: ask it on its own.")
    if sum(1 for k, _ in steps if k == "pay_shift") > 1:
        raise ActionError("One payout per request, please: ask for each shift separately.")
    texts, planned, changed_dates = [], [], set()
    for i, (kind, params) in enumerate(steps):
        day = params.get("shift_date")
        if kind == "pay_shift" and (day in changed_dates or (day is None and changed_dates)):
            # it's split (or split again) by an earlier step: the amount is known, and checked, only then
            texts.append("Then pay that shift through PayPal, only if the close-out check finds nothing and it's "
                         "within your payout limits (the amount is the split from the step before).")
            if day is None and len(changed_dates) == 1:
                day = next(iter(changed_dates))
            planned.append(("pay_shift", {"shift_date": day, "after_change": True}))
            continue
        try:
            proposal = propose(kind, params, location, user)
        except ActionError as exc:
            if i == 0:
                raise
            raise ActionError(f"Step {i + 1} can't be done: {exc}")
        texts.append(proposal["text"])
        data = signing.loads(proposal["token"], salt=SALT)
        planned.append((kind, data["p"]))
        if kind in ("close_shift", "add_tip"):
            changed_dates.add(day)
    token = signing.dumps({"k": "plan", "p": {"steps": planned}, "u": user.pk, "l": location.pk,
                           "n": secrets.token_hex(16)}, salt=SALT, compress=True)
    return {"text": f"{len(texts)} steps, in this order:", "steps": texts, "token": token}


def execute(token, request):
    """Do a confirmed action. Returns a sentence for the manager; raises ActionError."""
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.SignatureExpired:
        raise ActionError("That suggestion expired. Ask again.")
    except signing.BadSignature:
        raise ActionError("That suggestion isn't valid. Ask again.")
    if data["u"] != request.user.pk or data["l"] != request.location.pk:
        raise ActionError("That suggestion was made for another login or location. Ask again.")
    from django.db import IntegrityError, transaction

    from .models import UsedConfirmation
    try:
        with transaction.atomic():
            used = UsedConfirmation.objects.create(nonce=data.get("n", ""))
    except IntegrityError:
        raise ActionError("That was already done.")
    try:
        if data["k"] == "plan":
            return _run_plan(data, request)
        return _run(data, request)
    except ActionError:
        used.delete()  # nothing happened: the same confirmation may be tried again
        raise


def _run_plan(data, request):
    done = []
    for i, (kind, params) in enumerate(data["p"]["steps"]):
        try:
            if kind == "pay_shift" and params.get("after_change"):
                done.append(_pay_after_change(params.get("shift_date"), request))
            else:
                done.append(_run({"k": kind, "p": params}, request))
        except ActionError as exc:
            if not done:
                raise
            raise PartlyDone(" ".join(f"{n + 1}. {text}" for n, text in enumerate(done))
                             + f" Stopped at step {i + 1}: {exc}")
    return " ".join(f"{n + 1}. {text}" for n, text in enumerate(done))


def _pay_after_change(shift_date, request):
    """The pay step of a plan whose earlier step closed the shift or added tips: split again if needed, then
    pay only if the check finds nothing (and the limits allow it, inside pay_night)."""
    from . import services
    from .check import blocking
    from .models import TipRuleSet
    location = request.location
    nights = Night.objects.filter(location=location, status__in=[Night.Status.CLOSED, Night.Status.REVIEW])
    if shift_date:
        nights = nights.filter(date=date.fromisoformat(shift_date))
    night = nights.order_by("-date", "-created_at").first()
    if night is None:
        raise ActionError("the shift isn't ready to pay (it may still need who worked). Open it to check.")
    if night.status == Night.Status.CLOSED:
        rules = TipRuleSet.active(location)
        if not rules or not night.shifts.exists():
            raise ActionError(f"the {night} needs who worked and tip rules before it can be split.")
        services.build_split(night, rules)
        night.refresh_from_db()
    if night.hold or blocking(night):
        reason = night.hold or blocking(night)[0]["text"]
        raise ActionError(f"the close-out check wants you to look at the {night} first: {reason} Open it to check.")
    return _run({"k": "pay_shift", "p": {"night": night.pk}}, request)


def _run(data, request):
    from . import services
    from .models import Change
    from .paypal import PayPalError
    kind, params, location = data["k"], data["p"], request.location
    try:
        if kind == "pay_shift":
            night = Night.objects.get(pk=params["night"], location=location)
            from .check import blocking
            if night.status != Night.Status.REVIEW or blocking(night):
                raise ActionError(f"The {night} can't be paid from here any more. Open it to check.")
            services.pay_night(night, by_owner=request.user.manager.is_owner)
            Change.log(request, f"Paid the {night} (asked in Ask about your tips)")
            return f"Payout for the {night} sent to PayPal."
        if kind == "set_week":
            member = Membership.objects.get(pk=params["membership"], location=location)
            member.schedule = params["week"]
            member.save(update_fields=["schedule"])
            Change.log(request, f"Set {member.staff.name}'s usual week: {member.schedule_summary} (asked in Ask)")
            return f"{member.staff.name}'s usual week is now {member.schedule_summary}."
        if kind == "close_shift":
            from . import autoclose, hours
            from .models import Change as _Change
            night = Night.objects.get(pk=params["night"], location=location)
            if night.status == Night.Status.OPEN:
                # the manager is closing now: whoever is still on the clock finishes now (they didn't forget)
                hours.finish_running_entries(night, mark_forgotten=False)
                autoclose.close_night(night, location, pay=False, auto=False)
            elif night.status == Night.Status.CLOSED:
                needs_check = False
                if not night.shifts.exists():
                    _, needs_check = hours.fill_roster(night, location.use_usual_schedule)
                autoclose.split_and_pay(night, location, needs_check, pay=False)
            else:
                raise ActionError(f"The {night} is already closed.")
            night = Night.objects.filter(pk=night.pk).first()
            if night is None:
                return "Nothing happened on that shift (no tips, nobody worked), so there was nothing to close."
            _Change.log(request, f"Closed out the {night} (asked in Ask about your tips)")
            cur = location.business.symbol
            if night.status == Night.Status.REVIEW:
                people = night.shares.filter(amount__gt=0).count()
                note = f" The check wants you to look first: {night.hold}" if night.hold else ""
                return (f"Closed and split the {night}: {cur}{night.tip_total} for {people} "
                        f"{'person' if people == 1 else 'people'}.{note}")
            if night.status == Night.Status.PAID:
                return f"Closed the {night}: no tips, so nothing to pay."
            return f"Closed the {night}. It still needs who worked: add them on the close-out page."
        if kind == "fix_payouts":
            return _fix_payouts(params, request)
        if kind == "add_tip":
            night = Night.objects.get(pk=params["night"], location=location)
            tip = services.add_manual_tip(night, params["amount"], params["source"])
            Change.log(request, f"Added {location.business.symbol}{tip.amount} {tip.get_source_display().lower()} "
                                f"tips to the {night} (asked in Ask)")
            return f"Added {location.business.symbol}{tip.amount} to the {night}."
    except (Night.DoesNotExist, Membership.DoesNotExist):
        raise ActionError("That shift or person isn't there any more.")
    except services.TipPoolError as exc:
        raise ActionError(str(exc))
    except PayPalError as exc:
        raise ActionError(f"PayPal said no: {exc}")
    raise ActionError("I can't do that one.")


def _night_to_pay(location, shift_date):
    ready = Night.objects.filter(location=location, status=Night.Status.REVIEW).order_by("-date", "-created_at")
    if shift_date:
        try:
            ready = ready.filter(date=date.fromisoformat(shift_date))
        except ValueError:
            raise ActionError("Which shift? e.g. \"pay Friday's shift\".")
    night = ready.first()
    if night is None:
        raise ActionError("There's no shift ready to pay" + (f" on {shift_date}" if shift_date else "") +
                          ". Close it out and check the split first.")
    if not shift_date and ready.count() > 1:
        raise ActionError("More than one shift is ready to pay. Say which one, e.g. \"pay Friday's shift\".")
    return night


def _night_to_close(location, shift_date):
    nights = Night.objects.filter(location=location, status=Night.Status.OPEN)
    if shift_date:
        try:
            day = date.fromisoformat(shift_date)
        except ValueError:
            raise ActionError("Which shift? e.g. \"close out yesterday\".")
        closed = Night.objects.filter(location=location, date=day, status=Night.Status.CLOSED,
                                      shares__isnull=True).first()
        night = nights.filter(date=day).first() or closed
    else:
        night = nights.order_by("-date").first()
    if night is None:
        raise ActionError("There's no open shift" + (f" on {shift_date}" if shift_date else "") + " to close.")
    return night


def _payouts_to_fix(location):
    """(failed payouts whose person is now paid somewhere else, unclaimed payouts not reminded today)."""
    from django.core.cache import cache

    from . import services
    from .models import PayoutItem
    items = (PayoutItem.objects.filter(batch__night__location=location, superseded=False)
             .select_related("share__staff", "share__night").order_by("-pk")[:200])
    failed, unclaimed = [], []
    for item in items:
        if item.failed:
            _, receiver = services.payout_destination(item.share.staff, location.business.currency)
            if receiver != item.receiver:
                failed.append(item)
        elif item.status == "UNCLAIMED" and not cache.get(f"payout-remind:{item.pk}"):
            unclaimed.append(item)
    return failed, unclaimed


def _fix_payouts(params, request):
    from django.core.cache import cache
    from django.urls import reverse

    from . import emails, services
    from .models import Change, PayoutItem
    location = request.location
    sent, reminded, skipped = [], [], []
    for item in PayoutItem.objects.filter(pk__in=params.get("failed", []), batch__night__location=location):
        try:
            new = services.resend_payout(item)
            sent.append(f"{item.share.staff.name} (to {new.receiver})")
        except services.TipPoolError as exc:
            skipped.append(f"{item.share.staff.name}: {exc}")
    for item in PayoutItem.objects.filter(pk__in=params.get("unclaimed", []), batch__night__location=location,
                                          status="UNCLAIMED", superseded=False).select_related("share__staff"):
        if location.business.is_demo:
            skipped.append(f"{item.share.staff.name}: the demo doesn't send emails")
            continue
        if not cache.add(f"payout-remind:{item.pk}", 1, 86400):
            continue
        link = request.build_absolute_uri(reverse("my_tips", args=[item.share.staff.access_token]))
        emails.payout_reminder(item, link)
        reminded.append(item.share.staff.name)
    if not (sent or reminded):
        raise ActionError("Nothing could be fixed: " + ("; ".join(skipped) or "they're already sorted.") + "")
    Change.log(request, "Fixed payouts (asked in Ask about your tips): "
               + "; ".join(filter(None, [("sent again to " + ", ".join(sent)) if sent else "",
                                         ("reminded " + ", ".join(reminded)) if reminded else ""])))
    out = []
    if sent:
        out.append("Sent again: " + ", ".join(sent) + ".")
    if reminded:
        out.append("Reminded " + ", ".join(reminded) + " to accept their payout.")
    if skipped:
        out.append("Not done: " + "; ".join(skipped) + ".")
    return " ".join(out)


def _night_for_tips(location, shift_date):
    open_states = [Night.Status.OPEN, Night.Status.CLOSED, Night.Status.REVIEW]
    if not shift_date:
        return Night.current(location)
    try:
        day = date.fromisoformat(shift_date)
    except ValueError:
        raise ActionError("Which shift?")
    night = Night.objects.filter(location=location, date=day, status__in=open_states).order_by("-created_at").first()
    if night is None:
        raise ActionError(f"There's no shift on {day:%b %d} that can still take tips.")
    return night


def _member(location, name):
    name = (name or "").strip().lower()
    team = list(Membership.objects.filter(location=location, active=True).select_related("staff"))
    if not name:
        raise ActionError("Who? e.g. \"set Maria to Mon-Fri 6h\".")
    match = [m for m in team if m.staff.name.lower() == name]
    if not match:
        match = [m for m in team if m.staff.name.lower().split()[0] == name.split()[0]]
    if len(match) != 1:
        raise ActionError(f"I couldn't find exactly one \"{name}\" on this location's team.")
    return match[0]
