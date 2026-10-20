"""Ask about your tips: "How much did bussers make in March?", "Who earned the most last week?"

AI_MODE=claude -> Claude answers from a summary of the last 12 months (it only sees these numbers)
AI_MODE=mock   -> a small offline reader handles the common questions with exact sums

The numbers always come from reports.py (plain sums). The AI only reads and explains them.
"""
import calendar
from collections import Counter
import re
from datetime import date, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import actions, ai, reports
from .roles import plural

EXAMPLES = [
    "Who earned the most this month?",
    "How much did servers make last week?",
    "What were our total tips this year?",
    "Pay yesterday's shift",
    "Add $40 cash tips",
]

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


class ActionOut(BaseModel):
    kind: Literal["pay_shift", "set_week", "add_tip"]
    shift_date: Optional[str] = Field(default=None, description="YYYY-MM-DD of the shift, if one is meant")
    person: Optional[str] = Field(default=None, description="For set_week: the person's name")
    week: Optional[dict[str, float]] = Field(default=None, description="For set_week: hours per weekday, "
                                             "keys '0'..'6' (0 = Monday), only the days they work")
    amount: Optional[float] = Field(default=None, description="For add_tip")
    source: Optional[Literal["card", "cash"]] = None


class AnswerOut(BaseModel):
    answer: str = Field(description="The answer in 1-4 short plain sentences, or a short list. Exact numbers.")
    action: Optional[ActionOut] = Field(default=None, description="Only when the manager asks you to DO one of "
                                        "the actions. It is shown to them to confirm; never say it's done.")


ASK_SYSTEM = """You answer a manager's questions about their team's tips, using ONLY the data given.
- Quote exact dollar amounts and hours from the data; never estimate or invent numbers.
- If the data can't answer the question, say so in one sentence and say what's missing.
- Be short: 1-4 plain sentences, or a short list of up to 6 lines.
- "Shared" = from the tip pool under the rules; "personal" = tips guests left for one person by name.
- You may point out something notable (e.g. a big change), but don't give legal or tax advice.
- You can propose ONE action when asked to do something: pay a shift that is ready, set someone's usual
  week, or add card/cash tips. Fill "action" and say in "answer" what will happen once they confirm."""


# ---------------------------------------------------------------------------
def answer(question, locations, today, location=None, user=None, named_from=None):
    """Answer a question, or propose an action to confirm (only for `location`, by `user`).
    A location named in the question ("How much did Uptown make?") is the one answered about."""
    question = (question or "").strip()[:300]
    if not question:
        return None
    named = [loc for loc in (named_from or []) if loc.display_name.lower() in question.lower()]
    if named:
        locations = named
    result = _answer(question, locations, today, location, user)
    if result and named and result.get("by") != "Claude":
        result["answer"] = f"{', '.join(loc.display_name for loc in named)}: {result['answer']}"
    return result


def _answer(question, locations, today, location=None, user=None):
    if ai.mode() == "claude":
        try:
            content = [{"type": "text", "text": _data_summary(locations, today)
                        + (_waiting(location) if location else "") + "\n\nQuestion: " + question}]
            out = ai._claude_parse(ASK_SYSTEM, content, AnswerOut, effort="low")
            result = {"answer": out.answer, "by": "Claude"}
            if out.action and location and user:
                result.update(_proposal(out.action.kind, out.action.model_dump(exclude_none=True), location, user))
            return result
        except ai.AIError as exc:
            return {"answer": f"{exc} Here's what I can work out offline: " + _offline(question, locations, today),
                    "by": "offline"}
    parsed = actions.parse(question, today) if location and user else None
    if parsed:
        return {"by": "offline", **_proposal(parsed[0], parsed[1], location, user)}
    return {"answer": _offline(question, locations, today), "by": "offline"}


def _proposal(kind, params, location, user):
    try:
        proposal = actions.propose(kind, params, location, user)
        return {"answer": f"{proposal['text']} Confirm below to do it.", "proposal": proposal}
    except actions.ActionError as exc:
        return {"answer": str(exc)}


def _waiting(location):
    from .models import Night
    rows = Night.objects.filter(location=location, status__in=[Night.Status.CLOSED, Night.Status.REVIEW])
    team = ", ".join(m.staff.name for m in location.memberships.filter(active=True).select_related("staff"))
    lines = ["", f"## Now at {location.display_name}", f"Team: {team or 'nobody yet'}"]
    lines += [f"- shift {n.date.isoformat()} ({n.date:%A}): {n.get_status_display()}, "
              f"{location.business.symbol}{n.tip_total}" for n in rows]
    return "\n".join(lines)


def _money_in(locations):
    symbol = locations[0].business.symbol if locations else "$"
    return lambda value: f"{symbol}{value:,.2f}"


