"""
Common views for the application.
"""
import logging
import os

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import Http404, HttpResponseForbidden
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve as django_serve

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
_BILL_FILE_TOKEN_TTL = 3600  # seconds — 1 hour, plenty for a modal open


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
    signer = TimestampSigner(salt=_BILL_FILE_SIGNER_SALT)
    token = signer.sign(media_path)
    url = f"{settings.MEDIA_URL.rstrip('/')}/{media_path.lstrip('/')}?token={token}"
    if request is not None:
        return request.build_absolute_uri(url)
    return url


def _token_matches_path(token, requested_path):
    """Verify the signed token corresponds to the requested media path."""
    if not token:
        return False
    signer = TimestampSigner(salt=_BILL_FILE_SIGNER_SALT)
    try:
        signed_path = signer.unsign(token, max_age=_BILL_FILE_TOKEN_TTL)
    except SignatureExpired:
        logger.info("serve_bill_file token expired for path=%s", requested_path)
        return False
    except BadSignature:
        logger.info("serve_bill_file token invalid for path=%s", requested_path)
        return False
    # Normalise both sides so ``bills/foo.pdf`` and ``/bills/foo.pdf``
    # compare equal. Prevents path-swap attacks where a valid token for
    # ``bills/A.pdf`` is reused with ``?token=…`` but the URL points at
    # ``bills/B.pdf`` — signed_path must match requested_path exactly.
    return os.path.normpath(signed_path).lstrip("/") == os.path.normpath(
        requested_path
    ).lstrip("/")


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
    if not is_staff and not _token_matches_path(token, path):
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
    return response
