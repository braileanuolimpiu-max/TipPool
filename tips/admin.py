from django.contrib import admin

from .models import (Business, Invite, Location, Manager, Membership, Night, PayoutBatch, PayoutItem, Role, Share, Shift, StaffMember, Tip,
                     TipRuleSet, WebhookEvent)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "paypal_email")
    inlines = [MembershipInline]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("display_name", "slug", "business_type", "timezone")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "location", "no_tips")
    list_filter = ("location",)


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
    list_display = ("date", "location", "status", "tip_total")
    list_filter = ("location", "status")
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


class ManagerInline(admin.TabularInline):
    model = Manager
    extra = 0


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    inlines = [ManagerInline]


admin.site.register(Invite)
