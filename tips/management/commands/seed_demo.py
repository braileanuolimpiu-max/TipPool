"""Fill the database with a demo restaurant: a manager login, staff and starter tip rules.

    python manage.py seed_demo

Staff come from DEMO_STAFF in .env, format:  Name:role:paypal_email,Name:role:paypal_email,...
An empty email falls back to NAME_EMAIL in .env (e.g. MARIA_EMAIL=...).
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tips import ai
from tips.models import Location, Membership, Role, StaffMember, TipRuleSet

DEFAULT_STAFF = "Maria:server:,John:server:,Lea:server:,Alex:bartender:,Sam:busser:"
DEFAULT_RULES = "Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours worked."


class Command(BaseCommand):
    help = "Create a demo manager login, staff and tip rules."

    def handle(self, *args, **options):
        username = os.environ.get("DEMO_USERNAME", "manager")
        password = os.environ.get("DEMO_PASSWORD", "tippool-demo")
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={"is_staff": True, "is_superuser": True})
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(f"Manager login created: {username} / {password}")
        else:
            self.stdout.write(f"Manager login '{username}' already exists (password unchanged)")

        location = Location.default()
        for entry in os.environ.get("DEMO_STAFF", DEFAULT_STAFF).split(","):
            parts = [p.strip() for p in entry.split(":")]
            if len(parts) != 3 or not parts[0]:
                continue
            name, role, email = parts
            if not Role.objects.filter(location=location, key=role).exists():
                self.stderr.write(f"Skipping {name}: unknown role '{role}'")
                continue
            # also accept one line per person in .env, e.g. MARIA_EMAIL=...
            email = email or os.environ.get(f"{name.upper()}_EMAIL", "") or f"{name.lower()}@example.com"
            member, made = StaffMember.objects.update_or_create(name=name, defaults={"paypal_email": email})
            Membership.objects.update_or_create(staff=member, location=location,
                                                defaults={"default_role": role, "active": True})
            self.stdout.write(f"{'Added' if made else 'Updated'} {name} ({role}) -> {email}")

        if not TipRuleSet.active(location):
            parsed = ai.parse_rules(DEFAULT_RULES, location)
            TipRuleSet.objects.create(location=location, text=DEFAULT_RULES, rules=parsed["rules"], summary=parsed["summary"],
                                      warnings=parsed["warnings"])
            self.stdout.write("Starter tip rules created.")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
