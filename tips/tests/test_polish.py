from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from tips import guest_i18n
from tips.models import Location, Night, Tip
from tips.tests.helpers import hire


class GuestLanguageTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.url = reverse("tip_page") + "?at=" + self.location.slug
        hire("Sam", "server", self.location)  # a location with nobody on the team can't take tips

    def test_every_language_has_every_string(self):
        for code in guest_i18n.LANGS:
            self.assertEqual(set(guest_i18n.STRINGS[code]), set(guest_i18n.STRINGS["en"]), code)

    def test_browser_language_is_used(self):
        page = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="es-ES,es;q=0.9,en;q=0.8")
        self.assertContains(page, '<html lang="es">')
        self.assertContains(page, "Tu cuenta")
        self.assertContains(page, "Cargando PayPal")

    def test_unknown_language_falls_back_to_english(self):
        page = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="ja,zh;q=0.9")
        self.assertContains(page, "Your bill")

    def test_picked_language_is_remembered(self):
        page = self.client.get(self.url + "&lang=ro", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(page, "Nota ta")
        self.assertEqual(page.cookies[guest_i18n.COOKIE].value, "ro")
        page = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(page, "Nota ta")

    def test_switch_links_keep_the_code(self):
        page = self.client.get(self.url + "&table=7&lang=de")
        self.assertContains(page, "at=" + self.location.slug)
        self.assertContains(page, "table=7")
        self.assertContains(page, "lang=fr")

    def test_thanks_page_names_the_person(self):
        maria = hire("Maria", "server", self.location)
        tip = Tip.objects.create(night=Night.current(self.location), amount=Decimal("5.00"), staff=maria,
                                 status=Tip.Status.COMPLETED, paypal_order_id="ORDER-1")
        page = self.client.get(reverse("tip_thanks") + "?order=ORDER-1&lang=fr")
        self.assertContains(page, "PAYÉ")
        self.assertContains(page, "Maria")
        self.assertEqual(tip.amount, Decimal("5.00"))


class PublicPagesTests(TestCase):
    def test_privacy_and_terms_are_public(self):
        for name in ("privacy", "terms"):
            page = self.client.get(reverse(name))
            self.assertEqual(page.status_code, 200, name)
        self.assertContains(self.client.get(reverse("privacy")), "Anthropic")

    @override_settings(CONTACT_EMAIL="help@example.com")
    def test_contact_email_shown_when_set(self):
        self.assertContains(self.client.get(reverse("terms")), "help@example.com")

    @override_settings(DEBUG=False)
    def test_branded_404_shows_our_message_only(self):
        page = self.client.get(reverse("tip_page") + "?at=nope")
        self.assertEqual(page.status_code, 404)
        self.assertContains(page, "Nothing on this ticket", status_code=404)
        page = self.client.get("/no-such-page/")
        self.assertContains(page, "doesn't exist", status_code=404)
        self.assertNotContains(page, "Resolver404", status_code=404)


class UnfinishedPaymentTests(TestCase):
    def test_thanks_page_says_no_tip_was_taken(self):
        from unittest import mock
        from tips.paypal import PayPalError
        location = Location.default()
        hire("Sam", "server", location)
        Tip.objects.create(night=Night.current(location), amount=Decimal("5"), paypal_order_id="UNPAID-1")
        error = PayPalError("not approved", 422, details={"details": [{"issue": "ORDER_NOT_APPROVED"}]})
        with mock.patch("tips.services.capture_tip", side_effect=error):
            page = self.client.get(reverse("tip_thanks") + "?order=UNPAID-1&lang=en")
        self.assertContains(page, "No tip was taken")
