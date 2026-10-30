"""Payouts sent and followed to the end by a Render Workflow (workflows/main.py).

When a manager (or the agent, or the automatic close-out) pays a shift, every check runs right away as before:
the split they confirmed, the owner's limit, who confirmed where they're paid, the PayPal balance. Then the
batch is only prepared, and a run of the `pay_shift` workflow task sends it and follows it until every person's
payout is final, retrying on its own if PayPal or the network fails. Retrying is safe: the batch has one fixed
id, and PayPal never pays one id twice.

The run's steps are kept on a PayoutRun row and in the shared cache (a Render Key Value store), so the payout
page can show them live. Without Render Workflows set up (RENDER_API_KEY and TIPPOOL_WORKFLOW_SLUG), or if Render
can't be reached, the payout is sent right away by the web server, exactly as before."""
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from . import services
from .models import Night, PayoutItem, PayoutRun
from .paypal import PayPalError

log = logging.getLogger(__name__)

TASK = "pay_shift"
FOLLOW_FOR = 20 * 60  # a run follows PayPal for up to 20 minutes; the payout page keeps checking after that
FOLLOW_EVERY = 8


def enabled():
    return bool(settings.RENDER_API_KEY and settings.TIPPOOL_WORKFLOW_SLUG)


def _client():
    from render import Render
    return Render(token=settings.RENDER_API_KEY)


# --- the run's steps, as the manager sees them -----------------------------------
def _live_key(run_id):
    return f"payout-run:{run_id}"


def step(run, text, kind="ok", state=None):
    """Add one line to the run (kind: ok, wait or bad) and publish it for the payout page."""
    run.steps = [*run.steps, {"at": timezone.now().isoformat(timespec="seconds"), "text": text[:240], "kind": kind}]
    fields = ["steps"]
    if state:
        run.state = state
        fields.append("state")
        if state not in PayoutRun.ACTIVE:
            run.finished_at = timezone.now()
            fields.append("finished_at")
    run.save(update_fields=fields)
    publish(run)


def publish(run):
    try:
        cache.set(_live_key(run.pk), live(run), 3600)
    except Exception:  # the cache is a convenience: the database has it all
        log.warning("could not publish payout run %s", run.pk, exc_info=True)


def live(run):
    return {"id": run.pk, "state": run.state, "label": run.get_state_display(), "active": run.active,
            "on_render": run.on_render, "task_run_id": run.task_run_id, "steps": run.steps}


def live_status(run_id):
    """What the payout page polls: from the cache when it's there, without touching the database."""
    try:
        data = cache.get(_live_key(run_id))
    except Exception:
        data = None
    if data is None:
        run = PayoutRun.objects.filter(pk=run_id).first()
        data = live(run) if run else None
    return data


# --- the web side: start a payout --------------------------------------------------
def pay(night, allow_unconfirmed=False, by_owner=False, expected_split=None):
    """Pay a shift. Same checks, same errors and same result as services.pay_night; with Render Workflows set
    up, the sending happens in a workflow run instead of in this request."""
    if not enabled():
        return services.pay_night(night, allow_unconfirmed=allow_unconfirmed, by_owner=by_owner,
                                  expected_split=expected_split)
    batch = services.pay_night(night, allow_unconfirmed=allow_unconfirmed, by_owner=by_owner,
                               expected_split=expected_split, send=False)
    run = PayoutRun.objects.create(night=batch.night, batch=batch)
    people = batch.items.count()
    step(run, f"Checked: the split, the owner's limit, where {people} {'person is' if people == 1 else 'people are'} "
              "paid, and the PayPal balance")
    try:
        started = _client().workflows.start_task(f"{settings.TIPPOOL_WORKFLOW_SLUG}/{TASK}", [run.pk],
                                                 idempotency_key=f"tippool-payout-run-{run.pk}")
    except Exception:
        log.warning("Render Workflows didn't start payout run %s; sending it right away", run.pk, exc_info=True)
        step(run, "Render Workflows didn't answer, so TipPool is sending it right away", "wait", PayoutRun.State.SENDING)
        try:
            sent = services.send_prepared(batch)
        except PayPalError as exc:
            step(run, f"PayPal refused it: {exc}. Nobody was paid.", "bad", PayoutRun.State.STOPPED)
            raise
        except services.TipPoolError:
            step(run, "PayPal didn't answer clearly. \"Check PayPal again\" sends the very same request.", "bad",
                 PayoutRun.State.STUCK)
            raise
        if sent is not None and sent.paypal_batch_id:
            step(run, f"PayPal accepted it (batch {sent.paypal_batch_id}). This page follows it from here.",
                 state=PayoutRun.State.DONE)
        return sent or batch
    run.task_run_id = getattr(started, "id", "") or ""
    run.save(update_fields=["task_run_id"])
    step(run, "Handed to a Render Workflow: it sends the payout and follows it until everyone is paid", "wait")
    return batch


def current_run(night):
    return night.payout_runs.first()


# --- the workflow side: the two steps of a run (called from workflows/main.py, each in its own task) ---------
def _fresh():
    close_old_connections()  # a task may wait a long time between database calls


