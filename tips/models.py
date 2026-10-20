import secrets
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.timezone import now


class Business(models.Model):
    """A business account: everything (locations, team, tips, reports) belongs to one.
    Managers only ever see their own business."""

    CURRENCIES = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}

    class FeeMode(models.TextChoices):
        BUSINESS = "business", "The business pays the PayPal fees"
        POOL = "pool", "Take the fees out of the tips before sharing"

    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(default=now)
    currency = models.CharField(max_length=3, default="USD", choices=[(c, c) for c in CURRENCIES])
    fee_mode = models.CharField(max_length=10, choices=FeeMode.choices, default=FeeMode.BUSINESS)
    # card-machine fee, % of card tips (only used when fees come out of the tips)
    card_fee_percent = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    # the business's own PayPal app (REST credentials). Empty = sandbox demo on the TipPool account.
    paypal_client_id = models.CharField(max_length=200, blank=True)
    paypal_secret_encrypted = models.TextField(blank=True)
    paypal_webhook_id = models.CharField(max_length=60, blank=True)
    paypal_live = models.BooleanField(default=False)
    # made by "Try the demo": sample data, sandbox only, deleted after a week
    is_demo = models.BooleanField(default=False)

    # --- owner controls ---
    class Digest(models.TextChoices):
        OFF = "off", "Off"
        DAILY = "daily", "Every morning"
        WEEKLY = "weekly", "Every Monday morning"

    # payouts above this (one shift) wait for the owner's OK, even automatic ones. Empty = no limit.
    approval_limit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # the owner's emergency stop: no payout leaves while this is set (tips still come in)
    payouts_frozen_at = models.DateTimeField(null=True, blank=True)
    payouts_frozen_by = models.CharField(max_length=150, blank=True)
    # warn when something is quietly wrong (no tips by the usual time, far below normal, ...)
    smart_alerts = models.BooleanField(default=True)
    digest = models.CharField(max_length=10, choices=Digest.choices, default=Digest.WEEKLY)
    digest_sent_on = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name

    @property
    def payouts_frozen(self):
        return self.payouts_frozen_at is not None

    @property
    def symbol(self):
        return self.CURRENCIES.get(self.currency, self.currency + " ")

    @property
    def paypal_connected(self):
        return bool(self.paypal_client_id and self.paypal_secret_encrypted)

    @property
    def paypal_secret(self):
        from .vault import decrypt
        return decrypt(self.paypal_secret_encrypted)

    def set_paypal_secret(self, secret):
        from .vault import encrypt
        self.paypal_secret_encrypted = encrypt(secret)


class Manager(models.Model):
    """A login that manages a business. Staff don't need one (they use their private link)."""

    class Level(models.TextChoices):
        MANAGER = "manager", "Manager"
        LEAD = "lead", "Shift lead"

    user = models.OneToOneField("auth.User", related_name="manager", on_delete=models.CASCADE)
    business = models.ForeignKey(Business, related_name="managers", on_delete=models.CASCADE)
    is_owner = models.BooleanField(default=False)
    # shift leads run the shift (who worked, card/cash tips, codes, checking the split) but can't pay,
    # change rules, see reports or change settings
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.MANAGER)
    # two-step sign-in: a 6-digit code from an authenticator app (secret stored encrypted)
    totp_secret_encrypted = models.TextField(blank=True)
    totp_enabled = models.BooleanField(default=False)
    # the locations this manager runs; none picked = every location (owners always see everything)
    locations = models.ManyToManyField("Location", blank=True, related_name="location_managers")

    def __str__(self):
        return f"{self.user} at {self.business}"

    @property
    def is_lead(self):
        return self.level == self.Level.LEAD and not self.is_owner

    @property
    def level_label(self):
        return "Owner" if self.is_owner else self.get_level_display()

    @property
    def all_access(self):
        """Owner, or a manager not limited to some locations."""
        return self.is_owner or not self.locations.exists()

    def allowed_locations(self):
        own = Location.objects.filter(business=self.business, archived=False)
        if self.is_owner:
            return own
        picked = self.locations.filter(archived=False)
        return picked if self.locations.exists() else own


