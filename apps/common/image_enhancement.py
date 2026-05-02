"""
Server-side bill image enhancement.

Mirrors what apps like CamScanner / Adobe Scan do, but runs entirely on the
backend so the operator's browser never has to load a WASM OpenCV runtime.

Public API:
    detect_corners(image_bytes)        -> [(x,y), ...4]    # tl, tr, br, bl
    enhance_image(image_bytes, corners=None, filter='bw') -> (jpeg_bytes, corners_used, (w,h))

Both functions are pure / stateless. The view layer is responsible for any
caching or storage decisions.
"""
from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Largest dimension we work with. Bill photos from phones are commonly
# 4000–6000 px; OpenCV contour finding on that is unnecessary and slow.
MAX_EDGE = 1600

# Filter modes — kept lowercase so frontend strings line up
FILTER_BW = "bw"
FILTER_GRAYSCALE = "grayscale"
FILTER_ORIGINAL = "original"
FILTER_CHOICES = (FILTER_BW, FILTER_GRAYSCALE, FILTER_ORIGINAL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bytes_to_bgr(image_bytes: bytes) -> np.ndarray:
    """Decode arbitrary image bytes (JPEG/PNG/HEIC-via-PIL) into a BGR ndarray.

    Honours EXIF orientation so portrait phone photos don't come out sideways.
    """
    pil = Image.open(io.BytesIO(image_bytes))
    pil = ImageOps.exif_transpose(pil)
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    rgb = np.array(pil)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return bgr


def _downscale(img: np.ndarray, max_edge: int = MAX_EDGE) -> Tuple[np.ndarray, float]:
    """Resize so the longest edge is `max_edge`. Returns (resized, scale_factor)."""
    h, w = img.shape[:2]
    if max(h, w) <= max_edge:
        return img, 1.0
    scale = max_edge / float(max(h, w))
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA), scale


def _order_quad(points: np.ndarray) -> np.ndarray:
    """Order 4 corner points as [top-left, top-right, bottom-right, bottom-left].

    Uses x+y for tl/br extremes and x-y for tr/bl extremes — robust to small
    perspective skew. `points` is shape (4, 2).
    """
    pts = points.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]   # top-left
    ordered[2] = pts[np.argmax(s)]   # bottom-right
    ordered[1] = pts[np.argmin(diff)]  # top-right
    ordered[3] = pts[np.argmax(diff)]  # bottom-left
    return ordered


def _find_paper_quad(img: np.ndarray) -> Optional[np.ndarray]:
    """Return 4 corner points (ordered tl,tr,br,bl) for the paper, or None.

    Pipeline: grayscale -> blur -> Canny -> findContours -> approxPolyDP,
    keep the largest 4-vertex contour above an area threshold.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blur, 50, 200)

    # Dilate so broken edges connect into a single contour
    edged = cv2.dilate(edged, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(
        edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None

    img_area = img.shape[0] * img.shape[1]
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]

    for c in contours:
        if cv2.contourArea(c) < img_area * 0.20:
            # Anything under 20% of the frame is unlikely to be the bill itself.
            break
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4:
            return _order_quad(approx)

    return None


def _warp_perspective(img: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Warp the quadrilateral defined by `corners` into a flat rectangle.

    Output dimensions are derived from the longest top/bottom and left/right
    edges, preserving roughly the original aspect ratio.
    """
    tl, tr, br, bl = corners
    width = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
    height = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
    out_w = max(int(round(width)), 1)
    out_h = max(int(round(height)), 1)

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    return cv2.warpPerspective(img, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR)


def _apply_filter(img: np.ndarray, mode: str) -> np.ndarray:
    """Apply OCR-friendly enhancement.

    - bw:        adaptive threshold + sharpened — best for OCR
    - grayscale: plain grayscale (3-channel for downstream consistency)
    - original:  contrast-stretched original colour
    """
    if mode == FILTER_GRAYSCALE:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    if mode == FILTER_BW:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Adaptive threshold handles uneven lighting and shadows much better
        # than a global Otsu threshold for phone-camera bills.
        thresh = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=31,
            C=12,
        )
        # Tiny morphological cleanup to get rid of speckle while keeping text
        kernel = np.ones((1, 1), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
        return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)

    # original — apply mild contrast lift so faded prints look better
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_chan, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_chan = clahe.apply(l_chan)
    return cv2.cvtColor(cv2.merge((l_chan, a, b)), cv2.COLOR_LAB2BGR)


def _encode_jpeg(img: np.ndarray, quality: int = 88) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("Failed to encode enhanced image")
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_corners(image_bytes: bytes) -> Tuple[List[Tuple[float, float]], Tuple[int, int]]:
    """Detect the document paper corners on the working-resolution image.

    Returns:
        (corners, (work_w, work_h))
        `corners` is a list of (x, y) in working-resolution pixels, ordered
        top-left, top-right, bottom-right, bottom-left. If detection fails
        we return a 5%-margin default rectangle so the operator still has
        something to drag.
    """
    bgr = _bytes_to_bgr(image_bytes)
    work, _ = _downscale(bgr)
    h, w = work.shape[:2]
    quad = _find_paper_quad(work)
    if quad is None:
        m = min(w, h) * 0.05
        quad = np.array(
            [[m, m], [w - m, m], [w - m, h - m], [m, h - m]], dtype=np.float32
        )
    return [(float(p[0]), float(p[1])) for p in quad], (w, h)


def enhance_image(
    image_bytes: bytes,
    corners: Optional[List[Tuple[float, float]]] = None,
    filter_mode: str = FILTER_BW,
) -> Tuple[bytes, List[Tuple[float, float]], Tuple[int, int]]:
    """Auto-detect (or use provided) corners, perspective-warp, apply filter.

    Args:
        image_bytes: raw image bytes (JPEG/PNG)
        corners:     optional list of 4 (x,y) tuples in the WORKING-RESOLUTION
                     coordinate system returned by `detect_corners`. If None,
                     we run auto-detection.
        filter_mode: one of FILTER_BW / FILTER_GRAYSCALE / FILTER_ORIGINAL

    Returns:
        (enhanced_jpeg_bytes, corners_used, (work_w, work_h))
    """
    if filter_mode not in FILTER_CHOICES:
        filter_mode = FILTER_BW

    bgr = _bytes_to_bgr(image_bytes)
    work, _ = _downscale(bgr)
    h, w = work.shape[:2]

    if corners is None:
        quad = _find_paper_quad(work)
        if quad is None:
            m = min(w, h) * 0.05
            quad = np.array(
                [[m, m], [w - m, m], [w - m, h - m], [m, h - m]],
                dtype=np.float32,
            )
    else:
        if len(corners) != 4:
            raise ValueError("corners must be a list of exactly 4 (x,y) points")
        quad = np.array([[float(p[0]), float(p[1])] for p in corners], dtype=np.float32)
        # Re-order in case caller passed them in a non-canonical order
        quad = _order_quad(quad)

    warped = _warp_perspective(work, quad)
    filtered = _apply_filter(warped, filter_mode)
    encoded = _encode_jpeg(filtered)

    return (
        encoded,
        [(float(p[0]), float(p[1])) for p in quad],
        (w, h),
    )
