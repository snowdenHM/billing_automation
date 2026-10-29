"""
Common views for the application.
"""
import logging
import os

from django.conf import settings
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError
from django.http import Http404, HttpResponseForbidden
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve as django_serve
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.common.authentication import OptionalJWTAuthentication
from apps.common.models import DemoRequest
from apps.common.serializers import DemoRequestSerializer
from apps.common.utils import send_simple_email, send_templated_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signed-URL helpers for authenticated media access.
#
# ``<img>`` and ``<iframe>`` tags cannot send an ``Authorization`` header, so
# JWT auth on the media endpoint would break the file viewer. Instead we
# hand out short-lived HMAC-signed URLs: the API returns
# ``/media/bills/…?token=<signed>`` and the file view verifies that token
# server-side. Tokens are salt-namespaced so leaking a bill-file token
# doesn't affect any other Django signer usage.
# ---------------------------------------------------------------------------

_BILL_FILE_SIGNER_SALT = "billmunshi.bill-file"
#: Separates the media path from the user id inside a signed token.
#: A character that cannot occur in either, so the split is unambiguous.
_TOKEN_SEP = "\x1f"
# 15 minutes. The token is a bearer capability — whoever holds the URL
# can fetch the document — so its lifetime is the window in which a
# leaked URL (referrer, screenshot, support ticket, proxy log) is still
# usable. An hour was far longer than any modal needs.
_BILL_FILE_TOKEN_TTL = 900


def generate_signed_bill_file_url(file_field, request=None):
    """Return an absolute signed URL for ``file_field`` (a ``FieldFile``).

    Serializers call this to expose a ``file_url`` field the frontend can
    drop into ``<img src=…>`` / ``<iframe src=…>`` directly. The token
    embeds the media path + timestamp; ``serve_bill_file`` verifies it on
    every request.
    """
    if not file_field:
        return None
    try:
        media_path = file_field.name  # e.g. ``bills/foo.pdf``
    except Exception:
        return None
    if not media_path:
        return None
    # Bind the token to the user it was minted for, not just to the
    # path. A path-only token is a bearer capability: anyone who ends up
    # holding the URL can fetch another organisation's bill until it
    # expires, with no login at all. Signing the user id too means a
    # leaked URL is useless to anyone else who is signed in, and
    # `serve_bill_file` can refuse it.
    #
    # Unauthenticated minting (a background job building a payload with
    # no request) still produces a path-only token, so nothing that
    # already works stops working.
    user = getattr(request, "user", None) if request is not None else None
    user_id = (
        str(user.id)
        if user is not None and getattr(user, "is_authenticated", False)
        else ""
    )
    signer = TimestampSigner(salt=_BILL_FILE_SIGNER_SALT)
    token = signer.sign(f"{media_path}{_TOKEN_SEP}{user_id}")
    url = f"{settings.MEDIA_URL.rstrip('/')}/{media_path.lstrip('/')}?token={token}"
    if request is not None:
        return request.build_absolute_uri(url)
    return url


def _token_matches_path(token, requested_path, user=None):
    """Verify the token covers this path, and this user if it names one."""
    if not token:
        return False
    signer = TimestampSigner(salt=_BILL_FILE_SIGNER_SALT)
    try:
        signed_value = signer.unsign(token, max_age=_BILL_FILE_TOKEN_TTL)
    except SignatureExpired:
        logger.info("serve_bill_file token expired for path=%s", requested_path)
        return False
    except BadSignature:
        logger.info("serve_bill_file token invalid for path=%s", requested_path)
        return False
    signed_path, _, signed_user_id = signed_value.partition(_TOKEN_SEP)

    # Normalise both sides so ``bills/foo.pdf`` and ``/bills/foo.pdf``
    # compare equal. Prevents path-swap attacks where a valid token for
    # ``bills/A.pdf`` is reused with ``?token=…`` but the URL points at
    # ``bills/B.pdf`` — signed_path must match requested_path exactly.
    if os.path.normpath(signed_path).lstrip("/") != os.path.normpath(
        requested_path
    ).lstrip("/"):
        return False

    # A token minted for somebody is only good for that somebody. An
    # anonymous caller is still accepted — the browser fetching an
    # <img>/<iframe> may carry no session — but a *different* signed-in
    # user presenting a leaked URL is refused.
    if signed_user_id and user is not None and getattr(user, "is_authenticated", False):
        if str(getattr(user, "id", "")) != signed_user_id:
            logger.warning(
                "serve_bill_file token belongs to user %s but was presented by %s",
                signed_user_id, getattr(user, "id", None),
            )
            return False
    return True