def new_payout_key():
    return secrets.token_hex(3)


def new_invite_token():
    return secrets.token_urlsafe(24)


class Invite(models.Model):
    """An email invitation for another manager to join a business."""

    business = models.ForeignKey(Business, related_name="invites", on_delete=models.CASCADE)
    email = models.EmailField()
    level = models.CharField(max_length=10, default="manager")
    token = models.CharField(max_length=60, unique=True, default=new_invite_token)
    invited_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(default=now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    locations = models.ManyToManyField("Location", blank=True, related_name="+")

    VALID_DAYS = 7

    class Meta:
        ordering = ["-created_at"]

    @property
    def usable(self):
        from datetime import timedelta
        return self.accepted_at is None and self.created_at > timezone.now() - timedelta(days=self.VALID_DAYS)


class Location(models.Model):
    """One shop / venue. A business can run several, each with its own roles, rules, codes and closing times."""

    class AfterClose(models.TextChoices):
        REVIEW = "review", "Prepare the split for me to approve"
        PAY = "pay", "Pay the team automatically"

    business = models.ForeignKey(Business, related_name="locations", on_delete=models.CASCADE)
    name = models.CharField(max_length=80, blank=True)
    # closed for good: hidden everywhere, its tip codes stop working; history stays for the books
    archived = models.BooleanField(default=False)
    # used in public links (tip page, QR codes): /tip/?at=<slug>
    slug = models.SlugField(max_length=40, unique=True)
    business_type = models.CharField(max_length=20, default="restaurant", blank=True)
    timezone = models.CharField(max_length=64, default="America/New_York")
    auto_close_enabled = models.BooleanField(default=False)
    after_close = models.CharField(max_length=10, choices=AfterClose.choices, default=AfterClose.REVIEW)
    class TipMode(models.TextChoices):
        POOL = "pool", "Shared by the team"
        PERSONAL = "personal", "Each person keeps their own tips"
        BOTH = "both", "Guests choose: the team or one person"

    tip_mode = models.CharField(max_length=10, choices=TipMode.choices, default=TipMode.POOL)
    # automatic payouts wait this long after closing, so staff can flag a mistake first (0 = pay right away)
    review_minutes = models.PositiveIntegerField(default=0)
    class TipStyle(models.TextChoices):
        AMOUNTS = "amounts", "Fixed amounts ($5, $10...)"
        PERCENT = "percent", "Percent of the bill (15%, 20%...)"

    # tip buttons on the guest page; empty = the usual for this kind of business
    tip_style = models.CharField(max_length=10, choices=TipStyle.choices, blank=True)
    tip_values = models.JSONField(default=list, blank=True)
    tip_message = models.CharField(max_length=160, blank=True, help_text="A line for guests on the tip page")
    # at an automatic close with nobody on the roster and nobody clocked in: use everyone's usual week
    use_usual_schedule = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return self.name or "Your business"

    @property
    def display_name(self):
        return self.name or "Your business"

    @property
    def tip_buttons(self):
        """("percent" | "amounts", [4 numbers]) for the guest tip page."""
        from .roles import tip_buttons_for
        if self.tip_style and len(self.tip_values) == 4:
            return self.tip_style, list(self.tip_values)
        return tip_buttons_for(self.business_type)

    @classmethod
    def default(cls):
        """The first location (created if there is none). Old printed QR codes without ?at= land here."""
        obj = cls.objects.first()
        if obj is None:
            business = Business.objects.first() or Business.objects.create(name="My business")
            obj = cls.objects.create(slug="main", business=business)
        return obj

    @classmethod
    def new_slug(cls, name):
        from django.utils.text import slugify
        base = slugify(name)[:34] or "location"
        slug, n = base, 2
        while cls.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug


class Role(models.Model):
    """A job at one location (server, stylist, housekeeper...). Each location sets its own list."""

    location = models.ForeignKey(Location, related_name="roles", on_delete=models.CASCADE)
    # short id used in the tip rules and on past shifts; it never changes, even when the name does
    key = models.SlugField(max_length=20)
    name = models.CharField(max_length=40)
    aliases = models.CharField(max_length=200, blank=True,
                               help_text="Other words for this role, comma separated (helps read the rules)")
    no_tips = models.BooleanField(default=False, help_text="Managers and owners: never in the tip pool")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]
        unique_together = [("location", "key")]

    def __str__(self):
        return self.name

    @classmethod
    def label(cls, key, location_id):
        names = cls.names_for(location_id)
        return names.get(key) or (key or "").replace("-", " ").capitalize()

    @classmethod
    def names_for(cls, location_id):
        """{key: name} for a location, cached for a minute (cleared whenever a role changes)."""
        from django.core.cache import cache
        cache_key = f"role-names:{location_id}"
        names = cache.get(cache_key)
        if names is None:
            names = dict(cls.objects.filter(location_id=location_id).values_list("key", "name"))
            cache.set(cache_key, names, 60)
        return names

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._forget()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        self._forget()
        return result

    def _forget(self):
        from django.core.cache import cache
        cache.delete(f"role-names:{self.location_id}")

    @classmethod
    def new_key(cls, location, name):
        from django.utils.text import slugify
        base = slugify(name)[:16] or "role"
        key, n = base, 2
        while cls.objects.filter(location=location, key=key).exists():
            key, n = f"{base}-{n}", n + 1
        return key


