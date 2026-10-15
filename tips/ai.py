"""The AI layer: turns messy human input into structured data, and explains results.

Three jobs:
1. parse_rules(text)          plain-language tip rules  -> rules JSON (+ warnings)
2. read_schedule(image)       photo of the shift sheet   -> who worked, role, hours
3. explain_shares(...)        the calculated split        -> a short "why" per person

AI_MODE=mock   -> no API key needed, no cost (simple built-in parser, template explanations)
AI_MODE=claude -> real Claude API (needs ANTHROPIC_API_KEY)

Important: the AI never decides amounts. It writes the rules; split.py does the math.
"""
import base64
import logging
import re
from typing import Literal, Optional

from django.conf import settings
from pydantic import BaseModel, Field

from .split import compliance_warnings, validate_rules

log = logging.getLogger("tips.ai")

RoleName = Literal["server", "bartender", "busser", "host", "runner", "cook", "dishwasher", "manager"]


class AIError(Exception):
    pass


def mode():
    return settings.AI_MODE


# ---------------------------------------------------------------------------
# Structured output shapes (used by Claude mode)
# ---------------------------------------------------------------------------
class PoolOut(BaseModel):
    name: str = Field(description="Short label, e.g. 'Bartenders'")
    percent: float = Field(description="Percent of the total tips, 0-100")
    roles: list[RoleName]
    split: Literal["hours", "equal"]


class RemainderOut(BaseModel):
    name: str
    roles: list[RoleName]
    split: Literal["hours", "equal"]


class RulesOut(BaseModel):
    pools: list[PoolOut]
    remainder: Optional[RemainderOut]
    excluded_roles: list[RoleName]
    summary: str = Field(description="One or two plain sentences describing the rules")
    warnings: list[str] = Field(description="Legal or fairness problems, ambiguities, or assumptions made")


class ShiftOut(BaseModel):
    name: str
    role: RoleName
    hours: float


class ScheduleOut(BaseModel):
    shifts: list[ShiftOut]
    notes: list[str] = Field(description="Anything unclear or unreadable in the photo")


class ExplanationOut(BaseModel):
    name: str
    explanation: str


class ExplanationsOut(BaseModel):
    explanations: list[ExplanationOut]


# ---------------------------------------------------------------------------
# 1. Rules
# ---------------------------------------------------------------------------
RULES_SYSTEM = """You turn a restaurant manager's tip-pool rules, written in plain language, into structured rules.

How the rules work:
- "pools": each pool takes a percentage of the night's total tips and splits it among the listed roles,
  either by hours worked ("hours") or the same amount each ("equal").
- "remainder": the group that shares whatever is left after the pools. Usually servers, by hours.
- "excluded_roles": roles that never get tips.
- Allowed roles: server, bartender, busser, host, runner, cook, dishwasher, manager.
  Map synonyms (waiter/waitress -> server, busboy -> busser, hostess -> host, chef/kitchen -> cook).
- If the text does not say how to split, use "hours". Percentages must not add up to more than 100.

In "warnings", flag (in short plain English):
- anything that may be illegal under US federal law (FLSA): managers, supervisors or owners taking from the
  tip pool; mandatory pools that include back-of-house staff when the employer takes a tip credit
- anything ambiguous, and every assumption you had to make
If the manager gives a manager or owner a share, still include it in the rules exactly as written, and warn."""


def parse_rules(text):
    """Returns dict: rules, summary, warnings. Raises AIError if the rules can't be used."""
    text = (text or "").strip()
    if not text:
        raise AIError("Please describe your tip rules.")
    if mode() == "claude":
        out = _claude_parse(RULES_SYSTEM, [{"type": "text", "text": text}], RulesOut, effort="medium")
        rules = {
            "pools": [p.model_dump() for p in out.pools],
            "remainder": out.remainder.model_dump() if out.remainder else None,
            "excluded_roles": list(out.excluded_roles),
        }
        summary, warnings = out.summary, list(out.warnings)
    else:
        rules, summary, warnings = _mock_parse_rules(text)

    try:
        validate_rules(rules)
    except ValueError as exc:
        raise AIError(f"These rules can't be applied: {exc}") from exc
    for warning in compliance_warnings(rules):
        if warning not in warnings:
            warnings.append(warning)
    return {"rules": rules, "summary": summary, "warnings": warnings}


ROLE_WORDS = {
    "server": r"servers?|waiters?|waitress(?:es)?|waitstaff",
    "bartender": r"bartenders?|bar staff|bar",
    "busser": r"bussers?|busboys?|bus staff",
    "host": r"hosts?|hostess(?:es)?",
    "runner": r"(?:food )?runners?",
    "cook": r"cooks?|chefs?|kitchen(?: staff)?|line cooks?",
    "dishwasher": r"dishwashers?",
    "manager": r"managers?|owners?|supervisors?|me|myself",
}


def _roles_in(fragment):
    found = []
    for role, pattern in ROLE_WORDS.items():
        if re.search(rf"\b(?:{pattern})\b", fragment, re.I):
            found.append(role)
    return found


