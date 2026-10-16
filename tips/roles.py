"""Role presets, so any tipped business starts with roles that fit it.

Each role: (name, aliases, no_tips). `aliases` are other words people use for the role (they help the
rule reader), `no_tips` marks managers and owners, who may never take from an employee tip pool (US FLSA).
Each preset also has example rules for the rules page, written with its own role names.
"""
import re

MANAGER = ("Manager", "owner, supervisor, me, myself", True)

PRESETS = {
    "restaurant": {
        "label": "Restaurant",
        "roles": [
            ("Server", "waiter, waitress, waitstaff", False),
            ("Bartender", "bar, bar staff", False),
            ("Busser", "busboy, bus staff", False),
            ("Host", "hostess", False),
            ("Food runner", "runner", False),
            ("Cook", "chef, kitchen, kitchen staff, line cook", False),
            ("Dishwasher", "", False),
            MANAGER,
        ],
        "examples": [
            ("Bar + bussers", "Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours worked."),
            ("Everyone by hours", "Everyone shares the tips by hours worked: servers, bartenders and bussers."),
            ("Kitchen included", "Cooks get 15%, hosts get 5% split equally, the rest goes to servers by hours."),
        ],
    },
    "cafe": {
        "label": "Café or coffee shop",
        "roles": [
            ("Barista", "", False),
            ("Cashier", "till, register", False),
            ("Baker", "pastry", False),
            ("Cook", "kitchen, kitchen staff", False),
            MANAGER,
        ],
        "examples": [
            ("Everyone by hours", "Everyone shares the tips by hours worked: baristas, cashiers and bakers."),
            ("Front + back", "Bakers and cooks get 15%, the rest goes to baristas and cashiers by hours."),
        ],
    },
    "bar": {
        "label": "Bar or club",
        "roles": [
            ("Bartender", "bar staff, mixologist", False),
            ("Barback", "", False),
            ("Server", "waiter, waitress, cocktail server", False),
            ("Door staff", "bouncer, security, door", False),
            MANAGER,
        ],
        "examples": [
            ("Barbacks 15%", "Barbacks get 15% split equally, the rest goes to bartenders and servers by hours."),
            ("Everyone by hours", "Everyone shares the tips by hours worked: bartenders, barbacks and servers."),
        ],
    },
    "salon": {
        "label": "Salon or spa",
        "roles": [
            ("Stylist", "hairdresser, hair stylist, barber, colorist", False),
            ("Nail tech", "nail technician, manicurist", False),
            ("Massage therapist", "therapist, masseuse", False),
            ("Assistant", "shampoo assistant, junior", False),
            ("Receptionist", "front desk", False),
            MANAGER,
        ],
        "examples": [
            ("Assistants 15%", "Assistants get 15% split equally, receptionists 5%, the rest goes to stylists by hours."),
            ("Everyone by hours", "Everyone shares the tips by hours worked: stylists, assistants and receptionists."),
        ],
    },
    "hotel": {
        "label": "Hotel",
        "roles": [
            ("Housekeeper", "housekeeping, room attendant, maid", False),
            ("Front desk agent", "front desk, reception, receptionist", False),
            ("Bellhop", "bell staff, porter", False),
            ("Concierge", "", False),
            ("Valet", "parking", False),
            MANAGER,
        ],
        "examples": [
            ("Housekeeping first", "Housekeepers get 50% by hours, bellhops 20% split equally, the rest goes to the front desk agents by hours."),
            ("Everyone by hours", "Everyone shares the tips by hours worked: housekeepers, bellhops, valets and concierges."),
        ],
    },
    "delivery": {
        "label": "Delivery or moving",
        "roles": [
            ("Driver", "courier, delivery driver", False),
            ("Helper", "mover, loader", False),
            ("Dispatcher", "dispatch", False),
            MANAGER,
        ],
        "examples": [
            ("Crew by hours", "Drivers and helpers share the tips by hours worked."),
            ("Dispatch 10%", "Dispatchers get 10% split equally, the rest goes to drivers and helpers by hours."),
        ],
    },
}

GENERIC_EXAMPLES = [("Everyone by hours", "Everyone shares the tips by hours worked.")]


def examples_for(business_type):
    preset = PRESETS.get(business_type)
    return preset["examples"] if preset else GENERIC_EXAMPLES


def plural(name):
    """'Bartender' -> 'Bartenders', 'Front desk agent' -> 'Front desk agents', 'Nail tech' -> 'Nail techs'."""
    if re.search(r"(s|x|z|ch|sh)$", name, re.I):
        return name + "es"
    if re.search(r"[^aeiou]y$", name, re.I):
        return name[:-1] + "ies"
    if re.search(r"staff$", name, re.I):
        return name
    return name + "s"


def word_pattern(role):
    """Regex for every way a role can be written in the rules: its name, plural and aliases."""
    words = [role.name] + [a.strip() for a in role.aliases.split(",") if a.strip()]
    variants = set()
    for word in words:
        variants.add(re.escape(word.lower()))
        variants.add(re.escape(plural(word).lower()))
    # longest first, so "front desk agents" wins over "front desk"
    return "|".join(sorted(variants, key=len, reverse=True)).replace(r"\ ", r"\s+")


def roles_in_use():
    """Role keys still on someone's team (a role can't be removed while an active person has it)."""
    from .models import StaffMember
    return set(StaffMember.objects.filter(active=True).values_list("default_role", flat=True))


def add_role(name, aliases="", no_tips=False):
    from .models import Role
    name = " ".join(name.split())[:40]
    existing = Role.objects.filter(name__iexact=name).first()
    if existing:
        return existing, False
    last = Role.objects.order_by("-position").first()
    role = Role.objects.create(key=Role.new_key(name), name=name, aliases=aliases[:200], no_tips=no_tips,
                               position=(last.position + 1) if last else 0)
    return role, True


def apply_preset(business_type):
    """Switch to a preset's roles. Roles someone on the team still has are kept.

    Returns the names of roles that were kept although they're not in the preset.
    """
    from .models import Role
    preset = PRESETS[business_type]
    wanted = {name.lower() for name, _, _ in preset["roles"]}
    in_use = roles_in_use()
    kept = []
    for role in Role.objects.all():
        if role.name.lower() in wanted:
            continue
        if role.key in in_use:
            kept.append(role.name)
        else:
            role.delete()
    for position, (name, aliases, no_tips) in enumerate(preset["roles"]):
        role, _ = add_role(name, aliases, no_tips)
        role.position = position
        role.save(update_fields=["position"])
    for role in Role.objects.filter(name__in=kept):
        role.position = len(preset["roles"]) + 1
        role.save(update_fields=["position"])
    return kept
