# apps/users/views/__init__.py
"""
Users views package.
Re-exports all view functions so existing imports continue to work.
"""

from .auth import (
    register_view,
    login_view,
    password_reset_request_view,
    password_reset_confirm_view,
    change_password_view,
    verify_email_view,
    refresh_token_view,
)

from .profile import (
    me_view,
    user_list_view,
    user_detail_view,
    user_update_view,
    user_delete_view,
)

__all__ = [
    # Auth
    "register_view",
    "login_view",
    "password_reset_request_view",
    "password_reset_confirm_view",
    "change_password_view",
    "verify_email_view",
    "refresh_token_view",
    # Profile & User Management
    "me_view",
    "user_list_view",
    "user_detail_view",
    "user_update_view",
    "user_delete_view",
]
