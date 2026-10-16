from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from tips.ratelimit import limited

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html",
                                                redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/reset/", limited("password-reset", 5, 3600)(auth_views.PasswordResetView.as_view()),
         name="password_reset"),
    path("password/reset/sent/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("password/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("", include("tips.urls")),
]
