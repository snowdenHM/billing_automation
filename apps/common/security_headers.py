"""Content-Security-Policy and Permissions-Policy for Django's responses.

Scope, because it matters: nginx serves the SPA's HTML straight from
``dist/``, so this middleware never sees the pages an operator loads.
What it does cover is everything Django answers — ``/admin/``,
``/api/`` and ``/media/`` — and ``/admin/`` is the half that has a real
XSS surface, since it renders model data as HTML.

The SPA's own headers have to be set in the nginx server block. The
policy to paste is kept beside this file, in
``docs/nginx_security_headers.conf``, so the two stay together.

**Report-Only by default.** A CSP that blocks something the admin needs
takes the admin down, and the way to find out what it needs is to watch
it report. Set ``CSP_ENFORCE=True`` once the reports are quiet.
"""

from django.conf import settings

#: Sources the Django-rendered pages legitimately pull from.
#:
#: ``unsafe-inline`` for styles is django-unfold and the DRF browsable
#: API, both of which emit inline style attributes. Scripts do NOT get
#: it: that is the half that matters, and the admin's own JS all comes
#: from ``/static/``.
_POLICY = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src": "'self' data: https://fonts.gstatic.com",
    "img-src": "'self' data: blob:",
    "connect-src": "'self'",
    # The bill viewer embeds /media/ in an iframe on the same origin.
    "frame-ancestors": "'self'",
    "frame-src": "'self' blob:",
    "object-src": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
}

#: Browser features this product never uses. Denying them costs nothing
#: and removes them from anything that does manage to run here.
_PERMISSIONS = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)


def _policy_string():
    return "; ".join(f"{k} {v}" for k, v in _POLICY.items())


class SecurityHeadersMiddleware:
    """Add CSP and Permissions-Policy to responses Django produces."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.enforce = bool(getattr(settings, "CSP_ENFORCE", False))
        self.policy = _policy_string()

    def __call__(self, request):
        response = self.get_response(request)

        header = (
            "Content-Security-Policy" if self.enforce
            else "Content-Security-Policy-Report-Only"
        )
        # Never overwrite a policy something upstream already chose.
        if "Content-Security-Policy" not in response and \
                "Content-Security-Policy-Report-Only" not in response:
            response[header] = self.policy

        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = _PERMISSIONS

        return response