def new_access_token():
    return secrets.token_urlsafe(16)


class StaffMember(models.Model):
    """A person on one business's team. They can work at several of its locations (see Membership)."""

    class PayoutMethod(models.TextChoices):
        PAYPAL = "paypal", "PayPal"
        VENMO = "venmo", "Venmo"

    business = models.ForeignKey(Business, related_name="staff", on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    paypal_email = models.EmailField(help_text="Where their tips are paid (PayPal account email)")
    payout_method = models.CharField(max_length=10, choices=PayoutMethod.choices, default=PayoutMethod.PAYPAL)
    venmo_phone = models.CharField(max_length=20, blank=True)
    # the worker said "yes, pay me here" on their own page (the manager only typed it)
    payout_confirmed_at = models.DateTimeField(null=True, blank=True)
    # personal data erased on request; the amounts stay for the business's records
    erased_at = models.DateTimeField(null=True, blank=True)
    # secret link so a staff member can see their own tips without a login
    access_token = models.CharField(max_length=40, default=new_access_token, unique=True, editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def payout_to(self):
        """Human-readable destination: 'PayPal maria@x.com' or 'Venmo +1 555...'."""
        if self.payout_method == self.PayoutMethod.VENMO and self.venmo_phone:
            return f"Venmo {self.venmo_phone}"
        return f"PayPal {self.paypal_email}"


class Membership(models.Model):
    """Someone on the team of one location, with their usual role there."""

    staff = models.ForeignKey(StaffMember, related_name="memberships", on_delete=models.CASCADE)
    location = models.ForeignKey(Location, related_name="memberships", on_delete=models.CASCADE)
    default_role = models.CharField(max_length=20)
    active = models.BooleanField(default=True)
    # usual week: {"0": 6, "4": 8} = 6 h on Mondays, 8 h on Fridays (weekday as text, 0 = Monday)
    schedule = models.JSONField(default=dict, blank=True)

    DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    class Meta:
        ordering = ["staff__name"]
        unique_together = [("staff", "location")]

    def __str__(self):
        return f"{self.staff} at {self.location}"

    @property
    def role_name(self):
        return Role.label(self.default_role, self.location_id)

    def usual_hours(self, weekday):
        try:
            return Decimal(str(self.schedule.get(str(weekday), 0) or 0))
        except Exception:
            return Decimal("0")

    @property
    def week(self):
        """[(0, "Mon", Decimal hours), ...] for the schedule form."""
        return [(d, self.DAY_SHORT[d], self.usual_hours(d)) for d in range(7)]

    @property
    def schedule_summary(self):
        """'Mon–Fri 6h · Sat 8h', or '' when there's no usual week."""
        parts, d = [], 0
        while d < 7:
            hours = self.usual_hours(d)
            if not hours:
                d += 1
                continue
            end = d
            while end + 1 < 7 and self.usual_hours(end + 1) == hours:
                end += 1
            days = self.DAY_SHORT[d] if end == d else f"{self.DAY_SHORT[d]}–{self.DAY_SHORT[end]}"
            parts.append(f"{days} {hours.normalize():f}h")
            d = end + 1
        return " · ".join(parts)


class TipRuleSet(models.Model):
    """The manager's tip-pool rules: the original text + the structured version the split uses."""

    location = models.ForeignKey(Location, related_name="rule_sets", on_delete=models.CASCADE)
    text = models.TextField()
    rules = models.JSONField()
    warnings = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Rules #{self.pk}"

    @classmethod
    def active(cls, location):
        return cls.objects.filter(location=location, is_active=True).first()


class Night(models.Model):
    """One service (shift) at one location, whose tips are pooled and split."""

    class Status(models.TextChoices):
        OPEN = "open", "Open (collecting tips)"
        CLOSED = "closed", "Closed, needs who worked"
        REVIEW = "review", "Ready to approve"
        PAYING = "paying", "Paying out"
        PAID = "paid", "Paid"

    location = models.ForeignKey(Location, related_name="nights", on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    rule_set = models.ForeignKey(TipRuleSet, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    auto_closed = models.BooleanField(default=False)
    # why an automatic payout was held back for the manager (empty = nothing to check)
    hold = models.CharField(max_length=200, blank=True)
    # bumped each time PayPal clearly rejects a payout, so the corrected retry gets a fresh id
    payout_attempt = models.PositiveSmallIntegerField(default=0)
    # part of every PayPal payout id, so ids never repeat even across databases sharing one PayPal account
    payout_key = models.CharField(max_length=8, default=new_payout_key)
    # the close-out check (rules + AI): {"ok", "summary", "findings": [...], "suggestions": [...], "by"}
    review_check = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.date:%A, %b %d} shift"

    @property
    def tip_total(self):
        """Every completed tip: shared and personal, PayPal, card and cash."""
        return self._sum(self.tips.filter(status=Tip.Status.COMPLETED))

    @property
    def pool_total(self):
        """The tips the team shares under the rules (personal tips go straight to their person)."""
        return self._sum(self.tips.filter(status=Tip.Status.COMPLETED, staff__isnull=True))

    @property
    def payout_batch(self):
        """The first (main) payout batch; retries are separate small batches."""
        return self.payout_batches.filter(is_retry=False).first()

    @property
    def fee_total(self):
        fees = self.tips.filter(status=Tip.Status.COMPLETED).aggregate(s=models.Sum("fee"))["s"]
        return (fees or Decimal("0")).quantize(Decimal("0.01"))

    @staticmethod
    def _sum(qs):
        return (qs.aggregate(s=models.Sum("amount"))["s"] or Decimal("0")).quantize(Decimal("0.01"))

    @classmethod
    def current(cls, location):
        """The shift at this location that is collecting tips right now (created if needed).
        Locked on the location, so two requests at the same moment can't open two shifts."""
        night = cls.objects.filter(location=location, status=cls.Status.OPEN).order_by("created_at").first()
        if night:
            return night
        from django.db import transaction

        from .autoclose import service_window
        with transaction.atomic():
            Location.objects.select_for_update().filter(pk=location.pk).first()
            night = cls.objects.filter(location=location, status=cls.Status.OPEN).order_by("created_at").first()
            return night or cls.objects.create(location=location, date=service_window(location)[0])


class Tip(models.Model):
    """One tip: paid by a guest with PayPal / Venmo, or card / cash tips the manager adds at close-out."""

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    class Source(models.TextChoices):
        PAYPAL = "paypal", "PayPal / Venmo"
        CARD = "card", "Card machine"
        CASH = "cash", "Cash"

    night = models.ForeignKey(Night, related_name="tips", on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.PAYPAL)
    # set = a personal tip for this person; empty = shared by the team under the tip rules
    staff = models.ForeignKey(StaffMember, null=True, blank=True, related_name="personal_tips", on_delete=models.PROTECT)
    note = models.CharField(max_length=120, blank=True)
    paypal_order_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # what PayPal (or the card machine) kept from this tip
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # e.g. "Refunded to the guest" / "Guest opened a dispute"
    problem = models.CharField(max_length=200, blank=True)
    # partly refunded: `amount` is what still counts, this is what went back to the guest
    refunded = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    # the guest's amount before any refund (set the first time a refund touches this tip)
    original_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    paypal_capture_id = models.CharField(max_length=64, blank=True)
    payer_name = models.CharField(max_length=120, blank=True)
    table = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"${self.amount} ({self.get_status_display()})"


class ClockEntry(models.Model):
    """Someone clocked in (and later out) from their own tips page. Gives the hours for the shift."""

    staff = models.ForeignKey(StaffMember, related_name="clock_entries", on_delete=models.CASCADE)
    night = models.ForeignKey("Night", related_name="clock_entries", on_delete=models.CASCADE)
    started_at = models.DateTimeField(default=now)
    ended_at = models.DateTimeField(null=True, blank=True)
    # clocked out by the close-out because they forgot: the manager should check these hours
    auto_ended = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at"]

    @property
    def running(self):
        return self.ended_at is None

    def hours(self, until=None):
        end = self.ended_at or until or timezone.now()
        seconds = max((end - self.started_at).total_seconds(), 0)
        return (Decimal(seconds) / 3600).quantize(Decimal("0.01"))


class RoleLabelMixin:
    @property
    def role_name(self):
        location_id = self.__dict__.get("_location_id") or self.night.location_id
        return Role.label(self.role, location_id)


class Shift(RoleLabelMixin, models.Model):
    """Who worked a night, in which role, for how many hours."""

    night = models.ForeignKey(Night, related_name="shifts", on_delete=models.CASCADE)
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT)
    role = models.CharField(max_length=20)
    hours = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["staff__name"]
        unique_together = [("night", "staff")]

    def __str__(self):
        return f"{self.staff} - {self.role} {self.hours}h"


class Share(RoleLabelMixin, models.Model):
    """One person's calculated share of a night's tips."""

    night = models.ForeignKey(Night, related_name="shares", on_delete=models.CASCADE)
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT)
    role = models.CharField(max_length=20)
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    breakdown = models.JSONField(default=list)
    explanation = models.TextField(blank=True)

    class Meta:
        ordering = ["-amount"]
        unique_together = [("night", "staff")]

    @property
    def payout_item(self):
        """The latest payout for this share (an earlier one may have failed and been sent again)."""
        return self.payout_items.order_by("-pk").first()


class PayoutBatch(models.Model):
    """One PayPal Payouts batch = everyone paid for one night in one API call."""

    night = models.ForeignKey(Night, related_name="payout_batches", on_delete=models.CASCADE)
    # a retry sends one person's payout again (e.g. to a corrected email)
    is_retry = models.BooleanField(default=False)
    # exactly what was sent, so an unclear answer from PayPal can be checked by sending the very same
    # request again (same id: PayPal never pays it twice)
    request_items = models.JSONField(default=list, blank=True)
    sender_batch_id = models.CharField(max_length=64, unique=True)
    paypal_batch_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="NEW")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Payout {self.paypal_batch_id or self.sender_batch_id} ({self.status})"