def _mock_parse_rules(text):
    """Very small offline parser for sentences like
    'Bartenders get 20%, bussers 10% split equally, the rest goes to servers by hours'."""
    pools, warnings = [], ["Parsed offline (AI_MODE=mock). Turn on Claude for smarter rule reading."]
    parts = []
    for part in re.split(r"[,;\n]|\.(?=\s|$)", text):  # keep "12.5%" intact
        # "bartenders 20% and bussers 10%" -> two parts; "bussers and hosts 10%" stays one
        parts.extend(re.split(r"\band\b", part) if part.count("%") > 1 else [part])
    remainder = None
    for part in parts:
        part = part.strip()
        if not part:
            continue
        split = "equal" if re.search(r"equal|evenly|same", part, re.I) else "hours"
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", part)
        roles = _roles_in(part)
        if percent and roles:
            name = ", ".join(r.capitalize() + "s" for r in roles)
            pools.append({"name": name, "percent": float(percent.group(1)), "roles": roles, "split": split})
        elif roles and re.search(r"rest|remaining|remainder|everything else|left", part, re.I):
            name = ", ".join(r.capitalize() + "s" for r in roles)
            remainder = {"name": name, "roles": roles, "split": split}
    if remainder is None:
        remainder = {"name": "Servers", "roles": ["server"], "split": "hours"}
        warnings.append("No group for the rest of the money was mentioned, so I assumed: servers, by hours.")
    if not pools:
        warnings.append("No percentages found, so all tips go to the remainder group.")
    summary_bits = [f"{p['name']} get {p['percent']:g}% ({'by hours' if p['split'] == 'hours' else 'split equally'})"
                    for p in pools]
    summary_bits.append(f"the rest goes to {remainder['name'].lower()} "
                        f"{'by hours' if remainder['split'] == 'hours' else 'split equally'}")
    summary = "; ".join(summary_bits).capitalize() + "."
    return {"pools": pools, "remainder": remainder, "excluded_roles": ["manager"]}, summary, warnings


# ---------------------------------------------------------------------------
# 2. Schedule photo
# ---------------------------------------------------------------------------
SCHEDULE_SYSTEM = """You read a photo of a restaurant shift sheet / schedule / timecard and list who worked.
For each person give their name, role and hours worked (decimal hours; 5:00pm-11:30pm = 6.5).
Allowed roles: server, bartender, busser, host, runner, cook, dishwasher, manager.
Match names to the staff list when they clearly refer to the same person (use the staff-list spelling).
Never guess numbers you cannot read: leave the person out and explain it in "notes"."""


def read_schedule(image_bytes, media_type, staff_names):
    """Returns dict: shifts [{name, role, hours}], notes []."""
    if mode() != "claude":
        raise AIError("Reading a photo needs the real AI (AI_MODE=claude). For now, enter the hours below.")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(image_bytes).decode("utf-8")}},
        {"type": "text", "text": "Staff list: " + ", ".join(staff_names) + "\nList everyone who worked."},
    ]
    out = _claude_parse(SCHEDULE_SYSTEM, content, ScheduleOut, effort="medium")
    return {"shifts": [s.model_dump() for s in out.shifts], "notes": list(out.notes)}


# ---------------------------------------------------------------------------
# 3. Explanations
# ---------------------------------------------------------------------------
EXPLAIN_SYSTEM = """You write a short, friendly explanation for each restaurant worker of how their share of
this shift's tip pool was calculated. Use ONLY the numbers given; never recalculate or change an amount.
2-3 sentences each, plain language, mention the group(s), the basis (hours or equal) and the amount."""


def explain_shares(total, rules_summary, shares, notes):
    """shares: list of PersonShare. Returns {name: explanation}."""
    if mode() == "claude":
        lines = [f"Total tips this shift: ${total}", f"Rules: {rules_summary}"]
        for s in shares:
            parts = "; ".join(f"{b['group']}: {b['basis']} of ${b['pool']} = ${b['amount']}" for b in s.breakdown)
            lines.append(f"- {s.name} ({s.role}, {s.hours}h): ${s.amount}. {parts or 'no share'}")
        if notes:
            lines.append("Notes: " + " ".join(notes))
        try:
            out = _claude_parse(EXPLAIN_SYSTEM, [{"type": "text", "text": "\n".join(lines)}],
                                ExplanationsOut, effort="low")
            return {e.name: e.explanation for e in out.explanations}
        except AIError as exc:
            log.warning("Falling back to template explanations: %s", exc)
    return {s.name: _template_explanation(s) for s in shares}


def _template_explanation(share):
    if not share.breakdown:
        return f"{share.name} worked as {share.role}, which doesn't receive tips under the house rules."
    parts = []
    for b in share.breakdown:
        basis = b["basis"]
        parts.append(f"from the {b['group']} pool of ${b['pool']} you get {basis} = ${b['amount']}")
    return f"You worked {share.hours.normalize()}h as {share.role}: " + "; ".join(parts) + f". Total: ${share.amount}."


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------
def _claude_parse(system, content, output_model, effort="medium"):
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AIError("The anthropic package is not installed.") from exc

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    try:
        response = client.messages.parse(
            model=settings.CLAUDE_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            output_format=output_model,
        )
    except anthropic.AuthenticationError as exc:
        raise AIError("The Claude API key is missing or invalid (ANTHROPIC_API_KEY).") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Claude is rate-limited right now. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Claude API error ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("Could not reach the Claude API (network).") from exc

    if response.stop_reason == "refusal":
        raise AIError("Claude declined this request.")
    if response.parsed_output is None:
        raise AIError("Claude's answer could not be read. Please try again.")
    log.info("Claude usage: in=%s out=%s", response.usage.input_tokens, response.usage.output_tokens)
    return response.parsed_output
