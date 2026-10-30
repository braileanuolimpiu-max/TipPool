"""TipPool's Render Workflow: pays a shift and follows the payout until every person's money is final.

    pay_shift(run_id)      the run: send the payout, then follow it (each a task of its own, with its own retries)
    send_payout(run_id)    send the prepared PayPal batch; an unclear answer from PayPal is retried with the
                           very same request (same id: PayPal never pays it twice)
    follow_payout(run_id)  ask PayPal until every payout is final; never sends anything

The web app starts pay_shift when a shift is paid (tips/payout_runs.py). The steps themselves live in
tips/payout_runs.py and use TipPool's own database, so this service needs the web service's DATABASE_URL,
DJANGO_SECRET_KEY and TIPPOOL_ENCRYPTION_KEY (render.yaml copies them across).

Run on Render as a Workflow service: build `pip install -r requirements.txt`, start `python workflows/main.py`."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from render import Retry, TaskContext, Workflows  # noqa: E402

from tips import payout_runs  # noqa: E402

app = Workflows()


# TipPool's database calls are ordinary blocking code, so each step runs in a thread, off the task's event loop.
@app.task(retry=Retry(max_retries=5, wait_duration_ms=3000, backoff_scaling=2.0), timeout_seconds=180)
async def send_payout(ctx: TaskContext, run_id: int) -> dict:
    return await asyncio.to_thread(payout_runs.send_step, run_id)


@app.task(retry=Retry(max_retries=2, wait_duration_ms=10000, backoff_scaling=2.0), timeout_seconds=1800)
async def follow_payout(ctx: TaskContext, run_id: int) -> dict:
    return await asyncio.to_thread(payout_runs.follow_step, run_id)


@app.task(timeout_seconds=3600)
async def pay_shift(ctx: TaskContext, run_id: int) -> dict:
    try:
        sent = await ctx.run(send_payout, run_id)
    except Exception:  # out of tries: tell the manager what to do
        await asyncio.to_thread(payout_runs.gave_up, run_id, "send")
        return {"ok": False, "stuck": True}
    if not sent.get("ok"):
        return sent
    try:
        return await ctx.run(follow_payout, run_id)
    except Exception:
        await asyncio.to_thread(payout_runs.gave_up, run_id, "follow")
        return {"ok": True, "following": False}


if __name__ == "__main__":
    app.start()
