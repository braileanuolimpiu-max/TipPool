"""Things you can ask TipPool to do: "pay Friday's shift", "set Maria to Mon-Fri 6h", "add $40 cash tips".

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


class ActionError(Exception):
    pass


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
    else:
        raise ActionError("I can't do that one.")
    token = signing.dumps({"k": kind, "p": params, "u": user.pk, "l": location.pk, "n": secrets.token_hex(16)},
                          salt=SALT, compress=True)
    return {"text": text, "token": token}


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
        return _run(data, request)
    except ActionError:
        used.delete()  # nothing happened: the same confirmation may be tried again
        raise


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