class PayoutItem(models.Model):
    FAILED = {"FAILED", "RETURNED", "BLOCKED", "DENIED", "REFUNDED", "REVERSED"}

    batch = models.ForeignKey(PayoutBatch, related_name="items", on_delete=models.CASCADE)
    # a share that was paid can never be deleted by a new split (only together with its whole shift)
    share = models.ForeignKey(Share, related_name="payout_items", on_delete=models.RESTRICT)
    receiver = models.CharField(max_length=200, blank=True)  # where it was sent, e.g. "PayPal maria@x.com"
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sender_item_id = models.CharField(max_length=64, unique=True)
    paypal_item_id = models.CharField(max_length=64, blank=True)
    transaction_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    error = models.TextField(blank=True)
    # replaced by a newer payout (sent again after it failed or was cancelled)
    superseded = models.BooleanField(default=False)

    @property
    def failed(self):
        return self.status in self.FAILED

    LABELS = {"SUCCESS": "Arrived", "UNCLAIMED": "Not accepted yet", "PENDING": "Sending", "PROCESSING": "Sending",
              "NEW": "Sending", "SENDING": "Sending", "ONHOLD": "On hold at PayPal", "FAILED": "Failed",
              "RETURNED": "Came back", "BLOCKED": "Blocked by PayPal", "REFUNDED": "Taken back",
              "REVERSED": "Reversed", "DENIED": "Refused by PayPal", "UNKNOWN": "Checking with PayPal"}

    @property
    def status_label(self):
        return self.LABELS.get(self.status, self.status.title())


