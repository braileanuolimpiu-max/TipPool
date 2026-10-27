from django.contrib.auth import get_user_model
from django.utils import timezone

from tips.models import Location, Manager, Membership, StaffMember


def hire(name, role, location=None, email=None, confirmed=True):
    """A person on a location's team (the first location unless given). `confirmed`: they already said
    "yes, pay me here" on their tips page."""
    location = location or Location.default()
    person = StaffMember.objects.create(business=location.business, name=name,
                                        paypal_email=email or f"{name.lower()}@example.com",
                                        payout_confirmed_at=timezone.now() if confirmed else None)
    Membership.objects.create(staff=person, location=location, default_role=role)
    return person


def make_manager(username, business=None, owner=True):
    """A login that manages a business (the first location's business unless given)."""
    business = business or Location.default().business
    user = get_user_model().objects.create_user(username, email=f"{username}@example.com", password="pw")
    Manager.objects.create(user=user, business=business, is_owner=owner)
    return user


def make_operator(username, business=None):
    """A login that runs the site: an owner with two-step sign-in on (Platform settings need it)."""
    user = make_manager(username, business)
    user.is_superuser = True
    user.save()
    user.manager.totp_enabled = True
    user.manager.save(update_fields=["totp_enabled"])
    return user
