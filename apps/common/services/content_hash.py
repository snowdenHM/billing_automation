"""
SHA-256 content-hash duplicate detection (see #8 in the upload audit).

Two identical files uploaded to the same org should be detected as
duplicates *before* any AI analysis fires — otherwise the org is billed
twice for OCR and the user is confused by two rows for the same bill.

``compute_file_hash`` reads the incoming ``UploadedFile`` without
consuming it (seeks back to zero afterwards) so the caller can still
persist the file to storage.

``find_hash_duplicate`` returns the earliest existing bill in the same
org that shares the hash, or ``None``.
"""
import hashlib
import logging

logger = logging.getLogger(__name__)

_READ_CHUNK = 64 * 1024  # 64 KiB


def compute_file_hash(uploaded_file):
    """Return the SHA-256 hex digest of ``uploaded_file`` (streamed)."""
    hasher = hashlib.sha256()
    try:
        uploaded_file.seek(0)
    except Exception:
        pass
    for chunk in iter(lambda: uploaded_file.read(_READ_CHUNK), b""):
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        hasher.update(chunk)
    try:
        uploaded_file.seek(0)
    except Exception:
        pass
    return hasher.hexdigest()


def find_hash_duplicate(bill_model, organization, content_hash, exclude_id=None):
    """Return the earliest bill in ``organization`` with the same hash, or ``None``."""
    if not content_hash:
        return None
    qs = bill_model.objects.filter(
        organization=organization,
        content_hash=content_hash,
    ).order_by("created_at")
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.first()