class WebhookEvent(models.Model):
    """Every PayPal webhook we receive (kept so duplicates are ignored)."""

    event_id = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=80)
    verified = models.BooleanField(default=False)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)


class CloseTime(models.Model):
    """When a location's shift closes, per weekday of the service day (0 = Monday)."""

    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    location = models.ForeignKey(Location, related_name="close_times", on_delete=models.CASCADE)
    weekday = models.PositiveSmallIntegerField()
    time = models.TimeField()
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday"]
        unique_together = [("location", "weekday")]

    @property
    def day_name(self):
        return self.WEEKDAYS[self.weekday]

    @property
    def after_midnight(self):
        """1:00 AM on a Friday service means Saturday 1:00 AM."""
        return self.time.hour < 6


class Message(models.Model):
    """A staff member flags a problem ("my hours are wrong"), writes to the manager,
    or asks to be paid somewhere else (the manager approves that change)."""

    class Kind(models.TextChoices):
        MESSAGE = "message", "Message"
        PAYOUT_CHANGE = "payout", "Change where I'm paid"

    staff = models.ForeignKey(StaffMember, related_name="messages", on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MESSAGE)
    # for payout changes: {"method": "paypal"|"venmo", "email": "...", "phone": "..."}
    payload = models.JSONField(default=dict, blank=True)
    location = models.ForeignKey(Location, related_name="messages", on_delete=models.CASCADE)
    night = models.ForeignKey(Night, null=True, blank=True, related_name="messages", on_delete=models.SET_NULL)
    text = models.TextField(max_length=1000)
    created_at = models.DateTimeField(default=now)
    reply = models.TextField(blank=True, max_length=1000)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_open(self):
        return self.resolved_at is None