# --- data for Claude ------------------------------------------------------
def _data_summary(locations, today):
    _money = _money_in(locations)
    lines = [f"Today is {today:%A, %B %d, %Y}. Locations: " + ", ".join(l.display_name for l in locations) + "."]
    first_of_month = today.replace(day=1)
    months = []
    d = first_of_month
    for _ in range(12):
        months.append(d)
        d = (d - timedelta(days=1)).replace(day=1)
    for start in reversed(months):
        end = min(start.replace(day=calendar.monthrange(start.year, start.month)[1]), today)
        r = reports.build(locations, start, end)
        if not r["shift_count"]:
            continue
        lines.append(f"\n## {start:%B %Y} ({r['shift_count']} shifts): collected {_money(r['collected'])}, "
                     f"paid out {_money(r['paid_out'])}; " + ", ".join(f"{k} {_money(v)}" for k, v in r["by_source"]))
        for p in r["people"]:
            lines.append(f"- {p['name']} ({p['roles']}): {p['shifts']} shifts, {p['hours']}h, shared {_money(p['pool'])}, "
                         f"personal {_money(p['personal'])}, total {_money(p['total'])}")
        for role in r["roles"]:
            lines.append(f"- role {role['name']}: {role['people']} people, {role['hours']}h, total {_money(role['total'])}")
    week_start = today - timedelta(days=today.weekday())
    lines.append("\n## Last 8 weeks (Monday to Sunday)")
    for i in range(8):
        start = week_start - timedelta(weeks=i)
        r = reports.build(locations, start, min(start + timedelta(days=6), today))
        top = ", ".join(f"{p['name']} {_money(p['total'])}" for p in r["people"][:5])
        lines.append(f"- week of {start:%b %d}: {r['shift_count']} shifts, {_money(r['collected'])}" + (f"; {top}" if top else ""))
    return "\n".join(lines)


# --- offline answers --------------------------------------------------------
def _period(question, today):
    """(first, last, words) for the period a question talks about. Default: this month."""
    q = question.lower()
    for key, words in [("last_week", "last week"), ("this_week", "this week"), ("last_month", "last month"),
                       ("this_month", "this month"), ("this_year", "this year")]:
        if words in q:
            first, last = reports.period_range(key, today)
            return first, last, words
    if "today" in q:
        return today, today, "today"
    if "yesterday" in q:
        return today - timedelta(days=1), today - timedelta(days=1), "yesterday"
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", q)
    if m:
        month = MONTHS[m.group(1)]
        year = today.year if month <= today.month else today.year - 1
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), min(date(year, month, last_day), today), f"in {calendar.month_name[month]}"
    first, last = reports.period_range("this_month", today)
    return first, last, "this month"


def _offline(question, locations, today):
    _money = _money_in(locations)
    first, last, when = _period(question, today)
    r = reports.build(locations, first, last)
    q = question.lower()
    if not r["shift_count"]:
        return f"There are no closed shifts {when} yet."

    firsts = Counter(p["name"].split()[0].lower() for p in r["people"] if p["name"].split())
    for p in r["people"]:
        first = p["name"].split()[0].lower() if p["name"].split() else ""
        full_match = re.search(rf"\b{re.escape(p['name'].lower())}\b", q)
        first_match = first and firsts[first] == 1 and re.search(rf"\b{re.escape(first)}\b", q)
        if full_match or first_match:
            extra = f" ({_money(p['personal'])} of it personal tips)" if p["personal"] else ""
            rate = f", about {_money(p['per_hour'])} an hour from shared tips" if p["per_hour"] else ""
            return (f"{p['name']} made {_money(p['total'])} {when}{extra}: {p['shifts']} shift"
                    f"{'s' if p['shifts'] != 1 else ''}, {p['hours'].normalize():f} hours{rate}.")

    for role in r["roles"]:
        name = role["name"].lower()
        if re.search(rf"\b{re.escape(name)}(s|es)?\b", q):
            rate = f", {_money(role['per_hour'])} an hour" if role["per_hour"] else ""
            return (f"{plural(role['name'])} made {_money(role['total'])} {when}, shared by {role['people']} "
                    f"{'person' if role['people'] == 1 else 'people'} over {role['hours'].normalize():f} hours{rate}.")

    if re.search(r"\b(most|top|highest|best)\b", q) and r["people"]:
        best = r["people"][0]["total"]
        tied = [p["name"] for p in r["people"] if p["total"] == best]
        if len(tied) > 1:
            names = ", ".join(tied[:-1]) + " and " + tied[-1]
            return f"{names} earned the most {when}: {_money(best)} each."
        rest = r["people"][1] if len(r["people"]) > 1 else None
        runner = f" Next: {rest['name']} with {_money(rest['total'])}." if rest else ""
        return f"{tied[0]} earned the most {when}: {_money(best)}.{runner}"

    if re.search(r"\b(average|avg|per shift)\b", q):
        return f"The average shift {when} brought in {_money(r['per_shift'])} in tips ({_count(r['shift_count'], 'shift')})."

    if re.search(r"\b(cash|card)\b", q):
        sources = ", ".join(f"{label}: {_money(v)}" for label, v in r["by_source"])
        return f"Tips by type {when}: {sources}."

    if re.search(r"\b(total|how much|collected|all tips|tips)\b", q):
        return (f"Total tips {when}: {_money(r['collected'])} over {_count(r['shift_count'], 'shift')}, "
                f"{_money(r['paid_out'])} of it paid out so far.")

    return ("The built-in assistant can answer questions like: " + "; ".join(f'"{e}"' for e in EXAMPLES)
            + ". With Claude turned on you can ask anything.")


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"
