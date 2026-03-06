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

                bill = bill_model.objects.create(
                    file=ContentFile(
                        image_io.read(),
                        name=f"{filename_prefix}-{page_num + 1}-{unique_id}.jpg",
                    ),
                    file_type=file_type,
                    organization=organization,
                    uploaded_by=uploaded_by,
                )
                created_bills.append(bill)

    except Exception as e:
        logger.error(f"Error splitting PDF: {str(e)}")
        raise Exception(f"PDF processing failed: {str(e)}")

    return created_bills
