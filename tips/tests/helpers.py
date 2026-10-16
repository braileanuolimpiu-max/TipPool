from django.contrib.auth import get_user_model

from tips.models import Location, Manager, Membership, StaffMember


def hire(name, role, location=None, email=None):
    """A person on a location's team (the first location unless given)."""
    location = location or Location.default()
    person = StaffMember.objects.create(business=location.business, name=name,
                                        paypal_email=email or f"{name.lower()}@example.com")
    Membership.objects.create(staff=person, location=location, default_role=role)
    return person


def make_manager(username, business=None, owner=True):
    """A login that manages a business (the first location's business unless given)."""
    business = business or Location.default().business
    user = get_user_model().objects.create_user(username, email=f"{username}@example.com", password="pw")
    Manager.objects.create(user=user, business=business, is_owner=owner)
    return user
