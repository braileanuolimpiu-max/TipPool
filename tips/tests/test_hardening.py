"""Security review round 4: admin sign-in, two-step codes, reminders, shared staff, owner alerts, uploads,
switching PayPal accounts, and tips that arrive while a shift is being paid."""
import time
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from tips import services, totp
from tips.models import Alert, Business, Location, Membership, Night, PayoutBatch, PayoutItem, Share, Tip
from tips.tests.helpers import hire, make_manager
from tips.vault import encrypt


class CleanCache(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


class AdminSignInTests(CleanCache):
    def test_admin_has_no_sign_in_of_its_own(self):
        user = make_manager("boss")
        user.is_staff = user.is_superuser = True
        user.save()
        res = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res["Location"].startswith(reverse("login")))
        self.client.post("/admin/login/?next=/admin/", {"username": "boss@example.com", "password": "pw"})
        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.user.is_authenticated)


class TwoStepCodeTests(CleanCache):
    def setUp(self):
        super().setUp()
        self.user = make_manager("owner")
        self.secret = totp.new_secret()
        profile = self.user.manager
        profile.totp_secret_encrypted, profile.totp_enabled = encrypt(self.secret), True
        profile.save()

    def sign_in(self):
        return self.client.post(reverse("login"), {"username": "owner@example.com", "password": "pw"})

    def test_a_code_works_only_once(self):
        code = totp._code(self.secret, int(time.time() // 30))
        self.sign_in()
        self.assertRedirects(self.client.post(reverse("login_code"), {"code": code}), reverse("dashboard"))
        self.client.logout()
        self.sign_in()
        self.client.post(reverse("login_code"), {"code": code})
        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.user.is_authenticated)

    def test_five_wrong_codes_lock_the_account_and_warn_the_owner(self):
        right = totp._code(self.secret, int(time.time() // 30))
        wrong = "000000" if right != "000000" else "111111"
        self.sign_in()
        for _ in range(5):
            res = self.client.post(reverse("login_code"), {"code": wrong})
        self.assertRedirects(res, reverse("login"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("password", mail.outbox[0].subject)
        # from another browser (another network) with the right code: still locked
        self.client.logout()
        self.sign_in()
        self.client.post(reverse("login_code"), {"code": right})
        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.user.is_authenticated)


class PayoutReminderTests(CleanCache):
    def setUp(self):
        super().setUp()
        self.location = Location.default()
        self.client.force_login(make_manager("owner"))
        maria = hire("Maria", "server")
        self.night = Night.objects.create(location=self.location, date="2026-01-05", status=Night.Status.PAID)
        share = Share.objects.create(night=self.night, staff=maria, role="server", hours=5, amount=Decimal("20"))
        batch = PayoutBatch.objects.create(night=self.night, sender_batch_id="b1", paypal_batch_id="PB1",
                                           status="SUCCESS")
        self.item = PayoutItem.objects.create(batch=batch, share=share, sender_item_id="b1-1", status="SUCCESS",
                                              receiver="PayPal maria@example.com")

    def remind(self):
        return self.client.post(reverse("payout_action", args=[self.item.pk]), {"action": "remind"})

    def test_only_unclaimed_payouts_and_once_a_day(self):
        self.remind()
        self.assertEqual(len(mail.outbox), 0)  # it arrived: nothing to remind about
        PayoutItem.objects.filter(pk=self.item.pk).update(status="UNCLAIMED")
        self.remind()
        self.remind()
        self.assertEqual(len(mail.outbox), 1)

    def test_demo_never_emails(self):
        PayoutItem.objects.filter(pk=self.item.pk).update(status="UNCLAIMED")
        Business.objects.filter(pk=self.location.business_id).update(is_demo=True)
        self.remind()
        self.assertEqual(len(mail.outbox), 0)


class SharedStaffTests(CleanCache):
    def setUp(self):
        super().setUp()
        self.main = Location.default()
        self.uptown = Location.objects.create(business=self.main.business, name="Uptown", slug="uptown")
        self.ana = hire("Ana", "server", self.main, email="ana@example.com")
        # the same person works at both places
        self.member = Membership.objects.create(staff=self.ana, location=self.uptown, default_role="server")
        limited = make_manager("uptown-mgr", owner=False)
        limited.manager.locations.set([self.uptown])
        self.client.force_login(limited)

    def test_limited_manager_cannot_change_someone_who_works_elsewhere(self):
        self.client.post(reverse("staff_edit", args=[self.member.pk]), {"name": "Ana", "paypal_email": "me@evil.com"})
        self.client.post(reverse("staff_new_link", args=[self.member.pk]))
        self.client.post(reverse("staff_erase", args=[self.member.pk]))
        before = self.ana.access_token
        self.ana.refresh_from_db()
        self.assertEqual((self.ana.paypal_email, self.ana.access_token, self.ana.erased_at),
                         ("ana@example.com", before, None))


class OwnerAlertTests(CleanCache):
    def test_only_the_owner_closes_an_approval_alert(self):
        location = Location.default()
        make_manager("owner")
        alert = Alert.objects.create(business=location.business, location=location, text="Needs the owner's OK",
                                     key="owner-ok:1")
        self.client.force_login(make_manager("helper", owner=False))
        self.client.post(reverse("messages_page"), {"alert": alert.pk})
        alert.refresh_from_db()
        self.assertIsNone(alert.resolved_at)


class UploadSizeTests(CleanCache):
    def test_huge_requests_are_refused_before_reading(self):
        res = self.client.post(reverse("login"), data="x", content_type="text/plain",
                               CONTENT_LENGTH=str(50 * 1024 * 1024))
        self.assertEqual(res.status_code, 413)


class PayPalSwitchTests(CleanCache):
    def setUp(self):
        super().setUp()
        self.location = Location.default()
        self.business = self.location.business
        self.client.force_login(make_manager("owner"))

    def connect(self, live):
        with mock.patch("tips.paypal.detect_mode", return_value=live), \
                mock.patch("tips.paypal.PayPalClient.ensure_webhook", return_value="WH"):
            self.client.post(reverse("payment_settings"), {"paypal_client_id": f"CID-{live}", "paypal_secret": "S",
                                                           "currency": "USD", "fee_mode": "business"})
        return Business.objects.get(pk=self.business.pk)

    def test_test_tips_are_never_paid_with_real_money(self):
        Tip.objects.create(night=Night.current(self.location), amount=Decimal("10"), status=Tip.Status.COMPLETED,
                           paypal_order_id="SANDBOX-1")
        self.assertFalse(self.connect(live=True).paypal_live)

    def test_no_switch_while_a_shift_waits_to_be_paid(self):
        Night.objects.create(location=self.location, date="2026-01-05", status=Night.Status.REVIEW)
        self.assertNotEqual(self.connect(live=False).paypal_client_id, "CID-False")

    def test_switch_when_nothing_is_waiting(self):
        self.assertTrue(self.connect(live=True).paypal_live)


class LateTipTests(CleanCache):
    def test_a_tip_that_finishes_while_its_shift_is_paid_moves_on(self):
        location = Location.default()
        night = Night.objects.create(location=location, date="2026-01-05", status=Night.Status.REVIEW)
        tip = Tip.objects.create(night=night, amount=Decimal("5"), status=Tip.Status.COMPLETED, paypal_order_id="L1")
        stale = Tip.objects.select_related("night").get(pk=tip.pk)  # loaded before the payout started
        Night.objects.filter(pk=night.pk).update(status=Night.Status.PAYING)
        services._late_tip(stale)
        tip.refresh_from_db()
        self.assertNotEqual(tip.night_id, night.pk)


class TipPageWithoutPayPalTests(CleanCache):
    def test_guests_are_told_plainly_instead_of_refresh(self):
        location = Location.default()
        hire("Ana", "server", location)
        with mock.patch("tips.views.checkout_config", return_value=("", "")):
            page = self.client.get(reverse("tip_page") + f"?at={location.slug}")
        self.assertContains(page, "statusEl.textContent = T.not_ready")
        self.assertNotContains(page, "onPayPalWebSdkLoaded()\"></script>")
