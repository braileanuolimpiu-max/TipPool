"""How big one business can grow: far above any real team, low enough that nobody can fill the database.

Active numbers are what a business works with; the "ever" numbers also count archived locations and people who
left, so archiving and adding again can't grow without end."""
from .models import Location, Membership, StaffMember

MAX_LOCATIONS = 50          # open at once, per business (a large restaurant group)
MAX_LOCATIONS_EVER = 200    # including archived ones
MAX_TEAM = 250              # people on one location's team at once (a large hotel has fewer tipped staff)
MAX_PEOPLE_EVER = 10000     # people a business has ever added (years of turnover across 50 locations)


def cant_add_location(business):
    """Why this business can't open another location now, or ""."""
    if Location.objects.filter(business=business, archived=False).count() >= MAX_LOCATIONS:
        return (f"A business can have up to {MAX_LOCATIONS} open locations. Archive one you no longer use, "
                "or contact us if you need more.")
    if Location.objects.filter(business=business).count() >= MAX_LOCATIONS_EVER:
        return f"This business has reached {MAX_LOCATIONS_EVER} locations, archived ones included. Contact us if you need more."
    return ""


def cant_restore_location(business):
    if Location.objects.filter(business=business, archived=False).count() >= MAX_LOCATIONS:
        return f"A business can have up to {MAX_LOCATIONS} open locations. Archive another one first."
    return ""


def cant_add_person(location, email):
    """Why the person with this PayPal email can't join this location's team now, or ""."""
    person = StaffMember.objects.filter(business=location.business, paypal_email__iexact=email).first()
    if person is not None and Membership.objects.filter(staff=person, location=location, active=True).exists():
        return ""  # already on the team: nothing grows
    if team_full(location):
        return team_full(location)
    if person is None and StaffMember.objects.filter(business=location.business).count() >= MAX_PEOPLE_EVER:
        return f"This business has added {MAX_PEOPLE_EVER} people over time, the most it can. Contact us if you need more."
    return ""


def team_full(location):
    if Membership.objects.filter(location=location, active=True).count() >= MAX_TEAM:
        return (f"{location.display_name} already has {MAX_TEAM} people on its team, the most one location can have. "
                "Take people who left off the team first.")
    return ""
