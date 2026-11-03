"""The interactive tables (AG Grid): the rows each page hands the grid, and how they reach the page."""
import json
import re
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from tips import grids
from tips.models import Change, Location, Night, Share, Tip
from tips.tests.helpers import hire, make_manager


class GridPagesTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.maria = hire("Maria", "server")
        self.client.force_login(make_manager("owner"))
        self.night = Night.objects.create(location=self.location, date="2026-10-30", status=Night.Status.REVIEW)
        Tip.objects.create(night=self.night, amount=Decimal("80"), paypal_order_id="G1", status="completed")
        Share.objects.create(night=self.night, staff=self.maria, role="server", hours=Decimal("6"), amount=Decimal("80"))

    def rows(self, res, element_id):
        found = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', res.content.decode(), re.S)
        self.assertIsNotNone(found, element_id)
        return json.loads(found.group(1))

    def test_reports_hand_the_grid_numbers_and_load_it_with_the_nonce(self):
        res = self.client.get(reverse("reports"), {"period": "custom", "from": "2026-10-01", "to": "2026-10-31"})
        people = self.rows(res, "grid-people")
        self.assertEqual(people[0]["name"], "Maria")
        self.assertEqual(people[0]["total"], 80.0)  # a number, so the grid sorts and totals it
        nonce = res.wsgi_request.csp_nonce
        self.assertContains(res, f'<script nonce="{nonce}" src="/static/vendor/ag-grid/ag-grid-community-36.2.0.min.noStyle.js" defer>', html=False)
        self.assertContains(res, 'data-grid="people"')
        self.assertContains(res, "<table class=\"data\">")  # the plain table is still there without JavaScript
        # one location: no Location column in the per-shift grid (the plain table hides it too)
        self.assertNotIn("location", self.rows(res, "grid-shifts")[0])

    def test_past_shifts_link_to_our_own_pages(self):
        res = self.client.get(reverse("history"))
        row = self.rows(res, "grid-history")[0]
        self.assertEqual(row["url"], reverse("review", args=[self.night.pk]))
        self.assertEqual((row["status"], row["action"]), ("Ready to approve", "Check and pay"))

    def test_activity_text_reaches_the_page_as_data_not_html(self):
        Change.log("<img src=x onerror=alert(1)>", "</script><script>alert(2)</script>", location=self.location)
        res = self.client.get(reverse("activity"))
        body = res.content.decode()
        self.assertNotIn("<script>alert(2)", body)  # json_script escapes < and >, so nothing can close the tag
        row = next(r for r in self.rows(res, "grid-activity") if "alert(2)" in r["what"])
        self.assertEqual(row["who"], "<img src=x onerror=alert(1)>")  # kept as text; the grid shows it as text

    def test_pages_without_rows_have_no_grid(self):
        res = self.client.get(reverse("activity"), {"q": "nothing like this"})
        self.assertNotContains(res, 'data-grid="activity"')


class GridRowTests(TestCase):
    def test_a_paid_demo_shift_reads_sample_and_links_to_its_payout(self):
        location = Location.default()
        night = Night.objects.create(location=location, date=date(2026, 10, 29), status=Night.Status.PAID)
        batch = type("Batch", (), {"paypal_batch_id": "DEMO-1"})()
        row = grids.history_rows([{"night": night, "batch": batch, "total": Decimal("10"), "people": 2}])[0]
        self.assertEqual((row["statusKey"], row["status"]), ("sample", "Sample"))
        self.assertEqual(row["url"], reverse("payout_status", args=[night.pk]))
        self.assertEqual((row["label"], row["short"]), ("Thu, Oct 29", "Oct 29"))
