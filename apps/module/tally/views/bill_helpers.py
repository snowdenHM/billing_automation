# apps/module/tally/views/bill_helpers.py
"""
Shared helper functions for Tally vendor and expense bill views.
Eliminates duplication by parameterizing model/serializer.
"""
import logging
import os

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from apps.common.pagination import DefaultPagination
from apps.common.utils import get_organization_from_request

logger = logging.getLogger(__name__)


# ============================================================================
# Organization Error Response
# ============================================================================

def org_not_found_response(org_id):
    """Standard 404 response for organization not found / access denied."""
    return Response(
        {
            'error': 'Organization Access Denied',
            'message': f'Organization with ID {org_id} not found or you do not have access to it. '
                       'Please check the organization ID and your permissions.',
            'error_code': 'ORG_NOT_FOUND'
        },
        status=status.HTTP_404_NOT_FOUND
    )


# ============================================================================
# Bills List
# ============================================================================

def bills_list_base(request, org_id, bill_model, serializer_class, include_ownership_filter=True):
    """
    Generic list view for bills.
    
    Args:
        request: DRF request
        org_id: Organization UUID
        bill_model: Model class (TallyVendorBill or TallyExpenseBill)
        serializer_class: Serializer to use for response
        include_ownership_filter: Whether to include ownership filtering (vendor has it, expense doesn't)
    
    Returns:
        Response with paginated bill list
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return org_not_found_response(org_id)

    bills = bill_model.objects.filter(organization=organization)

    # Filter by status based on query parameters
    status_param = request.query_params.get('status', '').lower()
    if status_param == 'draft':
        bills = bills.filter(status='Draft')
    elif status_param == 'analysed':
        # Include both Analysed and Verified status bills
        bills = bills.filter(status__in=['Analysed', 'Verified'])
    elif status_param == 'synced':
        bills = bills.filter(status='Synced')

    # Filter by ownership (vendor bills have this extra filter)
    if include_ownership_filter:
        ownership_param = request.query_params.get('ownership', '').lower()
        if ownership_param == 'mine':
            bills = bills.filter(bill_belong_your_org=True)
        elif ownership_param == 'others':
            bills = bills.filter(bill_belong_your_org=False)

    bills = bills.order_by('-created_at')

    # Pagination
    paginator = DefaultPagination()
    page = paginator.paginate_queryset(bills, request)
    if page is not None:
        serializer = serializer_class(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    serializer = serializer_class(bills, many=True, context={'request': request})
    return Response(serializer.data)


# ============================================================================
# Bill Delete
# ============================================================================

def bill_delete_base(request, org_id, bill_id, bill_model):
    """
    Generic delete view for bills.
    
    Args:
        request: DRF request
        org_id: Organization UUID
        bill_id: Bill UUID to delete
        bill_model: Model class (TallyVendorBill or TallyExpenseBill)
    
    Returns:
        204 No Content on success, 404 if not found
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return org_not_found_response(org_id)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)
    except bill_model.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Delete the file from storage if it exists
    if bill.file:
        file_path = os.path.join(settings.MEDIA_ROOT, str(bill.file))
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"Failed to delete file {file_path}: {e}")

    # Delete the bill record from the database
    bill.delete()

    return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================================
# Bill Detail
# ============================================================================

