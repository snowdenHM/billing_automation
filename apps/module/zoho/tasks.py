# apps/module/zoho/tasks.py

import logging
import django_rq
from django.apps import apps

logger = logging.getLogger(__name__)


def enqueue_vendor_bill_analysis(bill_id, organization_id):
    """Enqueue vendor bill analysis task"""
    queue = django_rq.get_queue('default')
    return queue.enqueue(
        process_zoho_vendor_bill_analysis,
        bill_id,
        organization_id,
        timeout=600
    )


def enqueue_expense_bill_analysis(bill_id, organization_id):
    """Enqueue expense bill analysis task"""
    queue = django_rq.get_queue('default')
    return queue.enqueue(
        process_zoho_expense_bill_analysis,
        bill_id,
        organization_id,
        timeout=600
    )


def enqueue_journal_bill_analysis(bill_id, organization_id):
    """Enqueue journal bill analysis task"""
    queue = django_rq.get_queue('default')
    return queue.enqueue(
        process_zoho_journal_bill_analysis,
        bill_id,
        organization_id,
        timeout=600
    )


@django_rq.job('default', timeout=600)
def process_zoho_vendor_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task to analyze Zoho vendor bill and check for duplicates"""
    from .models import VendorBill
    from apps.organizations.models import Organization
    
    # Lazy import to avoid circular imports
    def get_analyze_function():
        from .vendor_views import analyze_vendor_bill_with_openai, create_vendor_zoho_objects_from_analysis
        return analyze_vendor_bill_with_openai, create_vendor_zoho_objects_from_analysis
    
    try:
        # Get bill and organization
        bill = VendorBill.objects.get(id=bill_id)
        organization = Organization.objects.get(id=organization_id)
        
        # Mark as processing
        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])
        
        logger.info(f"Starting background processing for Zoho vendor bill {bill_id}")
        
        # Step 1: AI Analysis + Full Automation + Duplicate Detection
        try:
            analyze_bill, create_zoho_objects = get_analyze_function()
            
            # Read file content
            if not bill.file:
                raise Exception("No file attached to bill")
                
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
            
            # Analyze with OpenAI
            logger.info(f"🤖 Running AI analysis for Zoho vendor bill {bill_id}")
            analyzed_data = analyze_bill(file_content, file_extension)
            
            # Update bill with analyzed data
            bill.analysed_data = analyzed_data
            bill.status = VendorBill.BillStatus.ANALYSED
            bill.save(update_fields=['analysed_data', 'status'])
            
            # Create Zoho objects with FULL AUTOMATION (includes duplicate detection)
            logger.info(f"⚙️ Running full automation for Zoho vendor bill {bill_id}")
            zoho_bill = create_zoho_objects(bill, analyzed_data, organization)
            
            logger.info(f"✅ Background automation completed successfully for Zoho vendor bill {bill_id}")
            
        except Exception as e:
            logger.error(f"❌ AI analysis + automation failed for Zoho vendor bill {bill_id}: {str(e)}")
            bill.is_processing = False
            bill.processing_error = f"Analysis + automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
            raise
        
        # Mark as processing complete
        bill.is_processing = False
        bill.save(update_fields=['is_processing'])
        
        logger.info(f"🎉 Background processing with full automation completed for Zoho vendor bill {bill_id}")
        return f"Successfully processed Zoho vendor bill {bill_id} with full field automation"
        
    except VendorBill.DoesNotExist:
        logger.error(f"❌ Zoho vendor bill {bill_id} not found")
        raise
    except Exception as e:
        logger.error(f"❌ Background automation failed for Zoho vendor bill {bill_id}: {str(e)}")
        try:
            bill = VendorBill.objects.get(id=bill_id)
            bill.is_processing = False
            bill.processing_error = f"Automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
        except:
            pass
        raise


@django_rq.job('default', timeout=600)
def process_zoho_expense_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task to analyze Zoho expense bill and check for duplicates"""
    from .models import ExpenseBill
    from apps.organizations.models import Organization
    
    # Lazy import to avoid circular imports
    def get_analyze_function():
        from .expense_views import analyze_bill_with_openai, create_expense_zoho_objects_from_analysis, check_duplicate_expense_bill
        return analyze_bill_with_openai, create_expense_zoho_objects_from_analysis, check_duplicate_expense_bill
    
    try:
        # Get bill and organization
        bill = ExpenseBill.objects.get(id=bill_id)
        organization = Organization.objects.get(id=organization_id)
        
        # Mark as processing
        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])
        
        logger.info(f"🚀 Starting background automation processing for Zoho expense bill {bill_id}")
        
        # Step 1: AI Analysis + Full Automation (if available)
        try:
            analyze_bill, create_zoho_objects, check_duplicate = get_analyze_function()
            
            # Read file content
            if not bill.file:
                raise Exception("No file attached to bill")
                
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
            
            # Analyze with OpenAI
            logger.info(f"🤖 Running AI analysis for Zoho expense bill {bill_id}")
            analyzed_data = analyze_bill(file_content, file_extension)
            
            # Create Zoho objects (with automation if available)
            logger.info(f"⚙️ Running automation for Zoho expense bill {bill_id}")
            create_zoho_objects(bill, analyzed_data, organization)
            
            # Update bill status
            bill.status = ExpenseBill.BillStatus.ANALYSED
            bill.save(update_fields=['status'])
            
        except Exception as e:
            logger.error(f"❌ AI analysis + automation failed for Zoho expense bill {bill_id}: {str(e)}")
            bill.is_processing = False
            bill.processing_error = f"Analysis + automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
            raise
        
        # Step 2: Check for duplicates (fallback if not integrated)
        try:
            duplicate_result = check_duplicate(bill, organization)
            
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                
                # Update duplicate fields
                bill.is_duplicate = is_duplicate
                bill.duplicate_score = similarity_score
                
                if is_duplicate:
                    # Create detailed duplicate description
                    duplicate_info = []
                    for dup_data in duplicate_bills[:3]:  # Limit to top 3 matches
                        duplicate_info.append({
                            'bill_id': str(dup_data['bill'].id),
                            'invoice_number': dup_data.get('invoice_number', 'N/A'),
                            'vendor_name': dup_data.get('vendor_name', 'N/A'),
                            'total': dup_data.get('total', 0),
                            'date': dup_data.get('date', 'N/A'),
                            'similarity_score': dup_data.get('similarity_score', 0)
                        })
                    
                    bill.duplicate_matched_bills = duplicate_info
                    bill.duplicate_description = f"Found {len(duplicate_bills)} potential duplicate(s) with {similarity_score:.1f}% similarity"
                else:
                    bill.duplicate_description = "No duplicates found"
                    bill.duplicate_matched_bills = []
                    
                bill.save(update_fields=['is_duplicate', 'duplicate_score', 'duplicate_matched_bills', 'duplicate_description'])
                logger.info(f"Duplicate check completed for Zoho expense bill {bill_id}: {is_duplicate}")
                
        except Exception as e:
            logger.error(f"Duplicate check failed for Zoho expense bill {bill_id}: {str(e)}")
            # Don't fail the entire task for duplicate check errors
            bill.duplicate_description = f"Duplicate check failed: {str(e)}"
            bill.save(update_fields=['duplicate_description'])
        
        # Mark as processing complete
        bill.is_processing = False
        bill.save(update_fields=['is_processing'])
        
        logger.info(f"🎉 Background automation completed for Zoho expense bill {bill_id}")
        return f"Successfully processed Zoho expense bill {bill_id} with automation"
        
    except ExpenseBill.DoesNotExist:
        logger.error(f"❌ Zoho expense bill {bill_id} not found")
        raise
    except Exception as e:
        logger.error(f"❌ Background automation failed for Zoho expense bill {bill_id}: {str(e)}")
        try:
            bill = ExpenseBill.objects.get(id=bill_id)
            bill.is_processing = False
            bill.processing_error = f"Automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
        except:
            pass
        raise


