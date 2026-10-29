"""The "Jump to" search in the top bar: every page and every settings card a person can open, with the
words people might type for it ("close time" finds Closing time). Shift leads only see what they can open."""
from django.urls import reverse

# (title, where it is, url name, #anchor, extra words, who: "all" | "manager" | "operator")
PAGES = [
    ("Current shift", "Page", "dashboard", "", "dashboard home today tonight live tips now", "all"),
    ("All locations", "Page", "overview", "", "overview board every location compare normal", "multi"),
    ("Close out", "Page", "close_night", "", "who worked roster hours end of shift split calculate clock", "all"),
    ("Past shifts", "Page", "history", "", "history previous paid shifts payouts list", "all"),
    ("Messages", "Page", "messages_page", "", "flags problems team wrote alerts inbox replies", "all"),
    ("QR codes", "Page", "qr_page", "", "qr code tip code table tents poster download png svg", "all"),
    ("Print QR codes", "QR codes", "qr_print", "", "print pdf table tents cards stickers poster", "all"),
    ("Reports", "Page", "reports", "", "report totals csv export payroll accountant per person per shift", "manager"),
    ("Ask about your tips", "Reports", "reports", "ask-form", "ask question ai agent chat pay everyone", "manager"),
    ("Team", "Page", "staff_list", "", "staff workers employees people members tips link", "manager"),
    ("Add someone", "Team", "staff_list", "add-someone", "add worker hire new staff employee paypal email", "manager"),
    ("Add several at once", "Team", "staff_list", "add-several", "bulk import csv list paste many workers", "manager"),
    ("Roles", "Team", "staff_list", "roles", "roles jobs server bartender busser host preset", "manager"),
    ("Add a role", "Team", "staff_list", "add-role", "new role job title", "manager"),
    ("Tip rules", "Page", "rules", "", "rules split percentages pool share bartenders servers law", "manager"),
    ("Activity", "Page", "activity", "", "activity log changes history audit who did what", "manager"),
    ("Summary email", "Page", "digest_preview", "", "digest weekly daily summary email preview", "manager"),
    ("Settings", "Page", "settings", "", "settings options preferences configuration", "manager"),
    ("Your business", "Settings · 01", "settings", "business", "business name location name time zone", "manager"),
    ("Tip page", "Settings · 02", "settings", "tip-page", "tip buttons percent amounts guests message", "manager"),
    ("How tips are shared", "Settings · 03", "settings", "sharing", "pool personal team tips guest chooses", "manager"),
    ("Closing time", "Settings · 04", "settings", "closing", "close time closing hours schedule automatic close-out days", "manager"),
    ("When a shift closes", "Settings · 05", "settings", "after-close", "automatic payout auto pay review window after close", "manager"),
    ("Payments", "Settings · 06", "settings", "payments", "paypal connect currency card fee venmo payout account live", "manager"),
    ("Owner controls", "Settings · 07", "settings", "owner", "approval limit smart alerts summary email freeze emergency stop", "manager"),
    ("Freeze all payouts", "Settings · 07", "settings", "owner", "freeze stop payouts emergency fraud", "manager"),
    ("Locations", "Settings · 08", "settings", "locations", "add location archive branches", "manager"),
    ("Managers", "Settings · 09", "settings", "managers", "invite manager shift lead access level", "manager"),
    ("Recent changes", "Settings · 10", "settings", "changes", "changes log history", "manager"),
    ("Your sign-in", "Settings · 11", "settings", "signin", "two-step 2fa authenticator security password sign in", "manager"),
    ("Two-step sign-in", "Your sign-in", "two_step", "", "two-step 2fa authenticator code security", "lead"),
    ("Change password", "Your sign-in", "password_change", "", "password change reset security", "all"),
    ("Platform settings", "Platform", "platform_settings", "", "platform site operator", "operator"),
    ("Email", "Platform · 01", "platform_settings", "email", "email brevo smtp gmail sender", "operator"),
    ("AI", "Platform · 02", "platform_settings", "ai", "ai gemini claude chatgpt model key limit", "operator"),
    ("TipPool's PayPal", "Platform · 03", "platform_settings", "paypal", "paypal sandbox platform account webhook", "operator"),
    ("The demo", "Platform · 04", "platform_settings", "demo", "demo buyer sandbox accounts", "operator"),
    ("Scheduler", "Platform · 05", "platform_settings", "scheduler", "scheduler cron timer keep awake tick", "operator"),
    ("Contact", "Platform · 06", "platform_settings", "contact", "contact email privacy terms", "operator"),
    ("Bot check on tip pages", "Platform · 07", "platform_settings", "botcheck", "bot check turnstile cloudflare captcha", "operator"),
]


def entries(request, profile, operator, several_locations):
    """What this person can jump to: [{"t", "w", "u", "k"}]."""
    lead = profile.is_lead
    out = []
    for title, where, name, anchor, words, who in PAGES:
        if who == "manager" and lead or who == "operator" and not operator:
            continue
        if who == "lead" and not lead or who == "multi" and not several_locations:
            continue
        out.append({"t": title, "w": where, "u": reverse(name) + (f"#{anchor}" if anchor else ""), "k": words})
    location = getattr(request, "location", None)
    if location is not None:  # the guest page as guests see it
        out.append({"t": "Guest tip page", "w": "Page", "u": reverse("tip_page") + f"?at={location.slug}",
                    "k": "guest tip page preview customer pay"})
    return out
