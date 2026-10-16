import secrets
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.timezone import now


class Location(models.Model):
    """One shop / venue. An owner can run several, each with its own roles, rules, codes and closing times."""

    class AfterClose(models.TextChoices):
        REVIEW = "review", "Prepare the split for me to approve"
        PAY = "pay", "Pay the team automatically"

    name = models.CharField(max_length=80, blank=True)
    # used in public links (tip page, QR codes): /tip/?at=<slug>
    slug = models.SlugField(max_length=40, unique=True)
    business_type = models.CharField(max_length=20, default="restaurant", blank=True)
    timezone = models.CharField(max_length=64, default="America/New_York")
    auto_close_enabled = models.BooleanField(default=False)
    after_close = models.CharField(max_length=10, choices=AfterClose.choices, default=AfterClose.REVIEW)
    # table QR codes: how many numbered tables, plus named spots like "Bar" or "Patio"
    table_count = models.PositiveSmallIntegerField(default=0)
    spots = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=now)

    SPOT_SUGGESTIONS = ["Bar", "Patio", "Counter", "Takeout"]

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return self.name or "Your business"

    @property
    def display_name(self):
        return self.name or "Your business"

    @property
    def table_labels(self):
        return [str(n) for n in range(1, self.table_count + 1)] + list(self.spots)

    @classmethod
    def default(cls):
        """The first location (created if there is none). Used where no location was chosen."""
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(slug="main")
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
        role = cls.objects.filter(location_id=location_id, key=key).first()
        return role.name if role else (key or "").replace("-", " ").capitalize()

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
    """A person. They can work at several locations (see Membership)."""

    name = models.CharField(max_length=80)
    paypal_email = models.EmailField(help_text="Where their tips are paid (PayPal account email)")
    # secret link so a staff member can see their own tips without a login
    access_token = models.CharField(max_length=40, default=new_access_token, unique=True, editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """Someone on the team of one location, with their usual role there."""

    staff = models.ForeignKey(StaffMember, related_name="memberships", on_delete=models.CASCADE)
    location = models.ForeignKey(Location, related_name="memberships", on_delete=models.CASCADE)
    default_role = models.CharField(max_length=20)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["staff__name"]
        unique_together = [("staff", "location")]

    def __str__(self):
        return f"{self.staff} at {self.location}"

    @property
    def role_name(self):
        return Role.label(self.default_role, self.location_id)


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
        REVIEW = "review", "Split ready for review"
        PAYING = "paying", "Paying out"
        PAID = "paid", "Paid"

    location = models.ForeignKey(Location, related_name="nights", on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    rule_set = models.ForeignKey(TipRuleSet, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    auto_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.date:%A, %b %d} shift"

    @property
    def tip_total(self):
        total = self.tips.filter(status=Tip.Status.COMPLETED).aggregate(s=models.Sum("amount"))["s"]
        return (total or Decimal("0")).quantize(Decimal("0.01"))

    @classmethod
    def current(cls, location):
        """The shift at this location that is collecting tips right now (created if needed)."""
        night = cls.objects.filter(location=location, status=cls.Status.OPEN).first()
        if night:
            return night
        from .autoclose import location_tz
        return cls.objects.create(location=location, date=timezone.now().astimezone(location_tz(location)).date())


class Tip(models.Model):
    """A tip a customer paid with PayPal (one PayPal order)."""

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    night = models.ForeignKey(Night, related_name="tips", on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paypal_order_id = models.CharField(max_length=64, unique=True)
    paypal_capture_id = models.CharField(max_length=64, blank=True)
    payer_name = models.CharField(max_length=120, blank=True)
    table = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"${self.amount} ({self.get_status_display()})"


class RoleLabelMixin:
    @property
    def role_name(self):
        return Role.label(self.role, self.night.location_id)


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


class PayoutBatch(models.Model):
    """One PayPal Payouts batch = everyone paid for one night in one API call."""

    night = models.OneToOneField(Night, related_name="payout_batch", on_delete=models.CASCADE)
    sender_batch_id = models.CharField(max_length=64, unique=True)
    paypal_batch_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="NEW")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Payout {self.paypal_batch_id or self.sender_batch_id} ({self.status})"


class PayoutItem(models.Model):
    batch = models.ForeignKey(PayoutBatch, related_name="items", on_delete=models.CASCADE)
    share = models.OneToOneField(Share, related_name="payout_item", on_delete=models.CASCADE)
    sender_item_id = models.CharField(max_length=64, unique=True)
    paypal_item_id = models.CharField(max_length=64, blank=True)
    transaction_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    error = models.TextField(blank=True)


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