@django_rq.job('default', timeout=600)
def process_zoho_journal_bill_analysis(bill_id, organization_id, **kwargs):
    """Background task to analyze Zoho journal bill and check for duplicates"""
    from .models import JournalBill
    from apps.organizations.models import Organization
    
    # Lazy import to avoid circular imports
    def get_analyze_function():
        from .journal_views import analyze_bill_with_openai, create_journal_zoho_objects_from_analysis, check_duplicate_journal_bill
        return analyze_bill_with_openai, create_journal_zoho_objects_from_analysis, check_duplicate_journal_bill
    
    try:
        # Get bill and organization
        bill = JournalBill.objects.get(id=bill_id)
        organization = Organization.objects.get(id=organization_id)
        
        # Mark as processing
        bill.is_processing = True
        bill.processing_error = ""
        bill.save(update_fields=['is_processing', 'processing_error'])
        
        logger.info(f"🚀 Starting background automation processing for Zoho journal bill {bill_id}")
        
        # Step 1: AI Analysis + Full Automation (if available) 
        try:
            analyze_bill, create_zoho_objects, check_duplicate = get_analyze_function()
            
            # Read file content
            if not bill.file:
                raise Exception("No file attached to bill")
                
            file_content = bill.file.read()
            file_extension = bill.file.name.split('.')[-1].lower()
            
            # Analyze with OpenAI
            logger.info(f"🤖 Running AI analysis for Zoho journal bill {bill_id}")
            analyzed_data = analyze_bill(file_content, file_extension)
            
            # Create Zoho objects (with automation if available)
            logger.info(f"⚙️ Running automation for Zoho journal bill {bill_id}")
            create_zoho_objects(bill, analyzed_data, organization)
            
            # Update bill status
            bill.status = JournalBill.BillStatus.ANALYSED
            bill.save(update_fields=['status'])
            
        except Exception as e:
            logger.error(f"❌ AI analysis + automation failed for Zoho journal bill {bill_id}: {str(e)}")
            bill.is_processing = False
            bill.processing_error = f"Analysis + automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
            raise
        
        # Step 2: Check for duplicates (fallback if not integrated)
        try:
            duplicate_result = check_duplicate(bill, organization)
            
            if duplicate_result:
                is_duplicate, duplicate_bills, similarity_score = duplicate_result
                
                # Update duplicate fields
                bill.is_duplicate = is_duplicate
                bill.duplicate_score = similarity_score
                
                if is_duplicate:
                    # Create detailed duplicate description
                    duplicate_info = []
                    for dup_data in duplicate_bills[:3]:  # Limit to top 3 matches
                        duplicate_info.append({
                            'bill_id': str(dup_data['bill'].id),
                            'invoice_number': dup_data.get('invoice_number', 'N/A'),
                            'vendor_name': dup_data.get('vendor_name', 'N/A'),
                            'total': dup_data.get('total', 0),
                            'date': dup_data.get('date', 'N/A'),
                            'similarity_score': dup_data.get('similarity_score', 0)
                        })
                    
                    bill.duplicate_matched_bills = duplicate_info
                    bill.duplicate_description = f"Found {len(duplicate_bills)} potential duplicate(s) with {similarity_score:.1f}% similarity"
                else:
                    bill.duplicate_description = "No duplicates found"
                    bill.duplicate_matched_bills = []
                    
                bill.save(update_fields=['is_duplicate', 'duplicate_score', 'duplicate_matched_bills', 'duplicate_description'])
                logger.info(f"Duplicate check completed for Zoho journal bill {bill_id}: {is_duplicate}")
                
        except Exception as e:
            logger.error(f"Duplicate check failed for Zoho journal bill {bill_id}: {str(e)}")
            # Don't fail the entire task for duplicate check errors
            bill.duplicate_description = f"Duplicate check failed: {str(e)}"
            bill.save(update_fields=['duplicate_description'])
        
        # Mark as processing complete
        bill.is_processing = False
        bill.save(update_fields=['is_processing'])
        
        logger.info(f"🎉 Background automation completed for Zoho journal bill {bill_id}")
        return f"Successfully processed Zoho journal bill {bill_id} with automation"
        
    except JournalBill.DoesNotExist:
        logger.error(f"❌ Zoho journal bill {bill_id} not found")
        raise
    except Exception as e:
        logger.error(f"❌ Background automation failed for Zoho journal bill {bill_id}: {str(e)}")
        try:
            bill = JournalBill.objects.get(id=bill_id)
            bill.is_processing = False
            bill.processing_error = f"Automation failed: {str(e)}"
            bill.save(update_fields=['is_processing', 'processing_error'])
        except:
            pass
        raise