"""Where the refresh token travels after login and refresh.

The browser gets it only as an httpOnly cookie. The mobile app keeps it
in the OS keystore and asks for it in the body with
``X-Token-Transport: body``. That header must not let a page script pull
the token out of the cookie through the refresh endpoint.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.views.auth import REFRESH_COOKIE_NAME

User = get_user_model()


class RefreshTokenTransportTests(APITestCase):

    def setUp(self):
        self.password = "x-test-pass-123"
        self.user = User.objects.create_user(
            email="transport@auth.test", password=self.password,
        )

    def login(self, **headers):
        return self.client.post(
            reverse("users:login"),
            {"email": self.user.email, "password": self.password},
            format="json",
            **headers,
        )

    def test_browser_login_gets_the_token_only_as_a_cookie(self):
        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        self.assertTrue(response.cookies[REFRESH_COOKIE_NAME]["httponly"])

    def test_mobile_login_gets_the_token_in_the_body(self):
        response = self.login(HTTP_X_TOKEN_TRANSPORT="body")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["refresh"])
        self.assertEqual(
            response.data["refresh"], response.cookies[REFRESH_COOKIE_NAME].value,
        )

    def test_the_header_does_not_unwrap_the_cookie_on_refresh(self):
        self.client.cookies[REFRESH_COOKIE_NAME] = str(RefreshToken.for_user(self.user))

        response = self.client.post(
            reverse("users:refresh-token"), {}, format="json",
            HTTP_X_TOKEN_TRANSPORT="body",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)

    def test_a_wrong_password_with_the_header_returns_no_token(self):
        response = self.client.post(
            reverse("users:login"),
            {"email": self.user.email, "password": "not-the-password"},
            format="json",
            HTTP_X_TOKEN_TRANSPORT="body",
        )

        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("refresh", response.data)
