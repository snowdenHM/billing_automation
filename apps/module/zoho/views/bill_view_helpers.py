# apps/module/zoho/views/bill_view_helpers.py
"""
Shared helper functions for Zoho expense and journal bill views.
Eliminates duplication by parameterizing model / serializer / label.
"""

import logging
import os

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from apps.common.bill_filters import apply_bill_search
from apps.common.pagination import DefaultPagination
from apps.common.utils import get_organization_from_request

logger = logging.getLogger(__name__)


# ============================================================================
# Bills List
# ============================================================================

def zoho_bills_list_base(request, org_id, *, bill_model, list_serializer):
    """
    Generic list view for Zoho bills.

    Args:
        request: DRF request
        org_id: Organization UUID
        bill_model: ExpenseBill or JournalBill
        list_serializer: ZohoExpenseBillSerializer or ZohoJournalBillSerializer
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    bills = bill_model.objects.filter(organization=organization)

    status_param = request.query_params.get('status', '').lower()
    if status_param == 'draft':
        bills = bills.filter(status='Draft')
    elif status_param == 'analysed':
        bills = bills.filter(status__in=['Analysed', 'Verified'])
    elif status_param == 'synced':
        bills = bills.filter(status='Synced')

    # Free-text search runs in the database so it covers every matching
    # bill, not just the page already downloaded. Zoho names the document
    # field in camelCase.
    bills = apply_bill_search(
        bills, request.query_params.get('search'), name_field='billmunshiName'
    )

    bills = bills.select_related('uploaded_by').order_by('-created_at')

    paginator = DefaultPagination()
    paginated_bills = paginator.paginate_queryset(bills, request)

    if paginated_bills is not None:
        serializer = list_serializer(paginated_bills, many=True)
        return paginator.get_paginated_response(serializer.data)

    serializer = list_serializer(bills, many=True)
    return Response({"results": serializer.data})


# ============================================================================
# Bills Upload
# ============================================================================

def zoho_bills_upload_base(
    request, org_id, *,
    bill_model, list_serializer, upload_serializer,
    label,
    split_pdf_fn,
    # Legacy sync-mode-only args — pass ``None`` (default) when using the
    # async ``enqueue_analysis_fn`` path (recommended for new callers).
    check_duplicate_fn=None,
    analyze_fn=None,
    create_objects_fn=None,
    enqueue_analysis_fn=None,
    pdf_split_task_fn=None,
):
    """
    Generic upload view for Zoho bills (handles single + multi upload, PDF split,
    auto-analysis, and duplicate detection).

    Args:
        request: DRF request
        org_id: Organization UUID
        bill_model: ExpenseBill or JournalBill
        list_serializer: Serializer for response (many=True)
        upload_serializer: Upload validation serializer
        label: 'Expense' or 'journal' — used in log messages
        check_duplicate_fn: callable(bill, org) -> (is_dup, dups, max_sim)
        split_pdf_fn: callable(pdf_file, org, file_type, uploaded_by) -> [bills]
        analyze_fn: callable(file_content, file_extension) -> analyzed_data
        create_objects_fn: callable(bill, analyzed_data, org) -> zoho_bill
        enqueue_analysis_fn: Optional callable(bill_id, org_id) -> job (for background processing)
    """
    tag = label.upper()

    files_data = []
    logger.debug(f"[{tag} DEBUG] Request data keys: {list(request.data.keys())}")

    if 'files' in request.data:
        files_data = (
            request.data.getlist('files')
            if hasattr(request.data, 'getlist')
            else request.data.get('files', [])
        )
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
        logger.debug(f"[{tag} DEBUG] Found 'files' field with {len(files_data)} file(s)")
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]
        logger.debug(f"[{tag} DEBUG] Found 'file' field with {len(files_data)} file(s)")

    logger.debug(f"[{tag} DEBUG] Total files collected: {len(files_data)}")

    for i, f in enumerate(files_data):
        logger.debug(
            f"[{tag} DEBUG] File {i + 1}: "
            f"{getattr(f, 'name', 'Unknown')} - Size: {getattr(f, 'size', 'Unknown')}"
        )

    serializer_data = {
        'files': files_data,
        # Accept both ``fileType`` (canonical for Zoho) and ``file_type``
        # (backend-canonical / Tally naming). Same fix pattern as #5.
        'fileType': (
            request.data.get('fileType')
            or request.data.get('file_type')
            or 'Single Invoice/File'
        ),
    }

    serializer = upload_serializer(data=serializer_data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({'error': 'Organization not found'}, status=status.HTTP_400_BAD_REQUEST)

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['fileType']
    created_bills = []

    if not files:
        return Response({'error': 'No files provided for upload'}, status=status.HTTP_400_BAD_REQUEST)

    # Track file paths so we can delete orphans if the atomic block
    # rolls back — Django's FileField writes to disk before commit.
    uploaded_file_paths = []
    async_pdf_bill_ids = set()

    try:
        with transaction.atomic():
            upload_warnings = []
            rejected_files = []

            from apps.common.services.content_hash import (
                compute_file_hash, find_hash_duplicate,
            )
            from apps.common.services.document_classification import (
                classify_document, describe_rejection,
            )

            for i, uploaded_file in enumerate(files):
                logger.debug(
                    f"[{tag} DEBUG] Processing file {i + 1}/{len(files)}: "
                    f"{uploaded_file.name}"
                )
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Exact-duplicate short-circuit via SHA-256 content hash
                # (see #8 in the upload audit).
                content_hash = compute_file_hash(uploaded_file)
                existing_hash_dup = find_hash_duplicate(
                    bill_model, organization, content_hash,
                )
                if existing_hash_dup:
                    upload_warnings.append({
                        'uploaded_file': uploaded_file.name,
                        'warning_type': 'exact_duplicate',
                        'existing_bills': [{
                            'bill_name': existing_hash_dup.billmunshiName,
                            'bill_id': str(existing_hash_dup.id),
                            'match_type': 'sha256_content_hash',
                            'reason': 'Byte-for-byte identical file already uploaded',
                        }],
                        'warning': (
                            f'"{uploaded_file.name}" is byte-for-byte identical to '
                            f'"{existing_hash_dup.billmunshiName}" — skipped.'
                        ),
                    })
                    logger.info(
                        "Exact-duplicate detected for %s (hash=%s) — skipping",
                        uploaded_file.name, content_hash[:12],
                    )
                    continue

                # Screen the document BEFORE creating the Draft row, so a
                # non-bill never becomes a bill the user has to clean up.
                # Runs after the hash check so re-uploading a known file
                # costs nothing.
                uploaded_file.seek(0)
                verdict = classify_document(uploaded_file.read(), file_extension)
                uploaded_file.seek(0)

                if not verdict['is_bill']:
                    rejected_files.append(
                        describe_rejection(uploaded_file.name, verdict)
                    )
                    logger.info(
                        "[%s] Rejected %s — detected as %s (confidence %.2f)",
                        tag, uploaded_file.name,
                        verdict['document_type'], verdict['confidence'],
                    )
                    continue

                # Check for potential file-level duplicates (same name /
                # similar size). Advisory only; AI-based dedup runs later.
                similar_files = bill_model.objects.filter(
                    organization=organization, file__isnull=False
                ).exclude(status='Draft')

                potential_duplicate_files = []
                for existing_bill in similar_files:
                    if not (existing_bill.file and existing_bill.file.name):
                        continue
                    existing_filename = os.path.basename(existing_bill.file.name)
                    uploaded_filename = uploaded_file.name

                    if existing_filename.lower() == uploaded_filename.lower():
                        potential_duplicate_files.append({
                            'bill': existing_bill,
                            'match_type': 'exact_filename',
                            'reason': 'Same filename detected',
                        })
                    elif (
                        existing_filename.lower().replace('.pdf', '').replace('.jpg', '').replace('.png', '')
                        == uploaded_filename.lower().replace('.pdf', '').replace('.jpg', '').replace('.png', '')
                    ):
                        potential_duplicate_files.append({
                            'bill': existing_bill,
                            'match_type': 'similar_filename',
                            'reason': 'Similar filename detected',
                        })
                    elif (
                        existing_bill.file
                        and hasattr(existing_bill.file.storage, 'exists')
                        and existing_bill.file.storage.exists(existing_bill.file.name)
                        and hasattr(existing_bill.file, 'size')
                        and hasattr(uploaded_file, 'size')
                    ):
                        try:
                            existing_size = existing_bill.file.size
                            uploaded_size = uploaded_file.size
                            if (
                                existing_size > 0
                                and uploaded_size > 0
                                and abs(existing_size - uploaded_size) / max(existing_size, uploaded_size) < 0.05
                            ):
                                potential_duplicate_files.append({
                                    'bill': existing_bill,
                                    'match_type': 'similar_size',
                                    'reason': 'Similar file size detected',
                                })
                        except (FileNotFoundError, OSError) as e:
                            logger.error(
                                f"[{tag} DEBUG] Error accessing file for bill "
                                f"{existing_bill.billmunshiName}: {e}"
                            )
                            continue

                if potential_duplicate_files:
                    upload_warnings.append({
                        'uploaded_file': uploaded_file.name,
                        'potential_duplicates': len(potential_duplicate_files),
                        'warning': (
                            f'File "{uploaded_file.name}" may be a duplicate of existing bills'
                        ),
                        'existing_bills': [
                            {
                                'bill_name': dup['bill'].billmunshiName,
                                'bill_id': str(dup['bill'].id),
                                'match_type': dup['match_type'],
                                'reason': dup['reason'],
                            }
                            for dup in potential_duplicate_files[:3]
                        ],
                    })

                # Handle PDF splitting for multiple invoice files
                if file_type == 'Multiple Invoice/File' and file_extension == 'pdf':
                    if pdf_split_task_fn is not None:
                        from apps.common.services.pdf_processing import (
                            enqueue_pdf_split_async,
                        )
                        pdf_bills = enqueue_pdf_split_async(
                            uploaded_file,
                            organization,
                            file_type,
                            request.user,
                            bill_model,
                            pdf_split_task_fn,
                        )
                    else:
                        pdf_bills = split_pdf_fn(
                            uploaded_file, organization, file_type, request.user,
                        )
                    for b in pdf_bills:
                        if b.file:
                            uploaded_file_paths.append(b.file.name)
                        if pdf_split_task_fn is not None:
                            async_pdf_bill_ids.add(str(b.id))
                        b.content_hash = content_hash
                        b.save(update_fields=["content_hash"])
                    created_bills.extend(pdf_bills)
                else:
                    bill = bill_model.objects.create(
                        file=uploaded_file,
                        fileType=file_type,
                        status='Draft',
                        organization=organization,
                        uploaded_by=request.user,
                        content_hash=content_hash,
                    )
                    if bill.file:
                        uploaded_file_paths.append(bill.file.name)
                    created_bills.append(bill)

            # Defer enqueue to ``transaction.on_commit`` so jobs only fire
            # after the DB row is truly durable. Prevents ghost jobs on
            # rollback (#9 in the upload audit).
            if enqueue_analysis_fn:
                bill_ids_to_enqueue = [
                    str(b.id) for b in created_bills
                    if str(b.id) not in async_pdf_bill_ids
                ]
                org_id_str = str(organization.id)

                def _enqueue_after_commit():
                    for bid in bill_ids_to_enqueue:
                        try:
                            job = enqueue_analysis_fn(bid, org_id_str)
                            bill_model.objects.filter(id=bid).update(
                                job_id=job.id, is_processing=True,
                            )
                            logger.info(
                                "Queued %s bill %s (job=%s) after commit",
                                label, bid, job.id,
                            )
                        except Exception as enqueue_err:
                            logger.error(
                                "Failed to enqueue %s bill %s: %s",
                                label, bid, enqueue_err,
                            )

                transaction.on_commit(_enqueue_after_commit)

        # -------- outside the atomic block --------
        # Response building for the async path. Actual enqueue already
        # scheduled via ``transaction.on_commit`` above so job IDs are
        # not yet populated at response time; the frontend polls for them.
        if enqueue_analysis_fn:
            # Nothing survived screening — tell the user plainly rather than
            # returning a "success" with zero bills.
            if rejected_files and not created_bills:
                return Response({
                    'error': 'No Bills Uploaded',
                    'detail': (
                        'None of the uploaded files look like bills or invoices.'
                    ),
                    'rejected_files': rejected_files,
                    'files_rejected': len(rejected_files),
                }, status=status.HTTP_400_BAD_REQUEST)

            processing_jobs = [
                {
                    'bill_id': str(bill.id),
                    'bill_name': bill.billmunshiName,
                    'job_id': None,
                    'status': 'queued_for_processing',
                }
                for bill in created_bills
            ]

            response_serializer = list_serializer(
                created_bills, many=True, context={'request': request},
            )
            logger.info(
                f"Successfully processed {len(files)} files and created "
                f"{len(created_bills)} {label} bills "
                f"({len(rejected_files)} rejected as non-bills)"
            )

            accepted = len(files) - len(rejected_files)
            response_data = {
                'message': (
                    f'Successfully uploaded {accepted} file(s) and created '
                    f'{len(created_bills)} {label} bill(s). '
                    'Processing started in background.'
                ),
                'files_uploaded': accepted,
                'bills_created': len(created_bills),
                'bills': response_serializer.data,
                'processing_jobs': processing_jobs,
                'note': (
                    'Bills are being processed in the background. '
                    'The page will automatically refresh to show results.'
                ),
            }

            if rejected_files:
                response_data['rejected_files'] = rejected_files
                response_data['files_rejected'] = len(rejected_files)
                response_data['rejection_message'] = (
                    f"{len(rejected_files)} file(s) were not uploaded because "
                    "they do not look like bills."
                )

            if upload_warnings:
                response_data['upload_warnings'] = upload_warnings
                response_data['warning_message'] = (
                    f"📁 FILE WARNING: {len(upload_warnings)} file(s) "
                    "may be duplicates based on filename/size"
                )

            return Response(response_data, status=status.HTTP_201_CREATED)

        # Synchronous processing mode (legacy)
        # Auto-analyze uploaded bills and check for duplicates
        analysis_results = []
        all_duplicate_warnings = []

        for bill in created_bills:
            if bill.status == 'Draft':
                try:
                    bill.file.seek(0)
                    file_content = bill.file.read()
                    file_ext = bill.file.name.split('.')[-1].lower()

                    analyzed_data = analyze_fn(file_content, file_ext)

                    bill.analysed_data = analyzed_data
                    bill.status = 'Analysed'
                    bill.process = True
                    bill.save()

                    create_objects_fn(bill, analyzed_data, organization)

                    is_duplicate, duplicate_bills, max_similarity = check_duplicate_fn(bill, organization)

                    analysis_result = {
                        'bill_id': str(bill.id),
                        'bill_name': bill.billmunshiName,
                        'analysis_successful': True,
                        'duplicate_detected': is_duplicate,
                    }

                    if is_duplicate:
                        dup_warnings = _build_duplicate_warnings(duplicate_bills, max_similarity, bill, label)
                        analysis_result.update(dup_warnings['result_fields'])
                        all_duplicate_warnings.extend(dup_warnings['warnings'])

                    analysis_results.append(analysis_result)

                except Exception as analysis_error:
                    logger.error(f"Auto-analysis failed for {label} bill {bill.billmunshiName}: {analysis_error}")
                    analysis_results.append({
                        'bill_id': str(bill.id),
                        'bill_name': bill.billmunshiName,
                        'analysis_successful': False,
                        'error': str(analysis_error),
                        'duplicate_detected': False,
                    })

        response_serializer = list_serializer(created_bills, many=True, context={'request': request})

        logger.info(f"Successfully processed {len(files)} files and created {len(created_bills)} {label} bills")

        response_data = {
            'message': f'Successfully uploaded {len(files)} file(s) and created {len(created_bills)} {label} bill(s)',
            'files_uploaded': len(files),
            'bills_created': len(created_bills),
            'bills': response_serializer.data,
            'auto_analysis_results': analysis_results,
        }

        _attach_upload_warnings(response_data, upload_warnings, all_duplicate_warnings, label)
        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading {label} bills: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Atomic block rolled back — clean up orphan files on disk (#10).
        for path in uploaded_file_paths:
            try:
                if default_storage.exists(path):
                    default_storage.delete(path)
                    logger.info(
                        "Cleaned up orphan Zoho upload after rollback: %s", path,
                    )
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to delete orphan Zoho upload %s: %s",
                    path, cleanup_err,
                )
        return Response({'detail': f'Error processing files: {e}'}, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Bill Detail
# ============================================================================

def zoho_bill_detail_base(
    request, org_id, bill_id, *,
    bill_model, zoho_bill_model, detail_serializer,
    label,
    prefetch_related=None,
    select_related=None,
):
    """
    Generic detail view for Zoho bills.

    Args:
        bill_model: ExpenseBill / JournalBill
        zoho_bill_model: ExpenseZohoBill / JournalZohoBill
        detail_serializer: ZohoExpenseBillDetailSerializer / ZohoJournalBillDetailSerializer
        label: 'Expense' / 'journal'
        prefetch_related: list of prefetch strings for zoho_bill query
        select_related: list of select_related strings for zoho_bill query
    """
    logger.info(f"[DEBUG] {label}_bill_detail_view - Starting for org_id: {org_id}, bill_id: {bill_id}")

    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)

        # Neighbouring bills in the verification queue, ordered the same way
        # the list page shows them. Positional so that Back is the exact
        # inverse of Next.
        from apps.common.services.bill_navigation import get_adjacent_bill_ids

        bill.previous_bill, bill.next_bill = get_adjacent_bill_ids(bill)

        # Get related ZohoBill
        try:
            qs = zoho_bill_model.objects.all()
            if select_related:
                qs = qs.select_related(*select_related)
            if prefetch_related:
                qs = qs.prefetch_related(*prefetch_related)
            zoho_bill = qs.get(selectBill=bill, organization=organization)
            bill.zoho_bill = zoho_bill
        except zoho_bill_model.DoesNotExist:
            bill.zoho_bill = None

        serializer = detail_serializer(bill, context={'request': request, 'organization': organization})
        return Response(serializer.data)

    except bill_model.DoesNotExist:
        return Response({"detail": f"{label} bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"[DEBUG] {label}_bill_detail_view - Unexpected error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return Response({"detail": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Bill Analyze
# ============================================================================

def zoho_bill_analyze_base(
    request, org_id, bill_id, *,
    bill_model, label,
    check_duplicate_fn, analyze_fn, create_objects_fn,
):
    """
    Generic analyze view for Zoho bills (Draft → Analysed).
    """
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)

        if bill.status != 'Draft':
            return Response(
                {"detail": "Bill must be in 'Draft' status to analyze"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            bill.file.seek(0)
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
        except Exception as e:
            logger.error(f"Error reading bill file: {e}")
            return Response({"detail": "Error reading the bill file"}, status=status.HTTP_400_BAD_REQUEST)

        analyzed_data = analyze_fn(file_content, file_extension)

        bill.analysed_data = analyzed_data
        bill.status = 'Analysed'
        bill.process = True
        bill.save()

        create_objects_fn(bill, analyzed_data, organization)

        is_duplicate, duplicate_bills, max_similarity = check_duplicate_fn(bill, organization)

        response_data = {
            "detail": f"{label} bill analyzed successfully",
            "analyzed_data": analyzed_data,
        }

        if is_duplicate:
            dup_warnings = _build_duplicate_warnings(duplicate_bills, max_similarity, bill, label)
            response_data.update(dup_warnings['result_fields'])
            response_data["duplicate_warning"] = True

        return Response(response_data)

    except bill_model.DoesNotExist:
        return Response({"detail": f"{label} bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return Response({"detail": f"Analysis failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Bill Delete
# ============================================================================

def zoho_bill_delete_base(request, org_id, bill_id, *, bill_model, label):
    """Generic delete view for Zoho bills."""
    organization = get_organization_from_request(request, org_id=org_id)
    if not organization:
        return Response({"detail": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)

        if bill.file:
            try:
                file_path = os.path.join(settings.MEDIA_ROOT, str(bill.file))
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                logger.warning(f"Could not delete file {bill.file}: {e}")

        bill.delete()
        return Response({"detail": f"{label} bill and associated file deleted successfully"})

    except bill_model.DoesNotExist:
        return Response({"detail": f"{label} bill not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error deleting {label} bill: {e}")
        return Response(
            {"detail": f"Failed to delete {label} bill: {e}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ============================================================================
# Internal Helpers
# ============================================================================

def _build_duplicate_warnings(duplicate_bills, max_similarity, bill, label):
    """Build duplicate-warning payload for upload/analyze responses."""
    warnings = []
    for dup in duplicate_bills:
        warnings.append({
            "duplicate_bill_id": str(dup['bill'].id),
            "duplicate_bill_name": dup['bill'].billmunshiName,
            "similarity_score": round(dup['similarity_score'], 2),
            "match_reasons": dup['match_reasons'],
            "invoice_number": dup['invoice_number'],
            "vendor_name": dup['vendor_name'],
            "total": dup['total'],
            "date": dup['date'],
            "status": dup['bill'].status,
        })

    result_fields = {
        "duplicate_count": len(duplicate_bills),
        "max_similarity": round(max_similarity, 2),
        "duplicate_bills": warnings,
        "warning_message": (
            f"⚠️ DUPLICATE DETECTED: {label.capitalize()} Bill '{bill.billmunshiName}' appears to be "
            f"{round(max_similarity, 1)}% similar to {len(duplicate_bills)} existing bill(s)."
        ),
    }

    return {'warnings': warnings, 'result_fields': result_fields}


def _attach_upload_warnings(response_data, upload_warnings, all_duplicate_warnings, label):
    """Attach upload + duplicate warnings to the upload response."""
    if not upload_warnings and not all_duplicate_warnings:
        return

    warnings_count = len(upload_warnings) + len(all_duplicate_warnings)
    warning_messages = []

    if upload_warnings:
        warning_messages.append(
            f"📁 FILE WARNING: {len(upload_warnings)} file(s) may be duplicates based on filename/size"
        )
        response_data['upload_warnings'] = upload_warnings

    if all_duplicate_warnings:
        warning_messages.append(
            f"🔍 CONTENT WARNING: {len(all_duplicate_warnings)} duplicate(s) detected after "
            f"analyzing {label} bill content"
        )
        response_data['duplicate_warnings'] = all_duplicate_warnings

    response_data.update({
        'total_warnings': warnings_count,
        'warning_message': " | ".join(warning_messages) + " | Please review carefully before proceeding.",
    })

    logger.warning(
        f"{label} bills - Total warnings: {warnings_count} "
        f"(Upload: {len(upload_warnings)}, Content: {len(all_duplicate_warnings)})"
    )
