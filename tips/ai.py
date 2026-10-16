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
from typing import Literal, Optional  # noqa: F401

from django.conf import settings
from pydantic import BaseModel, Field

from .roles import plural, word_pattern
from .split import compliance_warnings, validate_rules

log = logging.getLogger("tips.ai")

# Roles are the business's own list (Role model): Claude gets them in the prompt as ids,
# and every id it returns is checked against that list afterwards.
RoleName = str


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
RULES_SYSTEM = """You turn a business manager's tip-pool rules, written in plain language, into structured rules.
The business can be anything where staff get tips: a restaurant, cafe, bar, salon, spa, hotel, delivery crew...

How the rules work:
- "pools": each pool takes a percentage of the shift's total tips and splits it among the listed roles,
  either by hours worked ("hours") or the same amount each ("equal").
- "remainder": the group that shares whatever is left after the pools.
  "Everyone" / "the whole team" means every role that may receive tips.
- "excluded_roles": roles that never get tips.
- Use ONLY the role ids from the business's role list (map synonyms and plurals onto them).
  If the text names a role that is not on the list, leave it out and say so in "warnings".
- If the text does not say how to split, use "hours". Percentages must not add up to more than 100.

In "warnings", flag (in short plain English):
- anything that may be illegal under US federal law (FLSA): managers, supervisors or owners taking from the
  tip pool (roles marked "manager/owner" in the list); mandatory pools that include staff who don't usually
  get tips from customers (e.g. kitchen) when the employer takes a tip credit
- anything ambiguous, and every assumption you had to make
If the manager gives a manager or owner a share, still include it in the rules exactly as written, and warn."""


def _business_roles():
    from .models import Role
    return list(Role.objects.all())


def _role_list_text(roles):
    lines = []
    for r in roles:
        extra = f"; also called: {r.aliases}" if r.aliases else ""
        flag = "; manager/owner, may not receive tips" if r.no_tips else ""
        lines.append(f"- {r.key} ({r.name}{extra}{flag})")
    return "Role list of this business (id, name):\n" + "\n".join(lines)


def _keep_known(keys, known, warnings):
    out = []
    for key in keys:
        if key in known:
            if key not in out:
                out.append(key)
        else:
            warning = f"Unknown role '{key}' was left out. Add it on the Team page if you need it."
            if warning not in warnings:
                warnings.append(warning)
    return out


def parse_rules(text):
    """Returns dict: rules, summary, warnings. Raises AIError if the rules can't be used."""
    text = (text or "").strip()
    if not text:
        raise AIError("Please describe your tip rules.")
    roles = _business_roles()
    if not roles:
        raise AIError("Add your team's roles first (Team page).")
    known = {r.key for r in roles}
    no_tips = {r.key for r in roles if r.no_tips}
    if mode() == "claude":
        content = [{"type": "text", "text": _role_list_text(roles) + "\n\nThe manager's rules:\n" + text}]
        out = _claude_parse(RULES_SYSTEM, content, RulesOut, effort="medium")
        warnings = list(out.warnings)
        pools = []
        for pool in out.pools:
            pool = pool.model_dump()
            pool["roles"] = _keep_known(pool["roles"], known, warnings)
            if pool["roles"]:
                pools.append(pool)
        remainder = out.remainder.model_dump() if out.remainder else None
        if remainder:
            remainder["roles"] = _keep_known(remainder["roles"], known, warnings)
            if not remainder["roles"]:
                remainder = None
        rules = {"pools": pools, "remainder": remainder,
                 "excluded_roles": sorted(set(_keep_known(out.excluded_roles, known, warnings)) | no_tips)}
        summary = out.summary
    else:
        rules, summary, warnings = _mock_parse_rules(text, roles)

    try:
        validate_rules(rules)
    except ValueError as exc:
        raise AIError(f"These rules can't be applied: {exc}") from exc
    for warning in compliance_warnings(rules, no_tips):
        if warning not in warnings:
            warnings.append(warning)
    return {"rules": rules, "summary": summary, "warnings": warnings}


def _roles_in(fragment, roles):
    return [r for r in roles if re.search(rf"\b(?:{word_pattern(r)})\b", fragment, re.I)]


def _group_name(found):
    return ", ".join(plural(r.name) for r in found)


EVERYONE = r"\b(?:everyone|everybody|whole team|all staff|the team)\b"
REST = r"\b(?:rest|remaining|remainder|everything else|left)\b"