def send_step(run_id):
    """Send the prepared batch. Returns {"ok": True} once PayPal has it, {"ok": False} when nothing will be paid
    (refused, undone, frozen). Raises when PayPal's answer was unclear, so the workflow tries again: the very
    same request, which PayPal never pays twice."""
    _fresh()
    run = PayoutRun.objects.select_related("night", "batch").get(pk=run_id)
    batch = run.batch
    if batch is None:
        step(run, "This payout was undone before it was sent. Nobody was paid.", "bad", PayoutRun.State.STOPPED)
        return {"ok": False}
    if batch.paypal_batch_id:
        if run.state != PayoutRun.State.FOLLOWING:
            step(run, f"PayPal already has it (batch {batch.paypal_batch_id})", state=PayoutRun.State.FOLLOWING)
        return {"ok": True}
    business = services.business_of(run.night)
    try:
        services.check_not_frozen(business)
    except services.TipPoolError:
        if services.undo_unsent(batch):
            step(run, "Stopped before sending: payouts were frozen in the meantime. Nobody was paid, and the shift "
                      "is back to review.", "bad", PayoutRun.State.STOPPED)
        else:  # it may have reached PayPal already: frozen means nothing more is sent, the owner checks it
            step(run, "Payouts were frozen while PayPal's answer was unclear. Nothing more is sent: check it in "
                      "PayPal once payouts are back on.", "bad", PayoutRun.State.STUCK)
        return {"ok": False}
    attempt = sum(1 for s in run.steps if s["text"].startswith("Sending")) + 1
    step(run, f"Sending {batch.items.count()} payouts to PayPal" + (f" (try {attempt})" if attempt > 1 else ""),
         "wait", PayoutRun.State.SENDING)
    try:
        sent = services.send_prepared(batch)
    except PayPalError as exc:
        # a clear "no": _send_batch already undid the batch, so the manager can fix it and pay again
        step(run, f"PayPal refused it: {exc}. Nobody was paid, and the shift is back to review.", "bad",
             PayoutRun.State.STOPPED)
        return {"ok": False, "refused": str(exc)}
    except services.TipPoolError:
        step(run, "PayPal didn't answer clearly. Trying the very same request again in a moment.", "wait")
        raise
    if sent is None:
        step(run, "This payout was undone before it was sent. Nobody was paid.", "bad", PayoutRun.State.STOPPED)
        return {"ok": False}
    step(run, f"PayPal accepted the payout (batch {sent.paypal_batch_id})", state=PayoutRun.State.FOLLOWING)
    return {"ok": True}


def _settled(night):
    current = list(PayoutItem.objects.filter(batch__night=night, superseded=False).select_related("share__staff"))
    return current, bool(current) and all(i.status in services.ITEM_DONE | services.ITEM_FAILED for i in current)


def _item_line(item, sym):
    name, money = item.share.staff.name, f"{sym}{item.share.amount}"
    if item.status == "SUCCESS":
        return f"{name}: {money} paid", "ok"
    if item.status == "UNCLAIMED":
        return f"{name}: {money} sent, waiting for them to accept it in PayPal", "wait"
    if item.status in services.ITEM_FAILED:
        return f"{name}: {money} didn't arrive ({item.status.lower()}). It can be sent again.", "bad"
    return None, None


def follow_step(run_id, follow_for=FOLLOW_FOR, every=FOLLOW_EVERY, sleep=time.sleep):
    """Ask PayPal about the batch every few seconds until every person's payout is final (never sends anything).
    Each payout that becomes final is added to the run as it happens."""
    _fresh()
    run = PayoutRun.objects.select_related("night", "batch").get(pk=run_id)
    if run.batch is None or not run.batch.paypal_batch_id:
        return {"ok": False}
    sym = services.business_of(run.night).symbol
    told = {s["text"] for s in run.steps}
    deadline = time.monotonic() + follow_for
    while True:
        try:
            services.refresh_batch(run.batch)
        except (PayPalError, services.TipPoolError):
            log.warning("could not check payout run %s with PayPal", run_id, exc_info=True)
        items, settled = _settled(run.night)
        for item in items:
            text, kind = _item_line(item, sym)
            if text and text not in told:
                told.add(text)
                step(run, text, kind)
        if settled:
            failed = sum(1 for i in items if i.status in services.ITEM_FAILED)
            night = Night.objects.get(pk=run.night_id)
            step(run, "Every payout is final." + (f" {failed} didn't arrive: send them again from this page."
                                                   if failed else " The shift is paid." if night.status == Night.Status.PAID
                                                   else ""), "bad" if failed else "ok", PayoutRun.State.DONE)
            return {"ok": True, "failed": failed}
        if time.monotonic() >= deadline:
            step(run, "PayPal is still working on some payouts. This page keeps checking.", "wait",
                 PayoutRun.State.DONE)
            return {"ok": True, "pending": True}
        sleep(every)
        _fresh()


def gave_up(run_id, what):
    """The workflow ran out of tries (PayPal kept answering unclearly, or the task itself failed)."""
    _fresh()
    run = PayoutRun.objects.get(pk=run_id)
    if not run.active:
        return
    if what == "send":
        step(run, "PayPal still hasn't answered clearly after several tries. Don't pay again: press \"Check PayPal "
                  "again\", which sends the very same request.", "bad", PayoutRun.State.STUCK)
    else:
        step(run, "Stopped following the payout. This page keeps checking with PayPal.", "wait", PayoutRun.State.DONE)
