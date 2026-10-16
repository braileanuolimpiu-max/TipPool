"""Business logic: take tips, split a shift, pay it out, keep statuses in sync with PayPal."""
import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from . import ai
from .alerts import raise_alert
from .models import Business, Membership, Night, PayoutBatch, PayoutItem, Role, Share, StaffMember, Tip
from .paypal import PayPalError, get_client
from .split import PersonShare, RulesError, ShiftInput, SplitResult, calculate_split

log = logging.getLogger("tips.services")
CENT = Decimal("0.01")

# PayPal payout item statuses that are final
ITEM_DONE = {"SUCCESS", "UNCLAIMED"}  # UNCLAIMED = sent, waiting for the person to accept
ITEM_FAILED = PayoutItem.FAILED


class TipPoolError(Exception):
    pass


def business_of(night):
    return night.location.business


# --- Tips (money in) ---------------------------------------------------------
def create_tip_order(location, amount, table="", return_url=None, cancel_url=None, for_staff=None):
    """Create a PayPal order for a tip on this location's current shift, in the business's own PayPal.
    `for_staff`: a personal tip for that person (only when the location allows personal tips)."""
    business = location.business
    amount = Decimal(str(amount)).quantize(CENT)
    if amount < Decimal("1.00") or amount > Decimal("500.00"):
        raise TipPoolError(f"Tips must be between {business.symbol}1 and {business.symbol}500.")
    if for_staff is not None and location.tip_mode == location.TipMode.POOL:
        raise TipPoolError("Tips here are shared by the whole team.")
    if for_staff is None and location.tip_mode == location.TipMode.PERSONAL:
        raise TipPoolError("Please choose who you'd like to tip.")
    night = Night.current(location)
    who = f" for {for_staff.name}" if for_staff else ""
    order = get_client(business).create_order(amount, business.currency,
                                              description=f"Tip{who} at {location.display_name}"[:127],
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
    amount = Decimal(str(amount)).quantize(CENT)
    if amount <= 0 or amount > Decimal("20000"):
        raise TipPoolError("Enter an amount above 0.")
    fee = Decimal("0")
    if source == Tip.Source.CARD:
        fee = (amount * business_of(night).card_fee_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    tip = Tip.objects.create(night=night, amount=amount, source=source, staff=staff, note=note[:120], fee=fee,
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
    tip = Tip.objects.select_related("night__location__business").get(paypal_order_id=order_id)
    if tip.status == Tip.Status.COMPLETED:
        return tip
    client = get_client(business_of(tip.night))
    try:
        result = client.capture_order(order_id)
    except PayPalError as exc:
        issue = (exc.details.get("details") or [{}])[0].get("issue")
        if issue == "ORDER_ALREADY_CAPTURED":
            result = client.get_order(order_id)
        else:
            raise
    _apply_order_result(tip, result)
    return tip


def sync_pending_tips(night, max_checks=20):
    """Safety net: finish tips the guest approved but that were never captured
    (e.g. the PayPal window closed before it could tell our page)."""
    finished = 0
    client = get_client(business_of(night))
    # guests who opened PayPal and left: after 3 hours that order will never be paid
    night.tips.filter(status=Tip.Status.CREATED,
                      created_at__lt=timezone.now() - timedelta(hours=3)).update(status=Tip.Status.FAILED)
    for tip in night.tips.filter(status=Tip.Status.CREATED).order_by("-created_at")[:max_checks]:
        try:
            status = client.get_order(tip.paypal_order_id).get("status")
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
    fee = ((capture.get("seller_receivable_breakdown") or {}).get("paypal_fee") or {}).get("value")
    if fee is not None:
        tip.fee = Decimal(fee)
    tip.status = Tip.Status.COMPLETED if capture.get("status") == "COMPLETED" else tip.status
    tip.save()


def sync_tip(order_id):
    """Ask PayPal for the real state of a tip's order and apply it (used instead of trusting a webhook)."""
    tip = Tip.objects.select_related("night__location__business").get(paypal_order_id=order_id)
    if tip.status == Tip.Status.COMPLETED:
        return tip
    order = get_client(business_of(tip.night)).get_order(order_id)
    if order.get("status") == "APPROVED":
        return capture_tip(order_id)
    if order.get("status") == "COMPLETED":
        _apply_order_result(tip, order)
    return tip


def sync_capture(capture_id):
    """Ask PayPal whether a tip's payment was refunded or reversed (instead of trusting a webhook)."""
    tip = Tip.objects.select_related("night__location__business").get(paypal_capture_id=capture_id)
    status = get_client(business_of(tip.night)).get_capture(capture_id).get("status")
    if status in ("REFUNDED", "PARTIALLY_REFUNDED"):
        return tip_given_back(capture_id, "refund")
    if status == "REVERSED":
        return tip_given_back(capture_id, "reversal")
    return tip


def mark_capture_completed(order_id, capture_id):
    """Called from the webhook: PAYMENT.CAPTURE.COMPLETED."""
    updated = Tip.objects.filter(paypal_order_id=order_id).exclude(status=Tip.Status.COMPLETED)
    return updated.update(status=Tip.Status.COMPLETED, paypal_capture_id=capture_id)


def tip_given_back(capture_id, what):
    """A guest got a tip back (refund, chargeback) or opened a dispute.
    Before payout: the tip leaves the split. After payout: the business is told, since the money is gone."""
    tip = Tip.objects.select_related("night__location__business").filter(paypal_capture_id=capture_id).first()
    if tip is None:
        return None
    night = tip.night
    business = business_of(night)
    if what == "dispute":
        tip.problem = "The guest opened a dispute with PayPal"
        tip.save(update_fields=["problem"])
        raise_alert(business, f"A guest opened a PayPal dispute about a {business.symbol}{tip.amount} tip "
                              f"({night}). Answer it in your PayPal account.",
                    location=night.location, night=night, key=f"dispute:{capture_id}")
        return tip
    tip.status = Tip.Status.REFUNDED
    tip.problem = "Refunded to the guest" if what == "refund" else "Taken back by the guest's bank (chargeback)"
    tip.save(update_fields=["status", "problem"])
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        raise_alert(business, f"A {business.symbol}{tip.amount} tip from the {night} was "
                              f"{'refunded' if what == 'refund' else 'charged back'} after the team was paid. "
                              "The business covers it.",
                    location=night.location, night=night, key=f"refund:{capture_id}")
    else:
        _invalidate_split(night)
    return tip


# --- Split -------------------------------------------------------------------
def fees_to_share(night):
    """(fees taken out of shared tips, fees per person for personal tips) when the business passes fees on."""
    if business_of(night).fee_mode != Business.FeeMode.POOL:
        return Decimal("0"), {}
    tips = night.tips.filter(status=Tip.Status.COMPLETED)
    shared = sum((t.fee for t in tips if t.staff_id is None), Decimal("0"))
    personal = {}
    for t in tips:
        if t.staff_id is not None:
            personal[t.staff_id] = personal.get(t.staff_id, Decimal("0")) + t.fee
    return shared, personal


@transaction.atomic
def build_split(night, rule_set):
    """Calculate everyone's share for a night and store it (replaces an earlier draft)."""
    if night.status in (Night.Status.PAYING, Night.Status.PAID):
        raise TipPoolError("This shift has already been paid out.")
    shifts = list(night.shifts.select_related("staff"))
    if night.tip_total <= 0:
        raise TipPoolError("There are no completed tips for this shift yet.")
    shared_fees, personal_fees = fees_to_share(night)
    pool = max(night.pool_total - shared_fees, Decimal("0")).quantize(CENT)
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
    if shared_fees:
        result.notes.append(f"{business_of(night).symbol}{shared_fees.quantize(CENT)} in card and PayPal fees "
                            "was taken out before sharing.")
    _add_personal_tips(night, result, personal_fees)

    explanations = ai.explain_shares(result.total, rule_set.summary if rule_set else "Personal tips only.",
                                     result.shares, result.notes, role_names, symbol=business_of(night).symbol)
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
    from . import check
    check.run(night)
    return result


def _add_personal_tips(night, result, personal_fees):
    """Tips a guest left for one person go to that person in full (less their fees, if fees are passed on),
    on top of their pool share. Someone who only got personal tips (not on the roster) still gets a share."""
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
        amount = max(amount - personal_fees.get(staff.pk, Decimal("0")), Decimal("0")).quantize(CENT)
        person.amount += amount
        person.breakdown.append({"group": "Tips left for you", "pool": str(amount), "basis": "by name",
                                 "amount": str(amount), "personal": True})
        result.total += amount
    result.shares.sort(key=lambda p: (-p.amount, p.name))


# --- Payouts (money out) -----------------------------------------------------
def unconfirmed(shares):
    """People about to be paid who haven't confirmed where they get paid."""
    return [s.staff for s in shares if s.amount > 0 and s.staff.payout_confirmed_at is None]


def payout_destination(staff, currency):
    """PayPal Payouts fields for this person: their PayPal email, or Venmo (US dollars only)."""
    if staff.payout_method == StaffMember.PayoutMethod.VENMO and staff.venmo_phone:
        if currency != "USD":
            raise TipPoolError(f"{staff.name} chose Venmo, which only works in US dollars.")
        return {"recipient_type": "PHONE", "receiver": staff.venmo_phone, "wallet": "VENMO"}, f"Venmo {staff.venmo_phone}"
    return {"recipient_type": "EMAIL", "receiver": staff.paypal_email}, f"PayPal {staff.paypal_email}"


def check_balance(business, needed):
    """Raise if PayPal says the balance is too low for this payout (skipped when PayPal won't say)."""
    balance = get_client(business).available_balance(business.currency)
    if balance is not None and balance < needed:
        raise TipPoolError(f"Your PayPal balance is {business.symbol}{balance}, but this payout is "
                           f"{business.symbol}{needed}. Add money to your PayPal account first.")
    return balance


def pay_night(night, allow_unconfirmed=False):
    """Send one PayPal Payouts batch with everyone's share."""
    business = business_of(night)
    with transaction.atomic():
        night = Night.objects.select_for_update().get(pk=night.pk)
        if night.status != Night.Status.REVIEW:
            raise TipPoolError("Only a reviewed split can be paid.")
        shares = [s for s in night.shares.select_related("staff") if s.amount > 0]
        if not shares:
            raise TipPoolError("Nobody has anything to be paid.")
        waiting = unconfirmed(shares)
        if waiting and not allow_unconfirmed:
            raise TipPoolError("Not confirmed yet: " + ", ".join(p.name for p in waiting) + ". They confirm where "
                               "they get paid on their tips page. Check their address, then pay anyway if it's right.")
        destinations = {s.pk: payout_destination(s.staff, business.currency) for s in shares}
        check_balance(business, sum((s.amount for s in shares), Decimal("0")))
        # one fixed id per shift: if anything sends it twice, PayPal refuses to pay it twice
        batch = PayoutBatch.objects.create(night=night, sender_batch_id=f"tippool-shift-{night.pk}")
        for share in shares:
            PayoutItem.objects.create(batch=batch, share=share, receiver=destinations[share.pk][1],
                                      sender_item_id=f"{batch.sender_batch_id}-{share.staff_id}")
        batch.request_items = [{
            **destinations[item.share_id][0],
            "amount": str(item.share.amount),
            # a plain note we write ourselves (never AI text) goes to PayPal
            "note": f"Your tips for the {night} at {night.location.display_name}: {business.symbol}{item.share.amount}",
            "sender_item_id": item.sender_item_id,
        } for item in batch.items.select_related("share__staff")]
        batch.save(update_fields=["request_items"])
        night.status = Night.Status.PAYING
        night.hold = ""
        night.save()
    _send_batch(batch, business, subject=f"Your tips for {night.date:%b %d}")
    return batch


def _send_batch(batch, business, subject, replaces=None):
    """Send a stored payout batch. A clear "no" from PayPal undoes it; an unclear answer (server error,
    timeout) leaves it as UNKNOWN, to be checked by sending the very same request again."""
    items = [{**i, "amount": Decimal(i["amount"])} for i in batch.request_items]
    try:
        response = get_client(business).create_payout_batch(batch.sender_batch_id, items, subject=subject,
                                                            currency=business.currency)
    except PayPalError as exc:
        if exc.status is not None and 400 <= exc.status < 500 and batch.status != "UNKNOWN":
            # PayPal said no and sent nothing: undo so the manager can fix it and try again
            night = batch.night
            batch.delete()
            if replaces is not None:
                replaces.superseded = False
                replaces.save(update_fields=["superseded"])
            elif not batch.is_retry:
                night.status = Night.Status.REVIEW
                night.save(update_fields=["status"])
            raise
        batch.status = "UNKNOWN"
        batch.save(update_fields=["status"])
        raise TipPoolError("PayPal didn't answer clearly, so the payout may or may not have gone out. Don't pay "
                           "again: TipPool checks with PayPal (\"Check PayPal again\" on the payout page).") from exc
    header = response["batch_header"]
    batch.paypal_batch_id = header["payout_batch_id"]
    batch.status = header["batch_status"]
    batch.save(update_fields=["paypal_batch_id", "status"])
    return batch


def resend_payout(item):
    """Send one person's payout again after it failed or was taken back, to where they're paid now
    (e.g. after they corrected their email). The old payout stays in the history."""
    share = item.share
    night = share.night
    business = business_of(night)
    with transaction.atomic():
        item = PayoutItem.objects.select_for_update().get(pk=item.pk)
        if item.superseded or not item.failed:
            raise TipPoolError("Only a failed or returned payout can be sent again.")
        fields, receiver = payout_destination(share.staff, business.currency)
        # the id is tied to the payout it replaces: resending the same failed payout twice can't pay twice
        batch = PayoutBatch.objects.create(night=night, is_retry=True, sender_batch_id=f"tippool-again-{item.pk}")
        new = PayoutItem.objects.create(batch=batch, share=share, receiver=receiver,
                                        sender_item_id=f"{batch.sender_batch_id}-{share.staff_id}")
        batch.request_items = [{**fields, "amount": str(share.amount), "sender_item_id": new.sender_item_id,
                                "note": f"Your tips for the {night}, sent again: {business.symbol}{share.amount}"}]
        batch.save(update_fields=["request_items"])
        item.superseded = True
        item.save(update_fields=["superseded"])
    _send_batch(batch, business, subject=f"Your tips for {night.date:%b %d}", replaces=item)
    return new


def cancel_unclaimed(item):
    """Take back a payout nobody accepted (e.g. no PayPal account at that email). The money returns."""
    if item.status != "UNCLAIMED" or not item.paypal_item_id:
        raise TipPoolError("Only an unclaimed payout can be cancelled.")
    result = get_client(business_of(item.share.night)).cancel_payout_item(item.paypal_item_id)
    _apply_item(item, result)
    return item


def refresh_batch(batch):
    """Ask PayPal for the latest status of a batch and each person's payment."""
    if not batch.paypal_batch_id:
        if batch.status == "UNKNOWN" and batch.request_items:
            # same id + same request: PayPal answers with the batch if it exists, creates it if it doesn't
            _send_batch(batch, business_of(batch.night), subject=f"Your tips for {batch.night.date:%b %d}")
        return batch
    data = get_client(business_of(batch.night)).get_payout_batch(batch.paypal_batch_id)
    batch.status = data["batch_header"]["batch_status"]
    batch.save()
    by_sender_id = {i.sender_item_id: i for i in batch.items.all()}
    for row in data.get("items", []):
        item = by_sender_id.get(row["payout_item"].get("sender_item_id"))
        if item:
            _apply_item(item, row)
    _finish_night_if_done(batch.night)
    return batch


def apply_payout_item_event(resource):
    """Called from the webhook: PAYMENT.PAYOUTS-ITEM.*"""
    sender_item_id = resource.get("payout_item", {}).get("sender_item_id")
    item = PayoutItem.objects.filter(sender_item_id=sender_item_id).select_related("batch__night").first()
    if item:
        _apply_item(item, resource)
        _finish_night_if_done(item.batch.night)
    return item


def _apply_item(item, row):
    before = item.status
    item.status = row.get("transaction_status", item.status)
    item.paypal_item_id = row.get("payout_item_id", item.paypal_item_id)
    item.transaction_id = row.get("transaction_id", item.transaction_id) or ""
    fee = (row.get("payout_item_fee") or {}).get("value")
    if fee is not None:
        item.fee = Decimal(fee)
    errors = row.get("errors") or {}
    item.error = errors.get("message", "") if isinstance(errors, dict) else ""
    item.save()
    if item.status != before and not item.superseded:
        _alert_if_stuck(item)


def _alert_if_stuck(item):
    share = item.share
    night = share.night
    business = business_of(night)
    money = f"{business.symbol}{share.amount}"
    if item.status in ITEM_FAILED:
        raise_alert(business, f"{share.staff.name}'s payout of {money} for the {night} didn't go through "
                              f"({item.status.lower()}{': ' + item.error if item.error else ''}). "
                              "Check where they're paid and send it again.",
                    location=night.location, night=night, key=f"payout-failed:{item.pk}")
    elif item.status == "UNCLAIMED":
        raise_alert(business, f"{share.staff.name} hasn't accepted their {money} payout for the {night} "
                              f"({item.receiver}). PayPal returns it after 30 days if nobody claims it.",
                    location=night.location, night=night, key=f"payout-unclaimed:{item.pk}")


def _finish_night_if_done(night):
    statuses = set(PayoutItem.objects.filter(batch__night=night, superseded=False).values_list("status", flat=True))
    if statuses and statuses <= ITEM_DONE | ITEM_FAILED:
        if night.status != Night.Status.PAID:
            night.status = Night.Status.PAID
            night.save(update_fields=["status"])


# --- Where staff get paid ------------------------------------------------------
def confirm_payout(staff):
    staff.payout_confirmed_at = timezone.now()
    staff.save(update_fields=["payout_confirmed_at"])


def apply_payout_change(staff, payload):
    """A manager approved a worker's request to be paid somewhere else."""
    if payload.get("method") == StaffMember.PayoutMethod.VENMO:
        staff.payout_method = StaffMember.PayoutMethod.VENMO
        staff.venmo_phone = payload.get("phone", "")[:20]
    else:
        staff.payout_method = StaffMember.PayoutMethod.PAYPAL
        staff.paypal_email = payload.get("email", staff.paypal_email)
    staff.payout_confirmed_at = timezone.now()
    staff.save()
