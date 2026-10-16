from tips.models import Location, Membership, StaffMember


def hire(name, role, location=None, email=None):
    """A person on a location's team (the first location unless given)."""
    location = location or Location.default()
    person = StaffMember.objects.create(name=name, paypal_email=email or f"{name.lower()}@example.com")
    Membership.objects.create(staff=person, location=location, default_role=role)
    return person
