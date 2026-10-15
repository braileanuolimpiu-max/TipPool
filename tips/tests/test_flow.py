"""End-to-end flow with PayPal faked: tip -> capture -> close night -> split -> pay -> status."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai
from tips.models import Night, PayoutItem, StaffMember, Tip, TipRuleSet


class FakePayPal:
    def __init__(self):
        self.orders = 0
        self.payout_bodies = []

    def create_order(self, amount, currency="USD", description="Tip", reference_id=None):
        self.orders += 1
        return {"id": f"ORDER{self.orders}"}

    def capture_order(self, order_id):
        return {"purchase_units": [{"payments": {"captures": [{"id": f"CAP-{order_id}", "status": "COMPLETED"}]}}],
                "payer": {"name": {"given_name": "Pat", "surname": "Guest"}}}

    def create_payout_batch(self, sender_batch_id, items, subject="", currency="USD"):
        self.payout_bodies.append(items)
        return {"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "PENDING"}}

    def get_payout_batch(self, batch_id):
        items = self.payout_bodies[-1]
        return {"batch_header": {"batch_status": "SUCCESS"},
                "items": [{"payout_item": {"sender_item_id": i["sender_item_id"]}, "payout_item_id": f"I{n}",
                           "transaction_id": f"T{n}", "transaction_status": "SUCCESS"} for n, i in enumerate(items)]}


@override_settings(AI_MODE="mock")
class FullFlowTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        user = get_user_model().objects.create_user("manager", password="pw")
        self.client.force_login(user)
        self.staff = {n: StaffMember.objects.create(name=n, default_role=r, paypal_email=f"{n.lower()}@example.com")
                      for n, r in [("Maria", "server"), ("John", "server"), ("Alex", "bartender"), ("Sam", "busser")]}
        parsed = ai.parse_rules("Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours")
        TipRuleSet.objects.create(text="x", rules=parsed["rules"], summary=parsed["summary"])

    def tip(self, amount):
        res = self.client.post(reverse("api_create_order"), data=json.dumps({"amount": amount, "table": "4"}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 200)
        order_id = res.json()["orderId"]
        res = self.client.post(reverse("api_capture_order", args=[order_id]))
        self.assertEqual(res.status_code, 200)
        return order_id

    def test_tip_to_payout(self):
        self.tip("60.00")
        self.tip("40.00")
        night = Night.objects.get(status=Night.Status.OPEN)
        self.assertEqual(night.tip_total, Decimal("100.00"))
        self.assertEqual(Tip.objects.get(paypal_order_id="ORDER1").payer_name, "Pat Guest")

        form = {}
        for name, hours in [("Maria", "6"), ("John", "4"), ("Alex", "5"), ("Sam", "5")]:
            member = self.staff[name]
            form.update({f"worked_{member.pk}": "on", f"hours_{member.pk}": hours, f"role_{member.pk}": member.default_role})
        res = self.client.post(reverse("close_night"), form)
        self.assertRedirects(res, reverse("review", args=[night.pk]))

        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)
        shares = {s.staff.name: s.amount for s in night.shares.all()}
        self.assertEqual(shares, {"Alex": Decimal("20.00"), "Sam": Decimal("10.00"),
                                  "Maria": Decimal("42.00"), "John": Decimal("28.00")})
        self.assertTrue(all(s.explanation for s in night.shares.all()))

        # a new tip after closing goes to a fresh night
        self.tip("5.00")
        self.assertEqual(Night.objects.get(status=Night.Status.OPEN).tip_total, Decimal("5.00"))

        res = self.client.post(reverse("pay", args=[night.pk]))
        self.assertRedirects(res, reverse("payout_status", args=[night.pk]))
        sent = self.paypal.payout_bodies[0]
        self.assertEqual(sum(i["amount"] for i in sent), Decimal("100.00"))
        self.assertEqual({i["receiver"] for i in sent}, {"maria@example.com", "john@example.com",
                                                         "alex@example.com", "sam@example.com"})

        # paying twice is refused
        self.client.post(reverse("pay", args=[night.pk]))
        self.assertEqual(len(self.paypal.payout_bodies), 1)

        res = self.client.get(reverse("payout_status_json", args=[night.pk]))
        self.assertEqual(res.json()["night_status"], "paid")
        self.assertEqual(set(PayoutItem.objects.values_list("status", flat=True)), {"SUCCESS"})

    def test_bad_tip_amount(self):
        res = self.client.post(reverse("api_create_order"), data=json.dumps({"amount": "0.50"}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_pages_render(self):
        self.tip("10.00")
        for name in ["dashboard", "tip_page", "rules", "staff_list", "close_night", "qr_page"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        member = self.staff["Maria"]
        self.assertEqual(self.client.get(reverse("my_tips", args=[member.access_token])).status_code, 200)

    def test_webhook_marks_tip_completed(self):
        night = Night.current()
        Tip.objects.create(night=night, amount=Decimal("7.00"), paypal_order_id="ORD-W")
        event = {"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
                 "resource": {"id": "CAP-W", "supplementary_data": {"related_ids": {"order_id": "ORD-W"}}}}
        with override_settings(DEBUG=True, PAYPAL_WEBHOOK_ID=""):
            res = self.client.post(reverse("paypal_webhook"), data=json.dumps(event), content_type="application/json")
            self.assertEqual(res.status_code, 200)
            # duplicate delivery is ignored
            self.client.post(reverse("paypal_webhook"), data=json.dumps(event), content_type="application/json")
        tip = Tip.objects.get(paypal_order_id="ORD-W")
        self.assertEqual((tip.status, tip.paypal_capture_id), ("completed", "CAP-W"))
