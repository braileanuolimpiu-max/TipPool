from django.contrib import admin

from .models import Night, PayoutBatch, PayoutItem, Share, Shift, StaffMember, Tip, TipRuleSet, WebhookEvent


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "default_role", "paypal_email", "active")
    list_filter = ("default_role", "active")


class TipInline(admin.TabularInline):
    model = Tip
    extra = 0


class ShiftInline(admin.TabularInline):
    model = Shift
    extra = 0


class ShareInline(admin.TabularInline):
    model = Share
    extra = 0


@admin.register(Night)
class NightAdmin(admin.ModelAdmin):
    list_display = ("date", "status", "tip_total")
    inlines = [TipInline, ShiftInline, ShareInline]


class PayoutItemInline(admin.TabularInline):
    model = PayoutItem
    extra = 0


@admin.register(PayoutBatch)
class PayoutBatchAdmin(admin.ModelAdmin):
    list_display = ("night", "paypal_batch_id", "status", "created_at")
    inlines = [PayoutItemInline]


admin.site.register(TipRuleSet)
admin.site.register(Tip)
admin.site.register(WebhookEvent)