@xframe_options_exempt
@csrf_exempt
def serve_bill_file(request, path):
    """
    Serve media files (PDF/images) only when the caller presents a valid
    signed URL token or a valid staff-user session.

    The signed-URL scheme is the intended path for browsers embedding the
    file in ``<img>``/``<iframe>``. Staff bypass is preserved so ops can
    fetch files directly for debugging.

    ``django_serve`` also validates the on-disk path and raises Http404
    on traversal attempts — belt-and-braces alongside the signature check.
    """
    user = getattr(request, "user", None)
    is_staff = bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))

    token = request.GET.get("token")
    if not is_staff and not _token_matches_path(token, path, user):
        return HttpResponseForbidden("You do not have access to this file.")

    logger.debug(
        "serve_bill_file OK user=%s path=%s (staff=%s)",
        getattr(user, "id", None), path, is_staff,
    )

    response = django_serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    # Was ``public, max-age=3600``. With per-request signatures, keep
    # ``private`` so shared proxies don't cache one user's token response.
    response["Cache-Control"] = "private, max-age=3600"
    # Stop the ?token= in this URL reaching any third party through a
    # Referer header when the document is embedded in a page.
    response["Referrer-Policy"] = "no-referrer"
    return response


# ---------------------------------------------------------------------------
# Public demo booking
# ---------------------------------------------------------------------------

