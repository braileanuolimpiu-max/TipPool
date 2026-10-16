from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # customer (public)
    path("tip/", views.tip_page, name="tip_page"),
    path("tip/thanks/", views.tip_thanks, name="tip_thanks"),
    path("api/orders/", views.api_create_order, name="api_create_order"),
    path("api/orders/<str:order_id>/capture/", views.api_capture_order, name="api_capture_order"),
    path("qr.svg", views.qr_svg, name="qr_svg"),
    path("qr/show/", views.qr_show, name="qr_show"),
    # manager
    path("qr/", views.qr_page, name="qr_page"),
    path("history/", views.history, name="history"),
    path("messages/", views.messages_page, name="messages_page"),
    path("reports/", views.reports_page, name="reports"),
    path("clock/<int:pk>/out/", views.clock_out_for, name="clock_out_for"),
    path("settings/", views.settings_page, name="settings"),
    path("locations/switch/", views.location_switch, name="location_switch"),
    path("locations/add/", views.location_add, name="location_add"),
    path("staff/", views.staff_list, name="staff_list"),
    path("staff/<int:pk>/toggle/", views.staff_toggle, name="staff_toggle"),
    path("staff/<int:pk>/role/", views.staff_role, name="staff_role"),
    path("staff/<int:pk>/schedule/", views.staff_schedule, name="staff_schedule"),
    path("roles/add/", views.role_add, name="role_add"),
    path("roles/<int:pk>/remove/", views.role_remove, name="role_remove"),
    path("roles/preset/", views.role_preset, name="role_preset"),
    path("rules/", views.rules_page, name="rules"),
    path("close/", views.close_night, name="close_night"),
    path("nights/<int:pk>/close/", views.close_night, name="close_night_pk"),
    path("nights/<int:pk>/review/", views.review, name="review"),
    path("nights/<int:pk>/tips/add/", views.night_tip_add, name="night_tip_add"),
    path("tips/<int:pk>/remove/", views.night_tip_remove, name="night_tip_remove"),
    path("nights/<int:pk>/pay/", views.pay, name="pay"),
    path("nights/<int:pk>/payout/", views.payout_status, name="payout_status"),
    path("nights/<int:pk>/payout.json", views.payout_status_json, name="payout_status_json"),
    # staff (secret link)
    path("me/<str:token>/", views.my_tips, name="my_tips"),
    # PayPal
    path("webhooks/paypal/", views.paypal_webhook, name="paypal_webhook"),
]
