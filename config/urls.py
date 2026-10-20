from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import redirect_to_login
from django.urls import include, path

from tips.accounts import ResetConfirmView, TwoStepLoginView, change_password, login_code
from tips.ratelimit import limited

# the admin has no sign-in of its own: it would skip the two-step code
admin.site.login = lambda request, extra_context=None: redirect_to_login(request.GET.get("next") or "/admin/")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", TwoStepLoginView.as_view(), name="login"),
    path("login/code/", login_code, name="login_code"),
    path("password/change/", change_password, name="password_change"),
    path("password/change/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/reset/", limited("password-reset", 5, 3600)(auth_views.PasswordResetView.as_view()),
         name="password_reset"),
    path("password/reset/sent/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", ResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("password/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("", include("tips.urls")),
]

handler404 = "tips.errors.not_found"
