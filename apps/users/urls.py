from django.urls import path
from .views import (
    register_view,
    login_view,
    password_reset_request_view,
    password_reset_confirm_view,
    change_password_view,
    me_view,
    verify_email_view,
    refresh_token_view,
)

app_name = "users"

urlpatterns = [
    # Auth
    path("auth/register/", register_view, name="register"),
    path("auth/login/", login_view, name="login"),
    path("auth/refresh/", refresh_token_view, name="refresh-token"),
    path("auth/password/reset/", password_reset_request_view, name="password-reset"),
    path("auth/password/confirm/", password_reset_confirm_view, name="password-confirm"),
    path("auth/password/change/", change_password_view, name="password-change"),
    path("auth/verify-email/<str:uidb64>/<str:token>/", verify_email_view, name="verify-email"),

    # Profile
    path("me/", me_view, name="me"),
]
