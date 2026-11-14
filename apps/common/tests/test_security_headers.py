"""Django's own responses must carry a policy.

Scope first, because it is the easy thing to get wrong: nginx serves the
SPA's HTML from ``dist/``, so this middleware never sees the pages an
operator loads. It covers ``/api/``, ``/admin/`` and ``/media/`` — and
``/admin/`` is the half with a real XSS surface, since it renders model
data as HTML.

The SPA's headers live in ``docs/nginx_security_headers.conf``.
"""

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.common.security_headers import SecurityHeadersMiddleware


class SecurityHeadersTests(SimpleTestCase):

    def get(self, path="/api/v1/"):
        return self.client.get(path)

    def test_a_policy_is_sent(self):
        response = self.get()
        self.assertTrue(
            "Content-Security-Policy" in response
            or "Content-Security-Policy-Report-Only" in response,
            "no Content-Security-Policy on a Django response",
        )

    def test_it_starts_in_report_only(self):
        """A policy that blocks the admin takes the admin down."""
        response = self.get()
        self.assertIn("Content-Security-Policy-Report-Only", response)
        self.assertNotIn("Content-Security-Policy", response)

    def test_the_policy_denies_the_dangerous_sources(self):
        policy = self.get()["Content-Security-Policy-Report-Only"]
        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertIn("base-uri 'self'", policy)
        self.assertIn("form-action 'self'", policy)

    def test_scripts_do_not_get_unsafe_inline(self):
        """Styles need it for unfold and DRF; scripts are the half that matters."""
        policy = self.get()["Content-Security-Policy-Report-Only"]
        script = [d for d in policy.split(";") if d.strip().startswith("script-src")][0]
        self.assertNotIn("unsafe-inline", script)
        self.assertNotIn("unsafe-eval", script)

    def test_permissions_policy_is_sent(self):
        policy = self.get()["Permissions-Policy"]
        for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
            self.assertIn(feature, policy)

    def test_enforcing_switches_the_header_name(self):
        with override_settings(CSP_ENFORCE=True):
            mw = SecurityHeadersMiddleware(lambda r: _Bare())
            out = mw(None)
        self.assertIn("Content-Security-Policy", out)
        self.assertNotIn("Content-Security-Policy-Report-Only", out)

    def test_an_existing_policy_is_not_overwritten(self):
        """Something upstream may have chosen a narrower policy."""
        existing = _Bare()
        existing["Content-Security-Policy"] = "default-src 'none'"
        mw = SecurityHeadersMiddleware(lambda r: existing)
        out = mw(None)
        self.assertEqual(out["Content-Security-Policy"], "default-src 'none'")


class _Bare(dict):
    """The smallest thing that behaves like an HttpResponse for headers."""

    def __contains__(self, key):
        return dict.__contains__(self, key)
