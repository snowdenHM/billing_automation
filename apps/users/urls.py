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
    user_list_view,
    user_detail_view,
    user_update_view,
    user_delete_view,
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

    # User Management CRUD
    # path("users/", user_list_view, name="user-list"),
    # path("users/<uuid:user_id>/", user_detail_view, name="user-detail"),
    # path("users/<uuid:user_id>/update/", user_update_view, name="user-update"),
    # path("users/<uuid:user_id>/delete/", user_delete_view, name="user-delete"),
]
