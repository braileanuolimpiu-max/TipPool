from datetime import date
from decimal import Decimal

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from tips.models import Business, Location, Night, Tip
from tips.tests.helpers import hire, make_manager


class OverviewTests(TestCase):
    def setUp(self):
        self.first = Location.default()
        self.second = Location.objects.create(business=self.first.business, name="Second Spot", slug="second")
        other = Business.objects.create(name="Rival")
        Location.objects.create(business=other, name="Rival Cafe", slug="rival")
        self.client.force_login(make_manager("owner"))

    def test_lists_every_location_of_the_business_only(self):
        Tip.objects.create(night=Night.current(self.second), amount=Decimal("12"), paypal_order_id="X",
                           status="completed")
        Night.objects.create(location=self.first, date=date(2026, 10, 14), status=Night.Status.CLOSED)
        page = self.client.get(reverse("overview"))
        self.assertContains(page, "Second Spot")
        self.assertContains(page, "$12.00")
        self.assertContains(page, "needs who worked")
        self.assertNotContains(page, "Rival Cafe")
        self.assertContains(self.client.get(reverse("dashboard")), "All locations")

    def test_flag_emails_the_managers(self):
        maria = hire("Maria", "server")
        self.client.post(reverse("my_tips", args=[maria.access_token]),
                         {"location": self.first.pk, "text": "Can we talk about Friday?"})
        self.assertIn("owner@example.com", mail.outbox[-1].to)
        self.assertIn("Can we talk about Friday?", mail.outbox[-1].body)