@extend_schema(
    request=DemoRequestSerializer,
    responses={201: DemoRequestSerializer},
    tags=["Public"],
    methods=["POST"],
)
@api_view(["POST"])
@permission_classes([AllowAny])
def book_demo_view(request):
    """Record a demo request from the public /book-demo page.

    Rejects unverified captchas, non-business emails and repeat bookings.
    All three checks live in :class:`DemoRequestSerializer`; this view
    only adds the duplicate race guard and the notification email.

    ``context`` carries the request through so the captcha field can pass
    the caller's IP to Google.
    """
    serializer = DemoRequestSerializer(data=request.data, context={"request": request})

    if not serializer.is_valid():
        errors = serializer.errors
        email_errors = [str(e) for e in errors.get("email", [])]
        already_booked = DemoRequestSerializer.ALREADY_BOOKED_MESSAGE in email_errors

        # Correction 57: booked but never verified (e.g. the first email got
        # lost) — send the verification link again, rate-limited.
        if already_booked:
            _resend_demo_verify_if_pending(request.data.get("email"))

        # Surface one flat message for the toast, keep per-field errors
        # for inline display.
        first_error = next(
            (str(msgs[0]) for msgs in errors.values() if msgs),
            "Please check the form and try again.",
        )
        return Response(
            {
                "message": first_error,
                "code": "already_booked" if already_booked else "validation_error",
                "errors": errors,
            },
            status=status.HTTP_409_CONFLICT if already_booked else status.HTTP_400_BAD_REQUEST,
        )

    try:
        demo_request = serializer.save()
    except IntegrityError:
        # Two submissions for the same email raced past the serializer
        # check; the unique index is the real arbiter.
        return Response(
            {
                "message": DemoRequestSerializer.ALREADY_BOOKED_MESSAGE,
                "code": "already_booked",
                "errors": {"email": [DemoRequestSerializer.ALREADY_BOOKED_MESSAGE]},
            },
            status=status.HTTP_409_CONFLICT,
        )

    # Correction 57: ask the requester to confirm their work email.
    _send_demo_verify_email(demo_request)

    # Notify sales. A dead SMTP server must never fail the booking the
    # visitor already completed, so failures are logged and swallowed.
    try:
        send_simple_email(
            subject=f"New demo request — {demo_request.organization}",
            message=(
                f"Name: {demo_request.full_name}\n"
                f"Organization: {demo_request.organization}\n"
                f"Email: {demo_request.email}\n"
                f"Phone: {demo_request.phone}\n"
                f"Accounting software: {demo_request.get_accounting_software_display()}\n"
                "Email verified: No — a verification link was sent to the requester. "
                "You'll get another email once they verify.\n"
            ),
            to_email=getattr(settings, "SALES_NOTIFICATION_EMAIL", "support@billmunshi.com"),
        )
    except Exception as exc:
        logger.error("Demo-request notification failed for %s: %s", demo_request.email, exc)

    return Response(
        {
            "message": (
                "Thank you! We've sent a verification link to your email — please "
                "verify it. Our team will contact you shortly to schedule your demo."
            ),
            "email_verification_required": True,
            "demo_request": DemoRequestSerializer(demo_request).data,
        },
        status=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# Demo request email verification (Client Correction 57)
# ---------------------------------------------------------------------------

_DEMO_VERIFY_SALT = "apps.common.demo-request-verify"
_DEMO_VERIFY_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _resend_demo_verify_if_pending(email):
    from apps.common.utils import is_rate_limited

    email = (email or "").strip().lower()
    if not email:
        return
    demo_request = DemoRequest.objects.filter(email=email, email_verified=False).first()
    if demo_request and not is_rate_limited("demo-verify-resend", email, limit=3, window_seconds=3600):
        _send_demo_verify_email(demo_request)


def _send_demo_verify_email(demo_request):
    """Email the requester a signed link that confirms their address.
    Failures are logged, never raised — the booking is already saved."""
    token = signing.dumps({"demo": str(demo_request.pk)}, salt=_DEMO_VERIFY_SALT)
    base = getattr(settings, "FRONTEND_URL", "https://billmunshi.com").rstrip("/")
    verify_url = f"{base}/book-demo/verify?token={token}"
    try:
        send_templated_email(
            subject="Confirm your email for your Bill Munshi demo",
            template_base="demo_verify_email",
            to_email=demo_request.email,
            context={
                "display_name": demo_request.full_name,
                "organization": demo_request.organization,
                "verify_url": verify_url,
            },
        )
    except Exception as exc:
        logger.error("Demo verify-email send failed for %s: %s", demo_request.email, exc)


@extend_schema(tags=["Public"], methods=["POST"])
@api_view(["POST"])
@authentication_classes([OptionalJWTAuthentication])
@permission_classes([AllowAny])
def book_demo_verify_view(request):
    """Mark a demo request's email as verified from the emailed link."""
    from django.utils import timezone

    token = ((request.data or {}).get("token") or "").strip()
    invalid = Response(
        {"message": "This verification link is invalid or has expired."},
        status=status.HTTP_400_BAD_REQUEST,
    )
    if not token:
        return invalid
    try:
        payload = signing.loads(token, salt=_DEMO_VERIFY_SALT, max_age=_DEMO_VERIFY_MAX_AGE)
        demo_request = DemoRequest.objects.get(pk=payload.get("demo"))
    except (BadSignature, SignatureExpired, DemoRequest.DoesNotExist, ValueError, TypeError, AttributeError):
        return invalid
    except Exception:  # malformed UUID etc.
        return invalid

    if demo_request.email_verified:
        return Response({"message": "Your email is already verified. Our team will contact you shortly."})

    demo_request.email_verified = True
    demo_request.email_verified_at = timezone.now()
    demo_request.save(update_fields=["email_verified", "email_verified_at", "updated_at"])

    try:
        send_simple_email(
            subject=f"Demo request email verified — {demo_request.organization}",
            message=(
                f"{demo_request.full_name} <{demo_request.email}> verified their email.\n"
                f"Organization: {demo_request.organization}\n"
                f"Phone: {demo_request.phone}\n"
                f"Accounting software: {demo_request.get_accounting_software_display()}\n"
            ),
            to_email=getattr(settings, "SALES_NOTIFICATION_EMAIL", "support@billmunshi.com"),
        )
    except Exception as exc:
        logger.error("Demo-verified notification failed for %s: %s", demo_request.email, exc)

    return Response({"message": "Email verified! Our team will contact you shortly to schedule your demo."})


@extend_schema(
    summary="Maintenance status",
    description=(
        "Public. The frontend polls this so it can show a maintenance "
        "screen instead of a wall of failed requests."
    ),
    responses={200: None},
    tags=["Common"],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def maintenance_status_view(request):
    """Whether the application is in maintenance, and what to tell people.

    ``exempt_for_you`` is the important field. The middleware already
    lets staff through, so for them the API keeps answering — but the
    frontend gate was reading the global flag and showing them the
    maintenance screen anyway, which defeated the one thing the
    exemption exists for: walking through a release against production
    before letting clients back in.

    The server decides, not the browser. ``AllowAny`` on purpose — a
    signed-out visitor must still be told the site is down — but when a
    token is presented the answer is specific to that user.
    """
    from apps.common.maintenance import get_maintenance_state

    state = dict(get_maintenance_state())
    user = getattr(request, "user", None)
    state["exempt_for_you"] = bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )
    return Response(state)
