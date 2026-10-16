import secrets
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.timezone import now


class Business(models.Model):
    """A business account: everything (locations, team, tips, reports) belongs to one.
    Managers only ever see their own business."""

    name = models.CharField(max_length=80)
    created_at = models.DateTimeField(default=now)

    def __str__(self):
        return self.name


class Manager(models.Model):
    """A login that manages a business. Staff don't need one (they use their private link)."""

    user = models.OneToOneField("auth.User", related_name="manager", on_delete=models.CASCADE)
    business = models.ForeignKey(Business, related_name="managers", on_delete=models.CASCADE)
    is_owner = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user} at {self.business}"


def new_invite_token():
    return secrets.token_urlsafe(24)


class Invite(models.Model):
    """An email invitation for another manager to join a business."""

    business = models.ForeignKey(Business, related_name="invites", on_delete=models.CASCADE)
    email = models.EmailField()
    token = models.CharField(max_length=60, unique=True, default=new_invite_token)
    invited_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(default=now)
    accepted_at = models.DateTimeField(null=True, blank=True)

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
    """A person on one business's team. They can work at several of its locations (see Membership)."""

    business = models.ForeignKey(Business, related_name="staff", on_delete=models.CASCADE)
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
    # why an automatic payout was held back for the manager (empty = nothing to check)
    hold = models.CharField(max_length=200, blank=True)

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

    @staticmethod
    def _sum(qs):
        return (qs.aggregate(s=models.Sum("amount"))["s"] or Decimal("0")).quantize(Decimal("0.01"))

    @classmethod
    def current(cls, location):
        """The shift at this location that is collecting tips right now (created if needed)."""
        night = cls.objects.filter(location=location, status=cls.Status.OPEN).first()
        if night:
            return night
        from .autoclose import location_tz
        return cls.objects.create(location=location, date=timezone.now().astimezone(location_tz(location)).date())


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


class Message(models.Model):
    """A staff member flags a problem ("my hours are wrong") or writes to the manager."""

    staff = models.ForeignKey(StaffMember, related_name="messages", on_delete=models.CASCADE)
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
            who = request.user.get_username() if request.user.is_authenticated else "someone"
            location = location or getattr(request, "location", None)
        else:
            who = request_or_who
        return cls.objects.create(location=location, who=who[:150], what=what[:300])
