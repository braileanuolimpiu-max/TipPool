from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips.models import Business, Invite, Location, Manager, Night, Role, StaffMember, Tip
from tips.tests.helpers import hire, make_manager

User = get_user_model()

SIGNUP = {"business": "Studio 9", "business_type": "salon", "location": "", "timezone": "Europe/Bucharest",
          "name": "Ana Pop", "email": "Ana@Studio9.com", "password": "a-long-pass-123"}


@override_settings(AI_MODE="mock")
class SignupTests(TestCase):
    def test_signup_creates_business_location_roles_and_signs_in(self):
        res = self.client.post(reverse("signup"), SIGNUP)
        self.assertRedirects(res, reverse("dashboard"))
        user = User.objects.get(email="ana@studio9.com")
        self.assertEqual((user.first_name, user.last_name), ("Ana", "Pop"))
        business = user.manager.business
        self.assertTrue(user.manager.is_owner)
        location = business.locations.get()
        self.assertEqual((location.name, location.business_type, location.timezone),
                         ("Studio 9", "salon", "Europe/Bucharest"))
        self.assertTrue(Role.objects.filter(location=location, name="Stylist").exists())
        self.assertContains(self.client.get(reverse("dashboard")), "Studio 9")

    def test_duplicate_email_and_weak_password(self):
        self.client.post(reverse("signup"), SIGNUP)
        self.client.logout()
        res = self.client.post(reverse("signup"), {**SIGNUP, "email": "ana@studio9.com"})
        self.assertContains(res, "already an account")
        res = self.client.post(reverse("signup"), {**SIGNUP, "email": "new@x.com", "password": "123"})
        self.assertContains(res, "too short")
        self.assertEqual(Business.objects.filter(name="Studio 9").count(), 1)

    def test_sign_in_with_email_or_old_username(self):
        self.client.post(reverse("signup"), SIGNUP)
        self.client.logout()
        res = self.client.post(reverse("login"), {"username": "ANA@studio9.com", "password": "a-long-pass-123"})
        self.assertRedirects(res, reverse("dashboard"))
        self.client.logout()
        make_manager("manager")
        res = self.client.post(reverse("login"), {"username": "manager", "password": "pw"})
        self.assertRedirects(res, reverse("dashboard"))

    def test_login_without_business_finishes_signup(self):
        user = User.objects.create_user("admin", password="pw")
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse("staff_list")), reverse("signup"))
        res = self.client.post(reverse("signup"), {"business": "Late Bar", "business_type": "bar",
                                                   "timezone": "UTC"})
        self.assertRedirects(res, reverse("dashboard"))
        self.assertEqual(User.objects.get(pk=user.pk).manager.business.name, "Late Bar")

    def test_password_reset_email(self):
        self.client.post(reverse("signup"), SIGNUP)
        self.client.logout()
        self.client.post(reverse("password_reset"), {"email": "ana@studio9.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/password/reset/", mail.outbox[0].body)

    def test_pages_need_sign_in(self):
        res = self.client.get(reverse("staff_list"))
        self.assertRedirects(res, f"{reverse('login')}?next={reverse('staff_list')}")


@override_settings(AI_MODE="mock")
class IsolationTests(TestCase):
    """A manager only ever sees their own business."""

    def setUp(self):
        self.mine = Location.default()
        self.me = make_manager("me")
        other_business = Business.objects.create(name="Rival Cafe")
        self.theirs = Location.objects.create(business=other_business, name="Rival Cafe", slug="rival")
        Role.objects.create(location=self.theirs, key="barista", name="Barista")
        hire("Secret Sue", "barista", self.theirs, email="sue@example.com")
        self.their_night = Night.objects.create(location=self.theirs)
        Tip.objects.create(night=self.their_night, amount=Decimal("99"), paypal_order_id="R1", status="completed")
        self.client.force_login(self.me)

    def test_cannot_see_or_switch_to_other_business(self):
        self.assertNotContains(self.client.get(reverse("staff_list")), "Secret Sue")
        self.assertEqual(self.client.post(reverse("location_switch"), {"location": self.theirs.pk}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("staff_list") + "?at=rival"), "Secret Sue")
        self.assertEqual(self.client.get(reverse("review", args=[self.their_night.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("settings")), "Rival Cafe")

    def test_reports_for_all_locations_stay_in_the_business(self):
        page = self.client.get(reverse("reports") + "?scope=all&period=this_year")
        self.assertNotContains(page, "$99.00")

    def test_same_email_in_two_businesses_is_two_people(self):
        self.client.post(reverse("staff_list"), {"name": "Sue", "paypal_email": "SUE@example.com",
                                                 "default_role": "server"})
        people = StaffMember.objects.filter(paypal_email__iexact="sue@example.com")
        self.assertEqual(people.count(), 2)
        self.assertEqual({p.business for p in people}, {self.mine.business, self.theirs.business})

    def test_public_tip_page_follows_the_link_even_for_a_signed_in_manager(self):
        self.assertContains(self.client.get(reverse("tip_page") + "?at=rival"), "Rival Cafe")


@override_settings(AI_MODE="mock")
class InviteTests(TestCase):
    def setUp(self):
        self.owner = make_manager("owner")
        self.business = self.owner.manager.business
        self.client.force_login(self.owner)

    def invite(self, email="sam@example.com"):
        self.client.post(reverse("invite_manager"), {"email": email})
        return Invite.objects.get(email=email)

    def test_invite_and_accept_as_new_user(self):
        invite = self.invite()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(invite.token, mail.outbox[0].body)
        self.assertContains(self.client.get(reverse("settings")), invite.token)
        self.client.logout()
        res = self.client.post(reverse("accept_invite", args=[invite.token]),
                               {"name": "Sam Lee", "password": "another-good-pass-1"})
        self.assertRedirects(res, reverse("dashboard"))
        sam = User.objects.get(email="sam@example.com")
        self.assertEqual((sam.manager.business, sam.manager.is_owner), (self.business, False))
        invite.refresh_from_db()
        self.assertIsNotNone(invite.accepted_at)
        # used once
        self.client.logout()
        self.assertContains(self.client.get(reverse("accept_invite", args=[invite.token])), "expired")

    def test_expired_invite(self):
        invite = self.invite()
        Invite.objects.filter(pk=invite.pk).update(created_at=timezone.now() - timedelta(days=8))
        self.client.logout()
        self.assertContains(self.client.get(reverse("accept_invite", args=[invite.token])), "expired")

    def test_only_owner_invites_and_removes(self):
        helper = make_manager("helper", owner=False)
        self.client.force_login(helper)
        self.client.post(reverse("invite_manager"), {"email": "x@example.com"})
        self.assertFalse(Invite.objects.exists())
        self.client.force_login(self.owner)
        self.client.post(reverse("remove_manager", args=[helper.manager.pk]))
        self.assertFalse(Manager.objects.filter(user=helper).exists())
        self.client.post(reverse("remove_manager", args=[self.owner.manager.pk]))
        self.assertTrue(Manager.objects.filter(user=self.owner).exists())  # owner stays

    def test_manager_of_another_business_cannot_accept(self):
        invite = self.invite("taken@example.com")
        other = Business.objects.create(name="Elsewhere")
        Location.objects.create(business=other, name="Elsewhere", slug="elsewhere")
        taken = User.objects.create_user("taken@example.com", email="taken@example.com", password="pw")
        Manager.objects.create(user=taken, business=other, is_owner=True)
        self.client.force_login(taken)
        res = self.client.post(reverse("accept_invite", args=[invite.token]))
        self.assertContains(res, "already manages Elsewhere")