def _mock_parse_rules(text, roles):
    """Very small offline parser for sentences like
    'Bartenders get 20%, bussers 10% split equally, the rest goes to servers by hours'.
    Works with whatever roles the business has."""
    pools, warnings = [], ["Parsed offline (AI_MODE=mock). Turn on Claude for smarter rule reading."]
    eligible = [r for r in roles if not r.no_tips]
    parts = []
    for part in re.split(r"[,;:\n]|\.(?=\s|$)", text):  # keep "12.5%" intact
        # "bartenders 20% and bussers 10%" -> two parts; "bussers and hosts 10%" stays one
        parts.extend(re.split(r"\band\b", part) if part.count("%") > 1 else [part])
    remainder = None
    everyone_split = None
    listed = []  # roles named without a percentage, e.g. "Everyone shares by hours: A, B and C"
    for part in parts:
        part = part.strip()
        if not part:
            continue
        split = "equal" if re.search(r"equal|evenly|same", part, re.I) else "hours"
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", part)
        found = _roles_in(part, roles)
        if percent and found:
            pools.append({"name": _group_name(found), "percent": float(percent.group(1)),
                          "roles": [r.key for r in found], "split": split})
        elif found and re.search(REST, part, re.I):
            remainder = {"name": _group_name(found), "roles": [r.key for r in found], "split": split}
        elif not percent and re.search(EVERYONE, part, re.I):
            everyone_split = split
            listed += [r for r in found if r not in listed]
        elif found and not percent:
            listed += [r for r in found if r not in listed]
    if remainder is None and everyone_split is not None:
        group = [r for r in listed if not r.no_tips] or eligible
        remainder = {"name": "Everyone" if group == eligible else _group_name(group),
                     "roles": [r.key for r in group], "split": everyone_split}
    if remainder is None:
        in_pools = {k for p in pools for k in p["roles"]}
        group = [r for r in eligible if r.key not in in_pools] or eligible
        remainder = {"name": "Everyone else" if pools else "Everyone", "roles": [r.key for r in group],
                     "split": "hours"}
        warnings.append(f"No group for the rest of the money was mentioned, so I assumed: "
                        f"{remainder['name'].lower()}, by hours.")
    if not pools:
        warnings.append("No percentages found, so all tips go to one group.")
    bits = [f"{p['name']} get {p['percent']:g}% ({'by hours' if p['split'] == 'hours' else 'split equally'})"
            for p in pools]
    bits.append(f"{'the rest goes to' if pools else 'all tips go to'} {remainder['name'].lower()} "
                f"{'by hours' if remainder['split'] == 'hours' else 'split equally'}")
    summary = "; ".join(bits)
    summary = summary[0].upper() + summary[1:] + "."
    return {"pools": pools, "remainder": remainder,
            "excluded_roles": [r.key for r in roles if r.no_tips]}, summary, warnings


# ---------------------------------------------------------------------------
# 2. Schedule photo
# ---------------------------------------------------------------------------
SCHEDULE_SYSTEM = """You read a photo of a shift sheet / schedule / timecard and list who worked.
For each person give their name, role and hours worked (decimal hours; 5:00pm-11:30pm = 6.5).
Use only role ids from the business's role list; if a role is unclear, use the person's usual role.
Match names to the staff list when they clearly refer to the same person (use the staff-list spelling).
Never guess numbers you cannot read: leave the person out and explain it in "notes"."""


def read_schedule(image_bytes, media_type, staff_names):
    """Returns dict: shifts [{name, role, hours}], notes []."""
    if mode() != "claude":
        raise AIError("Reading a photo needs the real AI (AI_MODE=claude). For now, enter the hours below.")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(image_bytes).decode("utf-8")}},
        {"type": "text", "text": _role_list_text(_business_roles()) + "\n\nStaff list: " + ", ".join(staff_names)
                                 + "\nList everyone who worked."},
    ]
    out = _claude_parse(SCHEDULE_SYSTEM, content, ScheduleOut, effort="medium")
    return {"shifts": [s.model_dump() for s in out.shifts], "notes": list(out.notes)}


# ---------------------------------------------------------------------------
# 3. Explanations
# ---------------------------------------------------------------------------
EXPLAIN_SYSTEM = """You write a short, friendly explanation for each team member of how their share of
this shift's tip pool was calculated. Use ONLY the numbers given; never recalculate or change an amount.
2-3 sentences each, plain language, mention the group(s), the basis (hours or equal) and the amount."""


def explain_shares(total, rules_summary, shares, notes, role_names=None):
    """shares: list of PersonShare. role_names: {role key: name}. Returns {name: explanation}."""
    role_names = role_names or {}

    def role_of(share):
        return role_names.get(share.role, share.role).lower()

    if mode() == "claude":
        lines = [f"Total tips this shift: ${total}", f"Rules: {rules_summary}"]
        for s in shares:
            parts = "; ".join(f"{b['group']}: {b['basis']} of ${b['pool']} = ${b['amount']}" for b in s.breakdown)
            lines.append(f"- {s.name} ({role_of(s)}, {s.hours}h): ${s.amount}. {parts or 'no share'}")
        if notes:
            lines.append("Notes: " + " ".join(notes))
        try:
            out = _claude_parse(EXPLAIN_SYSTEM, [{"type": "text", "text": "\n".join(lines)}],
                                ExplanationsOut, effort="low")
            return {e.name: e.explanation for e in out.explanations}
        except AIError as exc:
            log.warning("Falling back to template explanations: %s", exc)
    return {s.name: _template_explanation(s, role_of(s)) for s in shares}


def _template_explanation(share, role):
    if not share.breakdown:
        return f"{share.name} worked as {role}, which doesn't receive tips under the house rules."
    parts = []
    for b in share.breakdown:
        basis = b["basis"]
        parts.append(f"from the {b['group']} pool of ${b['pool']} you get {basis} = ${b['amount']}")
    return f"You worked {share.hours.normalize()}h as {role}: " + "; ".join(parts) + f". Total: ${share.amount}."


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
