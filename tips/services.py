"""Business logic: split a night, pay it out, keep statuses in sync with PayPal."""
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from . import ai
from .models import Membership, Night, PayoutBatch, PayoutItem, Role, Share, Tip
from .paypal import PayPalError, get_client
from .split import PersonShare, RulesError, ShiftInput, SplitResult, calculate_split

log = logging.getLogger("tips.services")

# PayPal payout item statuses that are final
ITEM_DONE = {"SUCCESS", "UNCLAIMED"}  # UNCLAIMED = sent, waiting for the person to accept
ITEM_FAILED = {"FAILED", "RETURNED", "BLOCKED", "DENIED", "REFUNDED", "REVERSED"}


class TipPoolError(Exception):
    pass


# --- Tips (money in) ---------------------------------------------------------
def create_tip_order(location, amount, table="", return_url=None, cancel_url=None, for_staff=None):
    """Create a PayPal order for a tip on this location's current shift.
    `for_staff`: a personal tip for that person (only when the location allows personal tips)."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount < Decimal("1.00") or amount > Decimal("500.00"):
        raise TipPoolError("Tips must be between $1 and $500.")
    if for_staff is not None and location.tip_mode == location.TipMode.POOL:
        raise TipPoolError("Tips here are shared by the whole team.")
    if for_staff is None and location.tip_mode == location.TipMode.PERSONAL:
        raise TipPoolError("Please choose who you'd like to tip.")
    night = Night.current(location)
    who = f" for {for_staff.name}" if for_staff else ""
    order = get_client().create_order(amount, settings.TIP_CURRENCY,
                                      description=f"Tip{who}{f' - table {table}' if table else ''}"[:127],
                                      reference_id=f"night-{night.pk}",
                                      return_url=return_url, cancel_url=cancel_url)
    Tip.objects.create(night=night, amount=amount, paypal_order_id=order["id"], table=table[:20], staff=for_staff)
    return order["id"]


def add_manual_tip(night, amount, source, staff=None, note=""):
    """Card-machine or cash tips the manager adds at close-out."""
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        raise TipPoolError("This shift has already been paid out.")
    if source not in (Tip.Source.CARD, Tip.Source.CASH):
        raise TipPoolError("Choose card or cash.")
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount <= 0 or amount > Decimal("20000"):
        raise TipPoolError("Enter an amount above $0.")
    tip = Tip.objects.create(night=night, amount=amount, source=source, staff=staff, note=note[:120],
                             status=Tip.Status.COMPLETED)
    _invalidate_split(night)
    return tip


def remove_manual_tip(tip):
    night = tip.night
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        raise TipPoolError("This shift has already been paid out.")
    if tip.source == Tip.Source.PAYPAL:
        raise TipPoolError("PayPal tips can't be removed here.")
    tip.delete()
    _invalidate_split(night)


def _invalidate_split(night):
    """Tips changed after the split was calculated: it has to be calculated again."""
    if night.status == Night.Status.REVIEW:
        night.shares.all().delete()
        night.status = Night.Status.CLOSED
        night.save(update_fields=["status"])


def capture_tip(order_id):
    """Capture an approved tip order. Safe to call twice."""
    tip = Tip.objects.get(paypal_order_id=order_id)
    if tip.status == Tip.Status.COMPLETED:
        return tip
    try:
        result = get_client().capture_order(order_id)
    except PayPalError as exc:
        issue = (exc.details.get("details") or [{}])[0].get("issue")
        if issue == "ORDER_ALREADY_CAPTURED":
            result = get_client().get_order(order_id)
        else:
            raise
    _apply_order_result(tip, result)
    return tip


def sync_pending_tips(night, max_checks=20):
    """Safety net: finish tips the guest approved but that were never captured
    (e.g. the PayPal window closed before it could tell our page)."""
    finished = 0
    for tip in night.tips.filter(status=Tip.Status.CREATED).order_by("-created_at")[:max_checks]:
        try:
            status = get_client().get_order(tip.paypal_order_id).get("status")
            if status in ("APPROVED", "COMPLETED"):
                capture_tip(tip.paypal_order_id)
                finished += 1
        except PayPalError:
            log.warning("Could not sync tip order %s", tip.paypal_order_id)
    return finished


def _apply_order_result(tip, result):
    unit = result["purchase_units"][0]
    captures = unit.get("payments", {}).get("captures", [])
    capture = captures[0] if captures else {}
    payer = result.get("payer", {}).get("name", {})
    tip.payer_name = " ".join(filter(None, [payer.get("given_name"), payer.get("surname")]))
    tip.paypal_capture_id = capture.get("id", "")
    tip.status = Tip.Status.COMPLETED if capture.get("status") == "COMPLETED" else tip.status
    tip.save()


def mark_capture_completed(order_id, capture_id):
    """Called from the webhook: PAYMENT.CAPTURE.COMPLETED."""
    updated = Tip.objects.filter(paypal_order_id=order_id).exclude(status=Tip.Status.COMPLETED)
    return updated.update(status=Tip.Status.COMPLETED, paypal_capture_id=capture_id)


# --- Split -------------------------------------------------------------------
@transaction.atomic
def build_split(night, rule_set):
    """Calculate everyone's share for a night and store it (replaces an earlier draft)."""
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        raise TipPoolError("This shift has already been paid out.")
    shifts = list(night.shifts.select_related("staff"))
    if night.tip_total <= 0:
        raise TipPoolError("There are no completed tips for this shift yet.")
    pool = night.pool_total
    if pool > 0 and not shifts:
        raise TipPoolError("Add who worked this shift first.")
    if pool > 0 and rule_set is None:
        raise TipPoolError("Set your tip rules first.")

    roles = list(Role.objects.filter(location_id=night.location_id))
    role_names = {r.key: r.name for r in roles}
    # roles marked "no tips" today count even if the saved rules are older than that setting
    no_tips = {r.key for r in roles if r.no_tips}
    if pool > 0:
        inputs = [ShiftInput(str(s.staff_id), s.staff.name, s.role, s.hours) for s in shifts]
        try:
            result = calculate_split(pool, inputs, rule_set.rules, never_in_pool=no_tips, role_names=role_names)
        except RulesError as exc:
            raise TipPoolError(str(exc)) from exc
    else:
        result = SplitResult(total=Decimal("0.00"), shares=[], notes=[])
    _add_personal_tips(night, result, role_names)

    explanations = ai.explain_shares(result.total, rule_set.summary if rule_set else "Personal tips only.",
                                     result.shares, result.notes, role_names)
    night.shares.all().delete()
    for person in result.shares:
        Share.objects.create(
            night=night, staff_id=int(person.key), role=person.role, hours=person.hours,
            amount=person.amount, breakdown=person.breakdown,
            explanation=explanations.get(person.name, ""),
        )
    night.rule_set = rule_set
    night.status = Night.Status.REVIEW
    night.save()
    return result


