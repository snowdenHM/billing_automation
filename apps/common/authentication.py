"""
Authentication helpers for public (AllowAny) endpoints.
"""
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class OptionalJWTAuthentication(JWTAuthentication):
    """JWT auth that treats a missing / expired / invalid token as anonymous
    instead of answering 401.

    Used on public endpoints (support chat, demo verification, verify-email
    resend) so a visitor with a stale token left in the browser isn't bounced
    into the frontend's "session expired" logout flow.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except (AuthenticationFailed, InvalidToken, TokenError):
            return None
