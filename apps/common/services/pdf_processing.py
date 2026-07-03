# apps/common/services/pdf_processing.py
"""
Shared PDF splitting service.

Provides a generic ``split_pdf_to_bills`` function that works with any bill
model.  The five copy-pasted ``process_pdf_splitting_*`` helpers in tally
and zoho views now delegate to this single implementation.
"""
import logging
from datetime import datetime
from io import BytesIO

from django.core.files.base import ContentFile
from pdf2image import convert_from_bytes
from PyPDF2 import PdfReader

logger = logging.getLogger(__name__)


def split_pdf_to_bills(
    pdf_file,
    organization,
    file_type,
    uploaded_by,
    bill_model,
    filename_prefix="BM-Page",
):
    """Split a multi-page PDF into per-page JPEG images and create one bill per page.

    Args:
        pdf_file: File-like object containing the PDF bytes.
        organization: Organization instance the bills belong to.
        file_type: Value for the ``file_type`` field on the bill model.
        uploaded_by: User instance that uploaded the file.
        bill_model: Django model class to create (e.g. ``TallyVendorBill``).
        filename_prefix: Prefix used for generated JPEG filenames.

    Returns:
        list of newly created bill model instances.
    """
    created_bills = []

    try:
        pdf_file.seek(0)
        pdf_bytes = pdf_file.read()
        pdf = PdfReader(BytesIO(pdf_bytes))
        unique_id = datetime.now().strftime("%Y%m%d%H%M%S")

        for page_num in range(len(pdf.pages)):
            page_images = convert_from_bytes(
                pdf_bytes,
                first_page=page_num + 1,
                last_page=page_num + 1,
            )

            if page_images:
                image_io = BytesIO()
                page_images[0].save(image_io, format="JPEG")
                image_io.seek(0)

                field_names = {f.name for f in bill_model._meta.get_fields()}
                type_field = "file_type" if "file_type" in field_names else "fileType"
                bill = bill_model.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"{filename_prefix}-{page_num + 1}-{unique_id}.jpg",
                    ),
                    organization=organization,
                    uploaded_by=uploaded_by,
                    **{type_field: file_type},
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting PDF: {str(e)}")
        raise Exception(f"PDF processing failed: {str(e)}")

    return created_bills


def enqueue_pdf_split_async(
    pdf_file,
    organization,
    file_type,
    uploaded_by,
    bill_model,
    split_task_fn,
    filename_prefix="BM-Container",
):
    """Save the PDF as a *placeholder* bill and enqueue the split for RQ.

    Instead of rendering all pages in the request thread (which can
    block for tens of seconds on a 50-page PDF — see #14 in the upload
    audit), we:

    1. Persist the original PDF as a single placeholder bill row with
       ``is_processing=True`` — the user sees "Splitting in progress".
    2. Enqueue ``split_task_fn(placeholder_bill_id)`` which fires the
       actual page-by-page render + per-page bill creation via
       ``split_pdf_to_bills``, then deletes the placeholder.

    Callers pass ``split_task_fn`` (module-specific, e.g.
    ``apps.module.tally.tasks.split_pdf_bill_task``) so the RQ worker
    can locate the right model + downstream analyzer.

    Returns a single-item list containing the placeholder bill so the
    upload endpoint response shape is unchanged from the sync path.
    """
    pdf_file.seek(0)
    pdf_bytes = pdf_file.read()
    original_name = getattr(pdf_file, "name", "container.pdf")

    # Model field for bill type varies (Tally uses ``file_type``, Zoho
    # uses ``fileType``). Pick whichever the model actually defines.
    field_names = {f.name for f in bill_model._meta.get_fields()}
    type_field = "file_type" if "file_type" in field_names else "fileType"

    placeholder = bill_model.objects.create(
        file=ContentFile(pdf_bytes, name=f"{filename_prefix}-{original_name}"),
        organization=organization,
        uploaded_by=uploaded_by,
        is_processing=True,
        **{type_field: file_type},
    )
    # Enqueue via ``transaction.on_commit`` at the caller — same guarantee
    # as regular bill analysis, so the split job only fires if the
    # surrounding upload transaction actually commits.
    try:
        from django.db import transaction
        transaction.on_commit(
            lambda: split_task_fn(str(placeholder.id))
        )
    except Exception as exc:
        logger.warning(
            "Failed to schedule PDF split for placeholder %s: %s",
            placeholder.id, exc,
        )
    return [placeholder]
