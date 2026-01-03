# apps/module/tally/tasks.py
"""
Background tasks for Tally bill processing using Django-RQ
"""
import logging
import django_rq
from rq import get_current_job
from django.utils import timezone
from .models import TallyVendorBill, TallyExpenseBill

logger = logging.getLogger(__name__)


# Helper functions for RQ job management
def enqueue_vendor_bill_processing(bill_id):
    """
    Enqueue a vendor bill for background processing
    """
    queue = django_rq.get_queue('default')
    job = queue.enqueue(process_vendor_bill_analysis, bill_id, timeout=600)
    logger.info(f"Enqueued vendor bill {bill_id} processing - Job ID: {job.id}")
    return job


def enqueue_expense_bill_processing(bill_id):
    """
    Enqueue an expense bill for background processing
    """
    queue = django_rq.get_queue('default')
    job = queue.enqueue(process_expense_bill_analysis, bill_id, timeout=600)
    logger.info(f"Enqueued expense bill {bill_id} processing - Job ID: {job.id}")
    return job


def get_job_status(job_id):
    """
    Get the status of a background job
    """
    try:
        import redis
        from rq import Job
        from django_rq import get_connection
        
        connection = get_connection('default')
        job = Job.fetch(job_id, connection=connection)
        
        return {
            'id': job.id,
            'status': job.get_status(),
            'result': job.result,
            'exc_info': job.exc_info,
            'created_at': job.created_at,
            'started_at': job.started_at,
            'ended_at': job.ended_at,
        }
    except Exception as e:
        logger.error(f"Error fetching job {job_id}: {str(e)}")
        return None


def process_vendor_bill_analysis(bill_id, **kwargs):
    """
    Background task to analyze vendor bill and check for duplicates
    """
    # Lazy import to avoid circular imports
    from .vendor_views_functional import analyze_bill_with_ai, check_duplicate_tally_vendor_bill
    
    job = get_current_job()
    try:
        bill = TallyVendorBill.objects.get(id=bill_id)
        organization = bill.organization
        
        # Mark as processing
        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])
        
        logger.info(f"Starting background analysis for vendor bill {bill_id}")
        
        # Step 1: Analyze bill with AI
        try:
            analyzed_bill = analyze_bill_with_ai(bill, organization)
            logger.info(f"AI analysis completed for vendor bill {bill_id}")
        except Exception as e:
            logger.error(f"AI analysis failed for vendor bill {bill_id}: {str(e)}")
            bill.is_processing = False
            bill.processing_error = f"AI analysis failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
            raise
        
        # Step 2: Check for duplicates
        try:
            duplicate_result = check_duplicate_tally_vendor_bill(bill, organization)
            
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                
                # Update duplicate fields
                bill.is_duplicate = is_duplicate
                bill.duplicate_score = similarity_score
                
                if is_duplicate:
                    # Create detailed duplicate description
                    duplicate_info = []
                    for dup_bill in duplicate_bills[:3]:  # Limit to top 3 matches
                        dup_data = dup_bill.analysed_data or {}
                        duplicate_info.append({
                            'bill_id': str(dup_bill.id),
                            'invoice_number': dup_data.get('invoiceNumber', 'N/A'),
                            'vendor_name': dup_data.get('from', {}).get('name', 'N/A'),
                            'total': dup_data.get('total', 0),
                            'date': dup_data.get('dateIssued', 'N/A')
                        })
                    
                    bill.duplicate_matched_bills = duplicate_info
                    bill.duplicate_description = f"Found {len(duplicate_bills)} potential duplicate(s) with {similarity_score:.1f}% similarity"
                else:
                    bill.duplicate_description = "No duplicates found"
                    bill.duplicate_matched_bills = []
                    
                bill.save(update_fields=['is_duplicate', 'duplicate_score', 'duplicate_matched_bills', 'duplicate_description'])
                logger.info(f"Duplicate check completed for vendor bill {bill_id}: {is_duplicate}")
                
        except Exception as e:
            logger.error(f"Duplicate check failed for vendor bill {bill_id}: {str(e)}")
            # Don't fail the entire task for duplicate check errors
            bill.duplicate_description = f"Duplicate check failed: {str(e)}"
            bill.save(update_fields=['duplicate_description'])
        
        # Mark as processing complete
        bill.is_processing = False
        bill.save(update_fields=['is_processing'])
        
        logger.info(f"Background processing completed for vendor bill {bill_id}")
        return f"Successfully processed vendor bill {bill_id}"
        
    except TallyVendorBill.DoesNotExist:
        logger.error(f"Vendor bill {bill_id} not found")
        raise
    except Exception as e:
        logger.error(f"Background processing failed for vendor bill {bill_id}: {str(e)}")
        try:
            bill = TallyVendorBill.objects.get(id=bill_id)
            bill.is_processing = False
            bill.processing_error = str(e)
            bill.save(update_fields=['is_processing', 'processing_error'])
        except:
            pass
        raise


