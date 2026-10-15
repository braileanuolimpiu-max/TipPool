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
    # manager
    path("qr/", views.qr_page, name="qr_page"),
    path("staff/", views.staff_list, name="staff_list"),
    path("staff/<int:pk>/toggle/", views.staff_toggle, name="staff_toggle"),
    path("rules/", views.rules_page, name="rules"),
    path("close/", views.close_night, name="close_night"),
    path("nights/<int:pk>/close/", views.close_night, name="close_night_pk"),
    path("nights/<int:pk>/review/", views.review, name="review"),
    path("nights/<int:pk>/pay/", views.pay, name="pay"),
    path("nights/<int:pk>/payout/", views.payout_status, name="payout_status"),
    path("nights/<int:pk>/payout.json", views.payout_status_json, name="payout_status_json"),
    # staff (secret link)
    path("me/<str:token>/", views.my_tips, name="my_tips"),
    # PayPal
    path("webhooks/paypal/", views.paypal_webhook, name="paypal_webhook"),
]
