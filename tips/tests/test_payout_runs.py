"""Payouts through Render Workflows: the checks run when the manager pays, a workflow run sends the batch and
follows it, a retry never pays twice, and without Render the payout goes out right away as before."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips import ai, payout_runs, services
from tips.models import Location, Night, PayoutRun, Shift, Tip, TipRuleSet
from tips.paypal import PayPalError
from tips.tests.helpers import hire, make_manager, make_operator
from tips.tests.test_money import FakePayPal

ON_RENDER = {"RENDER_API_KEY": "rnd_test", "TIPPOOL_WORKFLOW_SLUG": "tippool-payouts"}


class PayPalWithStatus(FakePayPal):
    """Also answers "how is this batch doing?", with every payout arrived (or one still on its way)."""
    item_status = "SUCCESS"

    def __init__(self):
        super().__init__()
        self.sent_ids = []

    def create_payout_batch(self, sender_batch_id, items, subject="", currency="USD"):
        self.sent_ids.append(sender_batch_id)
        self.last_items = items
        return super().create_payout_batch(sender_batch_id, items, subject, currency)

    def get_payout_batch(self, batch_id):
        return {"batch_header": {"batch_status": "SUCCESS"},
                "items": [{"payout_item": {"sender_item_id": i["sender_item_id"]}, "payout_item_id": f"I{n}",
                           "transaction_status": self.item_status} for n, i in enumerate(self.last_items)]}


@override_settings(AI_MODE="mock")
class PayoutRunTests(TestCase):
    def setUp(self):
        self.paypal = PayPalWithStatus()
        for target in ("tips.services.get_client", "tips.views.get_client"):
            patcher = mock.patch(target, return_value=self.paypal)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.render = mock.MagicMock()
        self.render.workflows.start_task.return_value = SimpleNamespace(id="trn-abc123")
        patcher = mock.patch("tips.payout_runs._client", return_value=self.render)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.location = Location.default()
        self.owner = make_manager("owner")
        self.client.force_login(self.owner)
        maria, john = hire("Maria", "server"), hire("John", "server")
        parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                          summary=parsed["summary"])
        self.night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=self.night, amount=Decimal("100"), paypal_order_id="R1", status="completed")
        for person in (maria, john):
            Shift.objects.create(night=self.night, staff=person, role="server", hours=Decimal("5"))
        services.build_split(self.night, rules)

    def pay(self):
        return payout_runs.pay(Night.objects.get(pk=self.night.pk))

    def test_without_render_it_is_sent_right_away(self):
        self.pay()
        self.assertEqual(len(self.paypal.sent_ids), 1)
        self.assertFalse(PayoutRun.objects.exists())
        self.render.workflows.start_task.assert_not_called()

    @override_settings(**ON_RENDER)
    def test_on_render_a_workflow_sends_and_follows_it(self):
        batch = self.pay()
        self.assertEqual(self.paypal.sent_ids, [])  # nothing sent from the web request
        run = PayoutRun.objects.get()
        self.assertEqual((run.batch, run.task_run_id, run.state), (batch, "trn-abc123", PayoutRun.State.QUEUED))
        args, kwargs = self.render.workflows.start_task.call_args
        self.assertEqual(args, ("tippool-payouts/pay_shift", [run.pk]))
        self.assertEqual(kwargs["idempotency_key"], f"tippool-payout-run-{run.pk}")  # never two runs for one
        self.night.refresh_from_db()
        self.assertEqual(self.night.status, Night.Status.PAYING)

        self.assertEqual(payout_runs.send_step(run.pk), {"ok": True})
        self.assertEqual(self.paypal.sent_ids, [batch.sender_batch_id])
        self.assertEqual(payout_runs.send_step(run.pk), {"ok": True})  # a repeat sends nothing
        self.assertEqual(len(self.paypal.sent_ids), 1)

        result = payout_runs.follow_step(run.pk, sleep=lambda s: None)
        self.assertEqual(result, {"ok": True, "failed": 0})
        run.refresh_from_db()
        self.night.refresh_from_db()
        self.assertEqual((run.state, self.night.status), (PayoutRun.State.DONE, Night.Status.PAID))
        texts = " | ".join(s["text"] for s in run.steps)
        self.assertIn("Maria: $50.00 paid", texts)
        self.assertIn("The shift is paid", texts)

    @override_settings(**ON_RENDER)
    def test_an_unclear_answer_is_retried_with_the_very_same_request(self):
        self.pay()
        run = PayoutRun.objects.get()
        with mock.patch.object(self.paypal, "create_payout_batch", side_effect=PayPalError("boom", status=503)):
            with self.assertRaises(services.TipPoolError):  # the workflow retries the task
                payout_runs.send_step(run.pk)
        run.batch.refresh_from_db()
        self.assertEqual(run.batch.status, "UNKNOWN")
        payout_runs.send_step(run.pk)  # the retry
        self.assertEqual(self.paypal.sent_ids, [run.batch.sender_batch_id])
        run.refresh_from_db()
        self.assertEqual(run.state, PayoutRun.State.FOLLOWING)

    @override_settings(**ON_RENDER)
    def test_a_clear_no_stops_the_run_and_the_review_says_why(self):
        self.pay()
        run = PayoutRun.objects.get()
        with mock.patch.object(self.paypal, "create_payout_batch", side_effect=PayPalError("Receiver unknown", status=422)):
            self.assertEqual(payout_runs.send_step(run.pk)["ok"], False)
        run.refresh_from_db()
        self.night.refresh_from_db()
        self.assertEqual((run.state, run.batch, self.night.status), (PayoutRun.State.STOPPED, None, Night.Status.REVIEW))
        self.assertContains(self.client.get(reverse("review", args=[self.night.pk])), "The last payout was stopped")
        status = self.client.get(reverse("payout_status_json", args=[self.night.pk])).json()
        self.assertEqual((status["night_status"], status["run"]["state"]), ("review", "stopped"))

    @override_settings(**ON_RENDER)
    def test_freezing_payouts_before_it_was_sent_stops_it(self):
        self.pay()
        run = PayoutRun.objects.get()
        business = self.location.business
        business.payouts_frozen_at = timezone.now()
        business.save()
        self.assertEqual(payout_runs.send_step(run.pk), {"ok": False})
        self.assertEqual(self.paypal.sent_ids, [])
        self.night.refresh_from_db()
        self.assertEqual(self.night.status, Night.Status.REVIEW)

    @override_settings(**ON_RENDER)
    def test_if_render_does_not_answer_it_is_sent_right_away(self):
        self.render.workflows.start_task.side_effect = RuntimeError("no route to Render")
        self.pay()
        self.assertEqual(len(self.paypal.sent_ids), 1)
        run = PayoutRun.objects.get()
        self.assertEqual((run.task_run_id, run.state), ("", PayoutRun.State.DONE))

    @override_settings(**ON_RENDER)
    def test_running_out_of_tries_tells_the_manager_not_to_pay_again(self):
        self.pay()
        run = PayoutRun.objects.get()
        payout_runs.gave_up(run.pk, "send")
        run.refresh_from_db()
        self.assertEqual(run.state, PayoutRun.State.STUCK)
        self.assertIn("Don't pay again", run.steps[-1]["text"])

    @override_settings(**ON_RENDER)
    def test_still_pending_after_following_for_a_while(self):
        self.paypal.item_status = "PENDING"
        self.pay()
        run = PayoutRun.objects.get()
        payout_runs.send_step(run.pk)
        self.assertEqual(payout_runs.follow_step(run.pk, follow_for=0, sleep=lambda s: None), {"ok": True, "pending": True})

    @override_settings(**ON_RENDER)
    def test_the_payout_page_shows_the_run_live(self):
        res = self.client.post(reverse("pay", args=[self.night.pk]), follow=True)
        self.assertContains(res, "Render Workflow")
        self.assertContains(res, "trn-abc123")
        self.assertContains(res, "Handed to a Render Workflow")
        data = self.client.get(reverse("payout_status_json", args=[self.night.pk])).json()
        self.assertEqual((data["run"]["state"], data["run"]["active"]), ("queued", True))
        self.assertNotContains(res, "may or may not have gone out")  # on its way, not unclear

    @override_settings(**ON_RENDER)
    def test_platform_page_shows_render(self):
        self.client.force_login(make_operator("op"))
        page = self.client.get(reverse("platform_settings"))
        self.assertContains(page, "Workflows on")
        self.assertContains(page, "tippool-payouts/pay_shift")