def bill_detail_base(request, org_id, bill_id, bill_model, serializer_class):
    """
    Generic detail view for bills.
    
    Args:
        request: DRF request
        org_id: Organization UUID
        bill_id: Bill UUID
        bill_model: Model class
        serializer_class: Detail serializer to use
    
    Returns:
        Response with bill details
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return org_not_found_response(org_id)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)
    except bill_model.DoesNotExist:
        return Response(
            {'error': 'Bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = serializer_class(bill, context={'request': request})
    return Response(serializer.data)


# ============================================================================
# Processing Status
# ============================================================================

def bill_processing_status_base(request, org_id, bill_id, bill_model, bill_type_name=""):
    """
    Get processing status for a bill with job status info.
    
    Args:
        request: DRF request
        org_id: Organization UUID  
        bill_id: Bill UUID
        bill_model: Bill model class
        bill_type_name: Name for error messages (e.g., "Vendor", "Expense")
    
    Returns:
        Response with processing status, duplicate info, and job status
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return Response({
            'error': 'Organization not found',
            'message': f'Organization with ID {org_id} not found or access denied.'
        }, status=status.HTTP_404_NOT_FOUND)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)
        
        status_data = {
            'bill_id': str(bill.id),
            'bill_name': bill.bill_munshi_name,
            'is_processing': bill.is_processing,
            'processing_error': bill.processing_error,
            'status': bill.status,
            'is_duplicate': bill.is_duplicate,
            'duplicate_description': bill.duplicate_description,
            'duplicate_score': bill.duplicate_score,
            'duplicate_matched_bills': bill.duplicate_matched_bills
        }
        
        # Get job status if job_id exists
        if bill.job_id:
            from ..tasks import get_job_status
            job_status = get_job_status(bill.job_id)
            if job_status:
                status_data['job_status'] = job_status
            else:
                status_data['job_status'] = {'status': 'unknown', 'message': 'Job status unavailable'}
        
        return Response(status_data)
        
    except bill_model.DoesNotExist:
        return Response({
            'error': 'Bill not found',
            'message': f'{bill_type_name} bill with ID {bill_id} not found in organization {org_id}.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error checking {bill_type_name.lower()} bill processing status: {str(e)}")
        return Response({
            'error': 'Status check failed',
            'message': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# Bills by Status
# ============================================================================

def bills_by_status_base(request, org_id, bill_model, serializer_class):
    """
    Get bills filtered by status query param.
    
    Args:
        request: DRF request with 'status' query param
        org_id: Organization UUID
        bill_model: Model class
        serializer_class: Serializer for bill list
    
    Returns:
        Response with filtered bills
    """
    organization = get_organization_from_request(request, org_id)
    if not organization:
        return org_not_found_response(org_id)

    bills = bill_model.objects.filter(organization=organization)
    
    status_param = request.query_params.get('status', '').strip()
    if status_param:
        bills = bills.filter(status=status_param)
    
    bills = bills.order_by('-created_at')

    paginator = DefaultPagination()
    page = paginator.paginate_queryset(bills, request)
    if page is not None:
        serializer = serializer_class(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    serializer = serializer_class(bills, many=True, context={'request': request})
    return Response(serializer.data)


# ============================================================================
# Bill Upload
# ============================================================================

def bills_upload_base(
    request,
    org_id,
    bill_model,
    upload_serializer_class,
    response_serializer_class,
    pdf_split_func,
    enqueue_func,
    bill_type_label="",
    pdf_split_task_fn=None,
):
    """
    Generic upload handler for bills.
    
    Args:
        request: DRF request
        org_id: Organization UUID
        bill_model: Model class (TallyVendorBill or TallyExpenseBill)
        upload_serializer_class: Serializer for upload validation
        response_serializer_class: Serializer for response
        pdf_split_func: Function to split PDFs
        enqueue_func: Function to enqueue background processing
        bill_type_label: Label for log messages (e.g., "vendor", "expense")
    
    Returns:
        Response with created bills
    """
    # Handle both single file and multiple files seamlessly
    files_data = []

    if 'files' in request.data:
        files_data = request.data.getlist('files') if hasattr(request.data, 'getlist') else request.data.get('files', [])
        if not isinstance(files_data, list):
            files_data = [files_data] if files_data else []
    elif 'file' in request.data:
        single_file = request.data.get('file')
        if single_file:
            files_data = [single_file]

    # Accept BOTH ``file_type`` (backend canonical) and ``fileType``
    # (what the current React modal sends). Without the camelCase
    # fallback, the Tally upload path silently defaulted every request
    # to SINGLE — see #5 in the upload audit.
    incoming_file_type = (
        request.data.get('file_type')
        or request.data.get('fileType')
        or bill_model.BillType.SINGLE
    )
    serializer_data = {
        'files': files_data,
        'file_type': incoming_file_type,
    }

    serializer = upload_serializer_class(data=serializer_data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Upload Data',
            'message': f'The uploaded {bill_type_label} file data is invalid. Please check file format and size requirements.',
            'details': serializer.errors,
            'error_code': 'INVALID_UPLOAD_DATA'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    organization = get_organization_from_request(request, org_id)
    if not organization:
        return org_not_found_response(org_id)

    files = serializer.validated_data['files']
    file_type = serializer.validated_data['file_type']
    created_bills = []

    if not files:
        return Response({
            'error': 'No Files Provided',
            'message': f'At least one {bill_type_label} file must be provided for upload.',
            'error_code': 'NO_FILES_PROVIDED'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Track file paths written to storage inside the atomic block so we
    # can delete them if the transaction rolls back. Django's ``FileField``
    # writes the file to disk during ``.save()`` — BEFORE the atomic
    # commits — so a rollback leaves orphan files on the filesystem.
    # This list is consulted only in the ``except`` branch below.
    uploaded_file_paths = []
    # Bill ids created via the async PDF path — these are placeholders
    # whose analysis is triggered by the split task after per-page
    # rendering, so the after-commit callback must SKIP them.
    async_pdf_bill_ids = set()

    try:
        from django.core.files.storage import default_storage
        from django.db import transaction

        with transaction.atomic():
            upload_warnings = []
            rejected_files = []

            from apps.common.services.content_hash import (
                compute_file_hash, find_hash_duplicate,
            )
            from apps.common.services.document_classification import (
                classify_document, describe_rejection,
            )

            for uploaded_file in files:
                file_extension = uploaded_file.name.lower().split('.')[-1]

                # Check for potential duplicates
                upload_warnings.extend(
                    _check_file_duplicates(uploaded_file, organization, bill_model, bill_type_label)
                )

                # Exact-duplicate short-circuit via SHA-256 content hash
                # (see #8 in the upload audit). If a bill with the same
                # bytes already exists in this org, mark the incoming file
                # as duplicate warning and skip re-analysis.
                content_hash = compute_file_hash(uploaded_file)
                existing_hash_dup = find_hash_duplicate(
                    bill_model, organization, content_hash,
                )
                if existing_hash_dup:
                    upload_warnings.append({
                        'file_name': uploaded_file.name,
                        'warning_type': 'exact_duplicate',
                        'existing_bill_id': str(existing_hash_dup.id),
                        'existing_bill_name': getattr(
                            existing_hash_dup, 'bill_munshi_name', None,
                        ) or getattr(existing_hash_dup, 'billmunshiName', None),
                        'message': (
                            f"'{uploaded_file.name}' is byte-for-byte "
                            f"identical to a previously uploaded bill; "
                            f"skipping re-analysis."
                        ),
                    })
                    logger.info(
                        "Exact-duplicate detected for %s (hash=%s) — skipping upload",
                        uploaded_file.name, content_hash[:12],
                    )
                    continue

                # Screen the document BEFORE creating the Draft row, so a
                # non-bill never becomes a bill the user has to clean up.
                uploaded_file.seek(0)
                verdict = classify_document(uploaded_file.read(), file_extension)
                uploaded_file.seek(0)

                if not verdict['is_bill']:
                    rejected_files.append(
                        describe_rejection(uploaded_file.name, verdict)
                    )
                    logger.info(
                        "[%s] Rejected %s — detected as %s (confidence %.2f)",
                        bill_type_label, uploaded_file.name,
                        verdict['document_type'], verdict['confidence'],
                    )
                    continue

                # Handle PDF splitting for MULTI type. When an async task
                # function is supplied (#14 in the upload audit), persist
                # the PDF as a placeholder bill and let RQ do the page
                # rendering — otherwise fall back to the sync splitter.
                if file_type == bill_model.BillType.MULTI and file_extension == 'pdf':
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
                        pdf_bills = pdf_split_func(
                            uploaded_file, organization, file_type, request.user,
                        )
                    for pdf_bill in pdf_bills:
                        if pdf_bill.file:
                            uploaded_file_paths.append(pdf_bill.file.name)
                        if pdf_split_task_fn is not None:
                            async_pdf_bill_ids.add(str(pdf_bill.id))
                            # Placeholder carries the container-PDF hash;
                            # per-page bills get their own hash from the
                            # split task.
                            pdf_bill.content_hash = content_hash
                            pdf_bill.save(update_fields=["content_hash"])
                    created_bills.extend(pdf_bills)
                else:
                    bill = bill_model.objects.create(
                        file=uploaded_file,
                        file_type=file_type,
                        organization=organization,
                        uploaded_by=request.user,
                        content_hash=content_hash,
                    )
                    if bill.file:
                        uploaded_file_paths.append(bill.file.name)
                    created_bills.append(bill)

            # Enqueue RQ jobs via ``transaction.on_commit`` so they fire
            # ONLY if the surrounding transaction actually commits — a
            # rollback silently discards the pending callbacks and no
            # ghost jobs are queued for non-existent bill rows (#9 in
            # the upload audit).
            bill_ids_to_enqueue = [
                str(b.id) for b in created_bills
                if str(b.id) not in async_pdf_bill_ids
            ]

            def _enqueue_after_commit():
                for bill_id in bill_ids_to_enqueue:
                    try:
                        job = enqueue_func(bill_id)
                        bill_model.objects.filter(id=bill_id).update(
                            job_id=job.id,
                            is_processing=True,
                        )
                        logger.info(
                            "Queued %s bill %s (job=%s) after commit",
                            bill_type_label, bill_id, job.id,
                        )
                    except Exception as enqueue_err:
                        # Redis down / RQ crash mid-enqueue: log and
                        # move on. Bill row stays in Draft with
                        # ``is_processing=False`` and can be manually
                        # analysed by the user.
                        logger.error(
                            "Failed to enqueue %s bill %s: %s",
                            bill_type_label, bill_id, enqueue_err,
                        )

            transaction.on_commit(_enqueue_after_commit)

        # Response reports the bills as ``queued`` — actual job_ids are
        # populated post-commit; the frontend polls the status endpoint
        # to see when the analysis is done.
        job_results = [
            {
                'bill_id': str(bill.id),
                'bill_name': bill.bill_munshi_name,
                'job_id': None,          # set post-commit by the callback
                'status': 'queued_for_processing',
            }
            for bill in created_bills
        ]

        # Nothing survived screening — say so rather than reporting success
        # with zero bills.
        if rejected_files and not created_bills:
            return Response({
                'error': 'No Bills Uploaded',
                'detail': 'None of the uploaded files look like bills or invoices.',
                'rejected_files': rejected_files,
                'files_rejected': len(rejected_files),
            }, status=status.HTTP_400_BAD_REQUEST)

        response_serializer = response_serializer_class(created_bills, many=True, context={'request': request})

        accepted = len(files) - len(rejected_files)
        response_data = {
            'message': f'Successfully uploaded {accepted} file(s) and created {len(created_bills)} {bill_type_label} bill(s). Processing started in background.',
            'files_uploaded': accepted,
            'bills_created': len(created_bills),
            'bills': response_serializer.data,
            'processing_jobs': job_results,
            'note': 'Bills are being processed in the background. Use the bill status endpoint to check progress.'
        }

        if rejected_files:
            response_data['rejected_files'] = rejected_files
            response_data['files_rejected'] = len(rejected_files)
            response_data['rejection_message'] = (
                f"{len(rejected_files)} file(s) were not uploaded because they "
                "do not look like bills."
            )

        if upload_warnings:
            response_data['upload_warnings'] = upload_warnings
            response_data['warning_message'] = f"📁 FILE WARNING: {len(upload_warnings)} file(s) may be duplicates"

        return Response(response_data, status=status.HTTP_201_CREATED)

    except Exception as e:
        logger.error(f"Error uploading {bill_type_label} bills: {str(e)}")
        # The atomic block rolled back — DB rows are gone but the
        # FileField already wrote the underlying files to storage.
        # Delete any orphans so /media/bills/ doesn't accumulate
        # unowned uploads (see #10 in the audit).
        try:
            from django.core.files.storage import default_storage
            for path in uploaded_file_paths:
                try:
                    if default_storage.exists(path):
                        default_storage.delete(path)
                        logger.info("Cleaned up orphan upload after rollback: %s", path)
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to delete orphan upload %s: %s", path, cleanup_err,
                    )
        except Exception:
            pass

        return Response({
            'error': f'{bill_type_label.title()} File Upload Processing Failed',
            'message': 'There was an error processing the uploaded files.',
            'details': str(e),
            'error_code': 'UPLOAD_PROCESSING_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _check_file_duplicates(uploaded_file, organization, bill_model, bill_type_label=""):
    """Check for potential file-level duplicates."""
    warnings = []
    
    similar_files = bill_model.objects.filter(
        organization=organization,
        file__isnull=False
    ).exclude(status=bill_model.BillStatus.DRAFT)

    potential_duplicates = []
    uploaded_filename = uploaded_file.name.lower()
    uploaded_basename = uploaded_filename.replace('.pdf', '').replace('.jpg', '').replace('.png', '')

    for existing_bill in similar_files:
        if not (existing_bill.file and existing_bill.file.name):
            continue
            
        existing_filename = os.path.basename(existing_bill.file.name).lower()
        existing_basename = existing_filename.replace('.pdf', '').replace('.jpg', '').replace('.png', '')

        if existing_filename == uploaded_filename:
            potential_duplicates.append({
                'bill': existing_bill,
                'match_type': 'exact_filename',
                'reason': 'Same filename detected'
            })
        elif existing_basename == uploaded_basename:
            potential_duplicates.append({
                'bill': existing_bill,
                'match_type': 'similar_filename',
                'reason': 'Similar filename detected'
            })
        else:
            # Check file size similarity
            try:
                if (hasattr(existing_bill.file.storage, 'exists') and
                    existing_bill.file.storage.exists(existing_bill.file.name) and
                    hasattr(existing_bill.file, 'size') and hasattr(uploaded_file, 'size')):
                    
                    existing_size = existing_bill.file.size
                    uploaded_size = uploaded_file.size
                    
                    if (existing_size > 0 and uploaded_size > 0 and
                        abs(existing_size - uploaded_size) / max(existing_size, uploaded_size) < 0.05):
                        potential_duplicates.append({
                            'bill': existing_bill,
                            'match_type': 'similar_size',
                            'reason': 'Similar file size detected'
                        })
            except (FileNotFoundError, OSError):
                continue

    if potential_duplicates:
        warnings.append({
            'uploaded_file': uploaded_file.name,
            'potential_duplicates': len(potential_duplicates),
            'warning': f'File "{uploaded_file.name}" may be a duplicate of existing {bill_type_label} bills',
            'existing_bills': [
                {
                    'bill_name': dup['bill'].bill_munshi_name,
                    'bill_id': str(dup['bill'].id),
                    'match_type': dup['match_type'],
                    'reason': dup['reason']
                } for dup in potential_duplicates[:3]
            ]
        })

    return warnings


# ============================================================================
# Bill Analyze
# ============================================================================

def bill_analyze_base(
    request,
    org_id,
    bill_model,
    analyzed_bill_model,
    analysis_request_serializer,
    analyzed_bill_serializer,
    process_analysis_func,
    process_existing_analysis_func,
    bill_type_label="",
):
    """
    Generic analyze handler for bills.
    
    Args:
        request: DRF request
        org_id: Organization UUID
        bill_model: Bill model class
        analyzed_bill_model: AnalyzedBill model class
        analysis_request_serializer: Request serializer
        analyzed_bill_serializer: Response serializer
        process_analysis_func: Function to process raw analysis data
        process_existing_analysis_func: Function to process existing analysis
        bill_type_label: Label for messages
    
    Returns:
        Response with analysis results
    """
    serializer = analysis_request_serializer(data=request.data)
    if not serializer.is_valid():
        return Response({
            'error': 'Invalid Analysis Request',
            'message': f'The {bill_type_label} bill analysis request data is invalid.',
            'details': serializer.errors,
            'error_code': 'INVALID_ANALYSIS_REQUEST'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    bill_id = serializer.validated_data['bill_id']
    organization = get_organization_from_request(request, org_id)

    try:
        bill = bill_model.objects.get(id=bill_id, organization=organization)
    except bill_model.DoesNotExist:
        return Response(
            {'error': f'{bill_type_label.title()} bill not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    # Check if already analyzed
    existing_analyzed = analyzed_bill_model.objects.filter(selected_bill=bill).first()
    if existing_analyzed:
        serializer = analyzed_bill_serializer(existing_analyzed)
        return Response({
            'message': f'{bill_type_label.title()} bill already analyzed',
            'analyzed_bill': serializer.data
        })

    # Check if analysed_data exists in the bill
    if not bill.analysed_data:
        return Response({
            'error': 'Analysis Data Not Available',
            'message': f'No analysis data found for this {bill_type_label} bill. Please wait for processing to complete.',
            'error_code': 'NO_ANALYSIS_DATA'
        }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    try:
        analyzed_bill = process_existing_analysis_func(bill, bill.analysed_data, organization)
        
        bill.status = bill_model.BillStatus.ANALYSED
        bill.save(update_fields=['status'])
        
        response_serializer = analyzed_bill_serializer(analyzed_bill)
        return Response({
            'message': f'{bill_type_label.title()} bill analyzed successfully',
            'analyzed_bill': response_serializer.data
        })

    except Exception as e:
        logger.error(f"Error analyzing {bill_type_label} bill: {str(e)}")
        return Response({
            'error': 'Analysis Processing Failed',
            'message': f'Error processing {bill_type_label} bill analysis.',
            'details': str(e),
            'error_code': 'ANALYSIS_PROCESSING_FAILED'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
