"""The tip-split engine: plain, deterministic Python. No AI in here.

The AI only *writes* the rules (as JSON). This module does all the money math,
in exact cents, so the shares always add up to the night's total.

Rules format (what the AI / the rule form produces):

    {
      "pools": [
        {"name": "Bartenders", "percent": 20, "roles": ["bartender"], "split": "hours"},
        {"name": "Bussers",    "percent": 10, "roles": ["busser"],    "split": "equal"}
      ],
      "remainder": {"name": "Servers", "roles": ["server"], "split": "hours"},
      "excluded_roles": ["manager"]
    }

- each pool takes `percent` of the total and splits it among people with those roles
- whatever is left goes to the `remainder` group
- "split": "hours" = by hours worked, "equal" = same amount each
"""
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")
SPLIT_METHODS = {"hours", "equal"}
NEVER_IN_POOL = {"manager", "owner", "supervisor"}  # US FLSA: they can't receive from the pool


class RulesError(ValueError):
    """The rules can't be used as written (e.g. percentages over 100%)."""


@dataclass
class ShiftInput:
    key: str  # anything that identifies the person (staff id)
    name: str
    role: str
    hours: Decimal


@dataclass
class PersonShare:
    key: str
    name: str
    role: str
    hours: Decimal
    amount: Decimal = Decimal("0.00")
    breakdown: list = field(default_factory=list)


@dataclass
class SplitResult:
    total: Decimal
    shares: list
    notes: list  # plain-English notes about what happened (e.g. unused pools)


def _norm(role):
    return (role or "").strip().lower()


def validate_rules(rules):
    """Raise RulesError if the rules can't be applied. Returns the cleaned rules."""
    if not isinstance(rules, dict):
        raise RulesError("Rules must be an object.")
    pools = rules.get("pools", [])
    remainder = rules.get("remainder")
    if not pools and not remainder:
        raise RulesError("Rules need at least one pool or a remainder group.")

    total_percent = Decimal("0")
    for pool in pools:
        percent = Decimal(str(pool.get("percent", 0)))
        if percent <= 0:
            raise RulesError(f"Pool '{pool.get('name')}' needs a percentage above 0.")
        if not pool.get("roles"):
            raise RulesError(f"Pool '{pool.get('name')}' has no roles.")
        if pool.get("split", "hours") not in SPLIT_METHODS:
            raise RulesError(f"Pool '{pool.get('name')}' split must be 'hours' or 'equal'.")
        total_percent += percent
    if total_percent > 100:
        raise RulesError(f"The pools add up to {total_percent}%, which is more than 100%.")
    if remainder:
        if not remainder.get("roles"):
            raise RulesError("The remainder group has no roles.")
        if remainder.get("split", "hours") not in SPLIT_METHODS:
            raise RulesError("The remainder split must be 'hours' or 'equal'.")
    elif total_percent < 100:
        raise RulesError(f"The pools only add up to {total_percent}% and there is no group for the rest.")
    return rules


def compliance_warnings(rules, never_in_pool=frozenset()):
    """Rule checks that don't need AI (the AI can add more on top).

    `never_in_pool`: this business's own manager/owner roles, on top of the built-in ones.
    """
    never = NEVER_IN_POOL | {_norm(r) for r in never_in_pool}
    warnings = []
    groups = list(rules.get("pools", []))
    if rules.get("remainder"):
        groups.append(rules["remainder"])
    for group in groups:
        bad = never & {_norm(r) for r in group.get("roles", [])}
        if bad:
            warnings.append(
                f"'{group.get('name')}' includes {', '.join(sorted(bad))}. Under US federal law (FLSA), "
                "managers and supervisors may not receive money from an employee tip pool."
            )
    return warnings


def _weights(members, method):
    if method == "equal":
        return [Decimal(1) for _ in members]
    return [Decimal(str(m.hours)) for m in members]


