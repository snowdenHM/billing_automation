# apps/users/tokens.py
"""
Email-verification token (Client Correction 57).

Kept separate from the password-reset token: it uses its own salt, so a
verification link can never be replayed on ``auth/password/confirm/``, and
the hash includes ``email_verified`` so the link stops working once used.
"""
from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    key_salt = "apps.users.tokens.EmailVerificationTokenGenerator"

    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{user.email}{user.email_verified}{timestamp}"


email_verification_token = EmailVerificationTokenGenerator()
