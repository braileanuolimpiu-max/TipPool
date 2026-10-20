"""A real end-to-end run against the PayPal sandbox (no mocks), to prove the money path works.

    python manage.py sandbox_check            # sign-in, balance, a real tip order, a real payout batch
    python manage.py sandbox_check --wait 300 # also wait for you to approve the tip in a browser, then capture it

It makes its own "Sandbox check" demo business (removed after a week with the other demos), so real data
is never touched. Staff are paid at the sandbox personal accounts in .env (MARIA_EMAIL, JOHN_EMAIL, ...).
"""
import time
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tips import ai, check, roles as role_presets, services
from tips.models import Business, Location, Membership, Night, Shift, StaffMember, Tip, TipRuleSet
from tips.paypal import SANDBOX_API, PayPalError, get_client

TEAM = [("Maria", "server", 6), ("John", "server", 5), ("Lea", "server", 4), ("Alex", "bartender", 6),
        ("Sam", "busser", 5)]
RULES = "Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours worked."


class Command(BaseCommand):
    help = "Run a real tip and payout through the PayPal sandbox and report each step."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=int, default=0,
                            help="Seconds to wait for you to approve the tip in a browser (0 = skip the guest step).")
        parser.add_argument("--amount", default="5.00", help="Tip amount for the guest step.")

    def handle(self, *args, **opts):
        from tips.platform import paypal_platform
        if paypal_platform()[2].rstrip("/") != SANDBOX_API:
            raise CommandError("Only for the PayPal sandbox: TipPool's own PayPal is connected with real-money codes.")
        self.failed = 0
        client = get_client(None)

        self.step("PayPal sign-in (OAuth client credentials)", lambda: client.access_token() and "token ok")
        self.step("Balance check (Reporting API)", lambda: f"USD {client.available_balance('USD')}")

        business = Business.objects.create(name=f"Sandbox check {timezone.now():%b %d %H:%M}", is_demo=True)
        location = Location.objects.create(business=business, name="Sandbox check",
                                           slug=Location.new_slug("sandbox-check"))
        role_presets.apply_preset(location, "restaurant")
        with ai.offline():
            parsed = ai.parse_rules(RULES, location)
        rules = TipRuleSet.objects.create(location=location, text=RULES, rules=parsed["rules"],
                                          summary=parsed["summary"], warnings=parsed["warnings"])
        people = []
        for name, role, hours in TEAM:
            from tips.platform import demo_staff_email
            email = demo_staff_email(name)
            if not email:
                continue
            person = StaffMember.objects.create(business=business, name=name, paypal_email=email,
                                                payout_confirmed_at=timezone.now())
            Membership.objects.create(staff=person, location=location, default_role=role)
            people.append((person, role, hours))
        if not people:
            raise CommandError("Add your sandbox personal accounts to .env (MARIA_EMAIL=..., JOHN_EMAIL=...).")
        night = Night.current(location)

        # a guest tip: a real order (Orders v2), approved by a person, captured by us
        order_id = self.step("Create a real tip order (Orders v2)",
                             lambda: services.create_tip_order(location, Decimal(opts["amount"])))
        if order_id:
            order = client.get_order(order_id)
            link = next((l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")), "")
            self.stdout.write(f"    order {order_id} is {order['status']}. Approve it as a sandbox buyer:\n    {link}")
            if opts["wait"]:
                self.step("Guest approves, then we capture", lambda: self.wait_and_capture(client, order_id,
                                                                                           opts["wait"]))

        # card and cash tips the manager adds at close-out
        services.add_manual_tip(night, Decimal("40.00"), Tip.Source.CASH, note="Cash jar (sandbox check)")
        for person, role, hours in people:
            Shift.objects.create(night=night, staff=person, role=role, hours=Decimal(hours))
        night.status = Night.Status.CLOSED
        night.closed_at = timezone.now()
        night.save()

        def split():
            with ai.offline():
                services.build_split(night, rules)
            shares = ", ".join(f"{s.staff.name} ${s.amount}" for s in night.shares.select_related("staff"))
            findings = [f["text"] for f in (night.review_check or {}).get("findings", [])]
            blocking = check.blocking(night)
            return f"{shares}" + (f" | check: {'; '.join(findings)}" if findings else "") + \
                   (" | BLOCKED" if blocking else "")
        self.step("Split the shift (exact cents) + close-out check", split)

        batch = self.step("Pay the team (Payouts API batch)",
                          lambda: services.pay_night(Night.objects.get(pk=night.pk)))
        if batch:
            self.step("PayPal processes the payouts", lambda: self.wait_for_payouts(batch))

        night.refresh_from_db()
        self.stdout.write(f"\nShift status: {night.status}. Business: {business.name} (demo, removed in 7 days).")
        if self.failed:
            raise CommandError(f"{self.failed} step(s) failed.")
        self.stdout.write(self.style.SUCCESS("Sandbox check passed."))

    def step(self, label, fn):
        try:
            result = fn()
        except (PayPalError, services.TipPoolError, TimeoutError) as exc:
            self.failed += 1
            detail = getattr(exc, "debug_id", None)
            self.stdout.write(self.style.ERROR(f"FAIL  {label}: {exc}" + (f" (debug_id {detail})" if detail else "")))
            return None
        shown = result if isinstance(result, str) else getattr(result, "pk", result)
        self.stdout.write(self.style.SUCCESS(f"OK    {label}") + f"  {shown}")
        return result

    def wait_and_capture(self, client, order_id, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            status = client.get_order(order_id)["status"]
            if status in ("APPROVED", "COMPLETED"):
                tip = services.capture_tip(order_id)
                return f"tip {tip.status}, PayPal fee ${tip.fee}, capture {tip.paypal_capture_id}"
            time.sleep(5)
        raise TimeoutError(f"nobody approved the order in {seconds}s")

    def wait_for_payouts(self, batch, seconds=120):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            services.refresh_batch(batch)
            items = list(batch.items.all())
            if items and all(i.status in services.ITEM_DONE or i.failed for i in items):
                return f"batch {batch.paypal_batch_id} {batch.status}: " + \
                       ", ".join(f"{i.receiver} {i.status}" for i in items)
            time.sleep(6)
        raise TimeoutError(f"batch {batch.paypal_batch_id} still {batch.status} after {seconds}s")
