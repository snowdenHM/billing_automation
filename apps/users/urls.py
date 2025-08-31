from django.urls import path
from .views import (
    RegisterView,
    LoginView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
    ChangePasswordView,
    MeView,
    VerifyEmailView,
)

app_name = "users"

urlpatterns = [
    # Auth
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    # Optional alias so /api/v1/login/ works too
    # path("login/", LoginView.as_view(), name="login-alias"),
    path("auth/password/reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("auth/password/confirm/", PasswordResetConfirmView.as_view(), name="password-confirm"),
    path("auth/password/change/", ChangePasswordView.as_view(), name="password-change"),
    path("auth/verify-email/<str:uidb64>/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
    # Profile
    path("me/", MeView.as_view(), name="me"),
]
