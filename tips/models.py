import secrets
from decimal import Decimal

from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    SERVER = "server", "Server"
    BARTENDER = "bartender", "Bartender"
    BUSSER = "busser", "Busser"
    HOST = "host", "Host"
    RUNNER = "runner", "Food runner"
    COOK = "cook", "Cook"
    DISHWASHER = "dishwasher", "Dishwasher"
    MANAGER = "manager", "Manager"


def new_access_token():
    return secrets.token_urlsafe(16)


class StaffMember(models.Model):
    name = models.CharField(max_length=80)
    default_role = models.CharField(max_length=20, choices=Role.choices, default=Role.SERVER)
    paypal_email = models.EmailField(help_text="Where their tips are paid (PayPal account email)")
    active = models.BooleanField(default=True)
    # secret link so a staff member can see their own tips without a login
    access_token = models.CharField(max_length=40, default=new_access_token, unique=True, editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TipRuleSet(models.Model):
    """The manager's tip-pool rules: the original text + the structured version the split uses."""

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
    def active(cls):
        return cls.objects.filter(is_active=True).first()


class Night(models.Model):
    """One service (shift) whose tips are pooled and split."""

    class Status(models.TextChoices):
        OPEN = "open", "Open (collecting tips)"
        REVIEW = "review", "Split ready for review"
        PAYING = "paying", "Paying out"
        PAID = "paid", "Paid"

    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    rule_set = models.ForeignKey(TipRuleSet, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Night of {self.date:%b %d, %Y}"

    @property
    def tip_total(self):
        total = self.tips.filter(status=Tip.Status.COMPLETED).aggregate(s=models.Sum("amount"))["s"]
        return (total or Decimal("0")).quantize(Decimal("0.01"))

    @classmethod
    def current(cls):
        """The night that is collecting tips right now (created if needed)."""
        night = cls.objects.filter(status=cls.Status.OPEN).first()
        return night or cls.objects.create()


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


class Shift(models.Model):
    """Who worked a night, in which role, for how many hours."""

    night = models.ForeignKey(Night, related_name="shifts", on_delete=models.CASCADE)
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices)
    hours = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["staff__name"]
        unique_together = [("night", "staff")]

    def __str__(self):
        return f"{self.staff} - {self.role} {self.hours}h"


class Share(models.Model):
    """One person's calculated share of a night's tips."""

    night = models.ForeignKey(Night, related_name="shares", on_delete=models.CASCADE)
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices)
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