def _add_personal_tips(night, result, role_names):
    """Tips a guest left for one person go to that person in full, on top of their pool share.
    Someone who only got personal tips (not on the roster) still gets a share."""
    personal = {}
    for tip in night.tips.filter(status=Tip.Status.COMPLETED, staff__isnull=False).select_related("staff"):
        personal.setdefault(tip.staff, Decimal("0"))
        personal[tip.staff] += tip.amount
    if not personal:
        return
    by_key = {p.key: p for p in result.shares}
    usual = dict(Membership.objects.filter(location_id=night.location_id).values_list("staff_id", "default_role"))
    for staff, amount in personal.items():
        person = by_key.get(str(staff.pk))
        if person is None:
            person = PersonShare(str(staff.pk), staff.name, usual.get(staff.pk, ""), Decimal("0"))
            result.shares.append(person)
            by_key[person.key] = person
        amount = amount.quantize(Decimal("0.01"))
        person.amount += amount
        person.breakdown.append({"group": "Tips left for you", "pool": str(amount), "basis": "by name",
                                 "amount": str(amount), "personal": True})
    result.total += sum(personal.values(), Decimal("0")).quantize(Decimal("0.01"))
    result.shares.sort(key=lambda p: (-p.amount, p.name))


# --- Payouts (money out) -----------------------------------------------------
def pay_night(night):
    """Send one PayPal Payouts batch with everyone's share."""
    with transaction.atomic():
        night = Night.objects.select_for_update().get(pk=night.pk)
        if night.status != Night.Status.REVIEW:
            raise TipPoolError("Only a reviewed split can be paid.")
        shares = [s for s in night.shares.select_related("staff") if s.amount > 0]
        if not shares:
            raise TipPoolError("Nobody has anything to be paid.")
        batch = PayoutBatch.objects.create(night=night, sender_batch_id=f"tippool-{night.pk}-{uuid.uuid4().hex[:10]}")
        for share in shares:
            PayoutItem.objects.create(batch=batch, share=share,
                                      sender_item_id=f"{batch.sender_batch_id}-{share.staff_id}")
        night.status = Night.Status.PAYING
        night.save()

    items = [{
        "receiver": item.share.staff.paypal_email,
        "amount": item.share.amount,
        "note": (item.share.explanation or f"Your tips for {night}")[:4000],
        "sender_item_id": item.sender_item_id,
    } for item in batch.items.select_related("share__staff")]
    try:
        response = get_client().create_payout_batch(batch.sender_batch_id, items,
                                                    subject=f"Your tips for {night.date:%b %d}",
                                                    currency=settings.TIP_CURRENCY)
    except PayPalError:
        # nothing was sent: undo so the manager can fix and retry
        batch.delete()
        night.status = Night.Status.REVIEW
        night.save()
        raise
    header = response["batch_header"]
    batch.paypal_batch_id = header["payout_batch_id"]
    batch.status = header["batch_status"]
    batch.save()
    return batch


def refresh_batch(batch):
    """Ask PayPal for the latest status of a batch and each person's payment."""
    if not batch.paypal_batch_id:
        return batch
    data = get_client().get_payout_batch(batch.paypal_batch_id)
    batch.status = data["batch_header"]["batch_status"]
    batch.save()
    by_sender_id = {i.sender_item_id: i for i in batch.items.all()}
    for row in data.get("items", []):
        item = by_sender_id.get(row["payout_item"].get("sender_item_id"))
        if item:
            _apply_item(item, row)
    _finish_night_if_done(batch)
    return batch


def apply_payout_item_event(resource):
    """Called from the webhook: PAYMENT.PAYOUTS-ITEM.*"""
    sender_item_id = resource.get("payout_item", {}).get("sender_item_id")
    item = PayoutItem.objects.filter(sender_item_id=sender_item_id).select_related("batch").first()
    if item:
        _apply_item(item, resource)
        _finish_night_if_done(item.batch)
    return item


def _apply_item(item, row):
    item.status = row.get("transaction_status", item.status)
    item.paypal_item_id = row.get("payout_item_id", item.paypal_item_id)
    item.transaction_id = row.get("transaction_id", item.transaction_id) or ""
    errors = row.get("errors") or {}
    item.error = errors.get("message", "") if isinstance(errors, dict) else ""
    item.save()


def _finish_night_if_done(batch):
    statuses = set(batch.items.values_list("status", flat=True))
    if statuses and statuses <= ITEM_DONE | ITEM_FAILED:
        night = batch.night
        if night.status != Night.Status.PAID:
            night.status = Night.Status.PAID
            night.save()