class UsedConfirmation(models.Model):
    """An Ask confirmation that was already used, so the same one can't run twice (double click, resend)."""

    nonce = models.CharField(max_length=40, unique=True)
    used_at = models.DateTimeField(default=now)


class Change(models.Model):
    """History: who changed what, and when (rules, hours, tips, team, settings, payouts)."""

    location = models.ForeignKey(Location, null=True, blank=True, related_name="changes", on_delete=models.CASCADE)
    who = models.CharField(max_length=150)
    what = models.CharField(max_length=300)
    at = models.DateTimeField(default=now)

    class Meta:
        ordering = ["-at", "-pk"]

    @classmethod
    def log(cls, request_or_who, what, location=None):
        if hasattr(request_or_who, "user"):
            request = request_or_who
            user = request.user
            who = (user.get_full_name() or user.get_username()) if user.is_authenticated else "someone"
            location = location or getattr(request, "location", None)
        else:
            who = request_or_who
        return cls.objects.create(location=location, who=who[:150], what=what[:300])


class Alert(models.Model):
    """Something that needs the manager: a failed or unclaimed payout, a refunded tip, a dispute,
    a shift on hold. Shown on Messages and the overview, and emailed to the managers."""

    business = models.ForeignKey(Business, related_name="alerts", on_delete=models.CASCADE)
    location = models.ForeignKey(Location, null=True, blank=True, related_name="alerts", on_delete=models.CASCADE)
    night = models.ForeignKey(Night, null=True, blank=True, related_name="alerts", on_delete=models.SET_NULL)
    text = models.CharField(max_length=300)
    # a key so the same problem isn't raised twice (e.g. "payout-failed:123")
    key = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_open(self):
        return self.resolved_at is None


