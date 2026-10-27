"""Sample data, so anyone (a judge, a new owner) can see TipPool working in a minute.

- "Try the demo" makes a fresh demo business with a few weeks of history and signs you in.
- An empty real business can load the same sample data from its dashboard.
Demo businesses only ever use the PayPal sandbox, and are deleted after a week.
"""
import random
import secrets
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import ai, roles as role_presets, services
from .autoclose import location_tz
from .models import (Business, ClockEntry, CloseTime, Location, Manager, Membership, Message, Night, PayoutBatch,
                     PayoutItem, Shift, StaffMember, Tip, TipRuleSet)

DEMO_DAYS = 21
RULES = ("Bartenders get 20%, bussers get 10% split equally, hosts get 5% split equally, the rest goes to servers "
         "by hours worked.")
TEAM = [("Maria", "server"), ("John", "server"), ("Lea", "server"), ("Alex", "bartender"), ("Sam", "busser"),
        ("Nina", "host")]
GUESTS = ["Pat G.", "Chris L.", "Dana M.", "Alex R.", "Jamie T.", "Morgan K.", "Robin S.", "Taylor B."]


def _email(name):
    """Your PayPal sandbox personal accounts from .env (MARIA_EMAIL=...), so demo payouts really arrive."""
    from .platform import demo_staff_email
    return demo_staff_email(name) or f"{name.lower()}.demo@example.com"


def fill(location, seed=7):
    """Sample team, rules and history for one location. Returns the number of shifts made."""
    with ai.offline():  # dozens of sample splits shouldn't cost dozens of AI calls
        return _fill(location, seed)


@transaction.atomic
def _fill(location, seed):
    rng = random.Random(seed)
    business = location.business
    role_presets.apply_preset(location, "restaurant")
    location.tip_mode = Location.TipMode.BOTH
    location.auto_close_enabled = True
    location.save()
    for day in range(7):
        if day != 0:  # closed on Mondays
            CloseTime.objects.update_or_create(location=location, weekday=day,
                                               defaults={"time": time(23, 0), "enabled": True})
    parsed = ai.parse_rules(RULES, location)
    rules = TipRuleSet.objects.create(location=location, text=RULES, rules=parsed["rules"], summary=parsed["summary"],
                                      warnings=parsed["warnings"])
    people = []
    for name, role in TEAM:
        person, _ = StaffMember.objects.get_or_create(business=business, name=name,
                                                      defaults={"paypal_email": _email(name)})
        if business.is_demo:
            # only in the demo: sample people in a real business are never paid as if they'd said "pay me here"
            # (their addresses are test accounts, and the business may go live later)
            person.payout_confirmed_at = timezone.now()
            person.save(update_fields=["payout_confirmed_at"])
        Membership.objects.update_or_create(staff=person, location=location, defaults={
            "default_role": role, "active": True,
            "schedule": {str(d): 6 for d in range(1, 6)} if role == "server" else {}})
        people.append((person, role))

    tz = location_tz(location)
    today = timezone.now().astimezone(tz).date()
    made = 0
    for back in range(DEMO_DAYS, 1, -1):
        day = today - timedelta(days=back)
        if day.weekday() == 0:
            continue
        night = Night.objects.create(location=location, date=day)
        busy = 1.6 if day.weekday() in (4, 5) else 1.0
        _tips(night, rng, busy)
        _roster(night, people, rng)
        services.build_split(night, rules)
        _mark_paid(night)
        made += 1

    # yesterday: closed, split, waiting for a manager, with one question from the team
    yesterday = Night.objects.create(location=location, date=today - timedelta(days=1))
    _tips(yesterday, rng, 1.3)
    _roster(yesterday, people, rng)
    services.build_split(yesterday, rules)
    Message.objects.create(staff=people[1][0], location=location, night=yesterday,
                           text="I stayed an hour after close to help clean up. Can you add it?")
    # today: open, a few tips in, people on the clock
    tonight = Night.current(location)
    _tips(tonight, rng, 0.55, closed=False)
    start = timezone.now() - timedelta(hours=3)
    for person, role in people[:4]:
        ClockEntry.objects.create(staff=person, night=tonight, started_at=start + timedelta(minutes=rng.randint(0, 40)))
    return made + 2