def calculate_split(total, shifts, rules, never_in_pool=frozenset(), role_names=None):
    """Split `total` (Decimal dollars) between `shifts` (list of ShiftInput) using `rules`.

    `never_in_pool`: extra role keys that never get tips (the business's manager/owner roles).
    `role_names`: {key: display name}, only used to write readable notes.
    """
    validate_rules(rules)
    total = Decimal(str(total)).quantize(CENT)
    notes = []
    excluded = {_norm(r) for r in rules.get("excluded_roles", [])} | NEVER_IN_POOL | {_norm(r) for r in never_in_pool}
    role_names = role_names or {}

    def label(role):
        return role_names.get(role, role).lower()

    people = {s.key: PersonShare(s.key, s.name, _norm(s.role), Decimal(str(s.hours))) for s in shifts}
    exact = {key: Decimal("0") for key in people}  # un-rounded amounts

    def members_of(roles):
        roles = {_norm(r) for r in roles}
        return [p for p in people.values() if p.role in roles and p.role not in excluded and p.hours > 0]

    def distribute(group_name, amount, members, method):
        weights = _weights(members, method)
        weight_total = sum(weights)
        for person, weight in zip(members, weights):
            part = amount * weight / weight_total
            exact[person.key] += part
            if method == "hours":
                basis = f"{person.hours.normalize()}h of {weight_total.normalize()}h"
            else:
                basis = "the whole pool" if len(members) == 1 else f"an equal 1/{len(members)} share"
            person.breakdown.append({"group": group_name, "pool": str(amount.quantize(CENT)),
                                     "basis": basis, "amount": str(part.quantize(CENT))})

    used = Decimal("0")
    for pool in rules.get("pools", []):
        pool_amount = total * Decimal(str(pool["percent"])) / 100
        members = members_of(pool["roles"])
        if not members:
            notes.append(f"Nobody worked as {', '.join(label(_norm(r)) for r in pool['roles'])} this shift, so the "
                         f"{pool['percent']}% for {pool['name']} goes to the rest of the team.")
            continue
        distribute(pool["name"], pool_amount, members, pool.get("split", "hours"))
        used += pool_amount

    rest = total - used
    remainder = rules.get("remainder")
    if rest > 0:
        members = members_of(remainder["roles"]) if remainder else []
        method = remainder.get("split", "hours") if remainder else "hours"
        name = remainder["name"] if remainder else "Rest of the team"
        if not members:
            # nobody in the remainder group: share the rest with everyone eligible, by hours
            members = [p for p in people.values() if p.role not in excluded and p.hours > 0]
            method = "hours"
            notes.append(f"Nobody in '{name}' worked this shift, so the remaining money was shared by hours "
                         "between everyone who worked.")
        if not members:
            raise RulesError("Nobody who worked this shift can receive tips under these rules.")
        distribute(name, rest, members, method)

    for person in people.values():
        if person.role in excluded and person.hours > 0:
            notes.append(f"{person.name} worked as {label(person.role)}, which is excluded from the tip pool.")
        elif exact[person.key] == 0 and person.hours > 0:
            notes.append(f"{person.name} worked as {label(person.role)}, but no rule gives tips to that role.")

    _round_to_cents(total, people, exact)
    shares = sorted(people.values(), key=lambda p: (-p.amount, p.name))
    return SplitResult(total=total, shares=shares, notes=notes)


def _round_to_cents(total, people, exact):
    """Round down to cents, then hand the leftover cents to the largest remainders.

    This guarantees the shares add up to exactly `total`.
    """
    floored = {k: v.quantize(CENT, rounding=ROUND_DOWN) for k, v in exact.items()}
    leftover = int(((total - sum(floored.values())) / CENT).to_integral_value())
    if sum(exact.values()) == 0:
        leftover = 0
    order = sorted(exact, key=lambda k: (exact[k] - floored[k], exact[k]), reverse=True)
    for key in order[:max(leftover, 0)]:
        floored[key] += CENT
    for key, person in people.items():
        person.amount = floored[key]
