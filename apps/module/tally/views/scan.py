"""
Bill image scan + enhance endpoints.

The frontend uses these to give the operator a CamScanner-style flow before
the actual bill upload: pick image → server detects edges + returns enhanced
preview → operator can drag corners / change filter → re-process → final
upload uses the enhanced image as the bill file.

All processing happens server-side (OpenCV, NumPy, Pillow) so the operator's
browser never has to load a WASM runtime.
"""
import base64
import json
import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response

from apps.common.permissions import IsOrgAdmin
from apps.organizations.models import Organization
from apps.common import image_enhancement

from .helpers import OrganizationAPIKeyOrBearerToken

logger = logging.getLogger(__name__)


MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12 MB safety cap on a single image


def _parse_corners(raw):
    """Accept corners either as a JSON string ('[[x,y],...]') or already-parsed list."""
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    out = []
    for p in raw:
        if isinstance(p, dict):
            out.append((float(p.get("x", 0)), float(p.get("y", 0))))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append((float(p[0]), float(p[1])))
        else:
            return None
    return out


@extend_schema(tags=["Bill Scanner"])
@api_view(["POST"])
@permission_classes([OrganizationAPIKeyOrBearerToken])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def scan_process(request, org_id):
    """
    POST /tally/org/<org_id>/scan/process/

    Two ways to call:

    A) First pass — multipart with `image` file, optional `filter`, no corners
       Server auto-detects paper edges and returns the enhanced preview.

    B) Re-enhance — multipart with `image` file + `corners` JSON + optional
       `filter`. Server uses the supplied corners (after re-ordering) to warp.

    Response (JSON):
        {
            "success": true,
            "enhanced_b64": "data:image/jpeg;base64,...",
            "corners": [{"x":..,"y":..}, ...4],
            "image_size": {"width": int, "height": int}
        }
    """
    organization = get_object_or_404(Organization, id=org_id)
    image_file = request.FILES.get("image")
    if not image_file:
        return Response(
            {"success": False, "message": "Field `image` is required (multipart)"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if image_file.size > MAX_UPLOAD_BYTES:
        return Response(
            {"success": False, "message": "Image is too large (max 12 MB)"},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    content_type = (image_file.content_type or "").lower()
    if not content_type.startswith("image/"):
        return Response(
            {"success": False, "message": "Only image uploads are supported"},
            status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    filter_mode = (request.data.get("filter") or "bw").lower()
    corners = _parse_corners(request.data.get("corners"))

    try:
        raw_bytes = image_file.read()
        enhanced, used_corners, (w, h) = image_enhancement.enhance_image(
            raw_bytes,
            corners=corners,
            filter_mode=filter_mode,
        )
    except ValueError as e:
        return Response(
            {"success": False, "message": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.exception("scan_process failed for org %s: %s", organization.id, e)
        return Response(
            {
                "success": False,
                "message": "Could not process the image. Please try a different one.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    b64 = base64.b64encode(enhanced).decode("ascii")
    return Response(
        {
            "success": True,
            "enhanced_b64": f"data:image/jpeg;base64,{b64}",
            "corners": [{"x": x, "y": y} for x, y in used_corners],
            "image_size": {"width": w, "height": h},
        },
        status=status.HTTP_200_OK,
    )