def _tips(night, rng, busy, closed=True):
    """Guest tips through the evening, plus the card-machine and cash totals added at close-out.
    Tonight (not closed yet): only guest tips, from the last few hours."""
    tz = location_tz(night.location)
    now = timezone.now()
    # past shifts are busy around this time of day, so "normal by now" on the live board makes sense
    # whenever someone opens the demo
    local_now = now.astimezone(tz)
    since_midnight = local_now - datetime.combine(local_now.date(), time(0), tzinfo=tz)
    evening = datetime.combine(night.date, time(0), tzinfo=tz) + since_midnight - timedelta(hours=3)
    first = Tip.objects.filter(night=night).count()
    for n in range(int(rng.randint(9, 15) * busy)):
        amount = Decimal(rng.choice([3, 5, 5, 8, 10, 10, 12, 15, 20])) + Decimal(rng.choice([0, 0, 50])) / 100
        Tip.objects.create(night=night, amount=amount, status=Tip.Status.COMPLETED,
                           paypal_order_id=f"DEMO-{night.pk}-{n}-{secrets.token_hex(3)}",
                           payer_name=rng.choice(GUESTS), fee=(amount * Decimal("0.0349") + Decimal("0.49")).quantize(Decimal("0.01")))
    if closed:
        Tip.objects.create(night=night, amount=Decimal(rng.randint(40, 120)), source=Tip.Source.CARD,
                           status=Tip.Status.COMPLETED, note="Card machine total")
        Tip.objects.create(night=night, amount=Decimal(rng.randint(15, 60)), source=Tip.Source.CASH,
                           status=Tip.Status.COMPLETED, note="Cash jar")
    # spread the guest tips over the evening (never in the future), card and cash at closing
    for i, tip in enumerate(Tip.objects.filter(night=night).order_by("pk")[first:]):
        if not closed:
            at = now - timedelta(minutes=rng.randint(4, 170))
        elif tip.source == Tip.Source.PAYPAL:
            at = evening + timedelta(minutes=rng.randint(0, 330))
        else:
            at = evening + timedelta(hours=5, minutes=30)
        Tip.objects.filter(pk=tip.pk).update(created_at=min(at, now - timedelta(minutes=i + 1)))


def _roster(night, people, rng):
    for person, role in people:
        if role == "host" and rng.random() < .5:
            continue
        if role == "server" and rng.random() < .15:
            continue
        Shift.objects.create(night=night, staff=person, role=role, hours=Decimal(rng.choice([4, 5, 5.5, 6, 6.5, 7, 8])))


def _mark_paid(night):
    """Past demo shifts look paid (no money moves for history)."""
    batch = PayoutBatch.objects.create(night=night, sender_batch_id=f"demo-{night.pk}-{secrets.token_hex(4)}",
                                       paypal_batch_id=f"DEMO{night.pk:06d}", status="SUCCESS")
    for share in night.shares.filter(amount__gt=0).select_related("staff"):
        PayoutItem.objects.create(batch=batch, share=share, sender_item_id=f"{batch.sender_batch_id}-{share.staff_id}",
                                  receiver=share.staff.payout_to, status="SUCCESS", transaction_id=f"DEMO-{share.pk}")
    night.status = Night.Status.PAID
    night.closed_at = datetime.combine(night.date, time(23, 0), tzinfo=location_tz(night.location))
    night.save(update_fields=["status", "closed_at"])


@transaction.atomic
def create_demo():
    """A fresh demo business and a login for it. Returns the user."""
    User = get_user_model()
    tag = secrets.token_hex(4)
    business = Business.objects.create(name="Luna Bistro (demo)", is_demo=True)
    location = Location.objects.create(business=business, name="Luna Bistro", slug=Location.new_slug(f"luna-demo-{tag}"))
    user = User.objects.create_user(username=f"demo-{tag}", password=secrets.token_urlsafe(24), first_name="Demo",
                                    last_name="Manager")
    Manager.objects.create(user=user, business=business, is_owner=True)
    fill(location)
    return user


def cleanup(days=7):
    """Delete demo businesses (and their logins) older than `days`."""
    old = list(Business.objects.filter(is_demo=True, created_at__lt=timezone.now() - timedelta(days=days)))
    for business in old:
        with transaction.atomic():
            get_user_model().objects.filter(manager__business=business).delete()
            # shifts protect the people in them (real payout records never vanish), so demo shifts go first
            Night.objects.filter(location__business=business).delete()
            business.delete()
    return len(old)
