"""Changing a password must end the other sessions.

``ROTATE_REFRESH_TOKENS`` and ``BLACKLIST_AFTER_ROTATION`` only retire a
refresh token once it is *used*. A stolen token that is simply held
stayed valid for its full seven days — so a user who changed their
password because they suspected a compromise did not actually lock the
attacker out, which is the one thing they changed it for.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class PasswordChangeRevokesTokensTests(APITestCase):

    def setUp(self):
        self.password = "x-test-pass-123"
        self.user = User.objects.create_user(
            email="revoke@auth.test", password=self.password,
        )
        # A session the "attacker" holds, minted before the change.
        self.stolen = RefreshToken.for_user(self.user)

    def refresh_works(self, token):
        response = self.client.post(
            reverse("users:refresh-token"), {"refresh": str(token)}, format="json",
        )
        return response.status_code == status.HTTP_200_OK

    def test_the_stolen_token_works_before_the_change(self):
        """Baseline — otherwise the test below proves nothing."""
        self.assertTrue(self.refresh_works(self.stolen))

    def test_changing_the_password_revokes_outstanding_tokens(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("users:password-change"),
            {
                "old_password": self.password,
                "new_password": "y-test-pass-456",
                "confirm_password": "y-test-pass-456",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(None)
        self.assertFalse(
            self.refresh_works(self.stolen),
            "a refresh token minted before the password change still works",
        )

    def test_a_wrong_current_password_changes_nothing(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("users:password-change"),
            {
                "old_password": "not-the-password",
                "new_password": "y-test-pass-456",
                "confirm_password": "y-test-pass-456",
            },
            format="json",
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(None)
        self.assertTrue(
            self.refresh_works(self.stolen),
            "a refused password change revoked sessions anyway",
        )