def process_expense_bill_analysis(bill_id, **kwargs):
    """
    Background task to analyze expense bill and check for duplicates
    """
    # Lazy import to avoid circular imports
    from .expense_views_functional import analyze_expense_bill_with_ai, check_duplicate_tally_expense_bill
    
    job = get_current_job()
    try:
        bill = TallyExpenseBill.objects.get(id=bill_id)
        organization = bill.organization
        
        # Mark as processing
        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])
        
        logger.info(f"Starting background analysis for expense bill {bill_id}")
        
        # Step 1: Analyze bill with AI
        try:
            analyzed_bill = analyze_expense_bill_with_ai(bill, organization)
            logger.info(f"AI analysis completed for expense bill {bill_id}")
        except Exception as e:
            logger.error(f"AI analysis failed for expense bill {bill_id}: {str(e)}")
            bill.is_processing = False
            bill.processing_error = f"AI analysis failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
            raise
        
        # Step 2: Check for duplicates
        try:
            duplicate_result = check_duplicate_tally_expense_bill(bill, organization)
            
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                
                # Update duplicate fields
                bill.is_duplicate = is_duplicate
                bill.duplicate_score = similarity_score
                
                if is_duplicate:
                    # Create detailed duplicate description
                    duplicate_info = []
                    for dup_bill in duplicate_bills[:3]:  # Limit to top 3 matches
                        dup_data = dup_bill.analysed_data or {}
                        duplicate_info.append({
                            'bill_id': str(dup_bill.id),
                            'bill_number': dup_data.get('billNumber', 'N/A'),
                            'vendor_name': dup_data.get('from', {}).get('name', 'N/A'),
                            'total': dup_data.get('total', 0),
                            'date': dup_data.get('dateIssued', 'N/A')
                        })
                    
                    bill.duplicate_matched_bills = duplicate_info
                    bill.duplicate_description = f"Found {len(duplicate_bills)} potential duplicate(s) with {similarity_score:.1f}% similarity"
                else:
                    bill.duplicate_description = "No duplicates found"
                    bill.duplicate_matched_bills = []
                    
                bill.save(update_fields=['is_duplicate', 'duplicate_score', 'duplicate_matched_bills', 'duplicate_description'])
                logger.info(f"Duplicate check completed for expense bill {bill_id}: {is_duplicate}")
                
        except Exception as e:
            logger.error(f"Duplicate check failed for expense bill {bill_id}: {str(e)}")
            # Don't fail the entire task for duplicate check errors
            bill.duplicate_description = f"Duplicate check failed: {str(e)}"
            bill.save(update_fields=['duplicate_description'])
        
        # Mark as processing complete
        bill.is_processing = False
        bill.save(update_fields=['is_processing'])
        
        logger.info(f"Background processing completed for expense bill {bill_id}")
        return f"Successfully processed expense bill {bill_id}"
        
    except TallyExpenseBill.DoesNotExist:
        logger.error(f"Expense bill {bill_id} not found")
        raise
    except Exception as e:
        logger.error(f"Background processing failed for expense bill {bill_id}: {str(e)}")
        try:
            bill = TallyExpenseBill.objects.get(id=bill_id)
            bill.is_processing = False
            bill.processing_error = str(e)
            bill.save(update_fields=['is_processing', 'processing_error'])
        except:
            pass
        raise


def process_multiple_bills(bill_ids, bill_type='vendor'):
    """
    Process multiple bills in batch for better performance
    """
    logger.info(f"Processing batch of {len(bill_ids)} {bill_type} bills")
    
    # Get the default queue
    queue = django_rq.get_queue('default')
    
    results = []
    for bill_id in bill_ids:
        try:
            if bill_type == 'vendor':
                job = queue.enqueue(process_vendor_bill_analysis, bill_id, timeout=600)
            else:
                job = queue.enqueue(process_expense_bill_analysis, bill_id, timeout=600)
            results.append(f"Started processing {bill_type} bill {bill_id} - Job ID: {job.id}")
        except Exception as e:
            logger.error(f"Failed to start processing {bill_type} bill {bill_id}: {str(e)}")
            results.append(f"Failed to start processing {bill_type} bill {bill_id}: {str(e)}")
    
    return results