def new_cron_secret():
    return secrets.token_urlsafe(24)


class PlatformConfig(models.Model):
    """Site-wide settings the person running TipPool fills in on the website (Platform settings),
    instead of in code or .env. One row. Secrets are stored encrypted."""

    class EmailProvider(models.TextChoices):
        GMAIL = "gmail", "Gmail"
        OUTLOOK = "outlook", "Outlook / Microsoft 365"
        BREVO = "brevo", "Brevo (free, works on any host)"
        OTHER = "other", "Another email service"

    # email: invites, password codes, alerts, summaries
    email_provider = models.CharField(max_length=10, choices=EmailProvider.choices, blank=True)
    email_host = models.CharField(max_length=120, blank=True)
    email_port = models.PositiveIntegerField(default=587)
    email_user = models.CharField(max_length=200, blank=True)
    email_password_encrypted = models.TextField(blank=True)
    email_from = models.CharField(max_length=200, blank=True)
    # AI
    ai_enabled = models.BooleanField(default=False)
    anthropic_key_encrypted = models.TextField(blank=True)
    claude_model = models.CharField(max_length=60, blank=True)
    # TipPool's own PayPal (the demo and businesses that haven't connected theirs yet)
    paypal_client_id = models.CharField(max_length=200, blank=True)
    paypal_secret_encrypted = models.TextField(blank=True)
    paypal_live = models.BooleanField(default=False)
    paypal_webhook_id = models.CharField(max_length=60, blank=True)
    # the demo
    demo_buyer_email = models.CharField(max_length=200, blank=True)
    demo_buyer_password = models.CharField(max_length=100, blank=True)
    demo_staff = models.JSONField(default=dict, blank=True)  # {"Maria": "sandbox email", ...}
    # the outside scheduler (cron-job.org) and the privacy page
    cron_secret = models.CharField(max_length=60, default=new_cron_secret)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    contact_email = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        from django.conf import settings
        defaults = {"cron_secret": settings.CRON_SECRET} if settings.CRON_SECRET else {}
        return cls.objects.get_or_create(pk=1, defaults=defaults)[0]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from .platform import forget
        forget()

    @property
    def email_password(self):
        from .vault import decrypt
        return decrypt(self.email_password_encrypted)

    @property
    def anthropic_key(self):
        from .vault import decrypt
        return decrypt(self.anthropic_key_encrypted)

    @property
    def paypal_secret(self):
        from .vault import decrypt
        return decrypt(self.paypal_secret_encrypted)
