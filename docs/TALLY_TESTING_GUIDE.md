# Tally App Testing Guide

## Overview
The Tally app provides comprehensive functionality for managing vendor bills and expense bills with AI-powered analysis, verification workflows, and external system synchronization.

## Architecture
The Tally app consists of the following main components:

### Models
- **ParentLedger**: Top-level ledger categories (e.g., "Sundry Creditors", "Duties & Taxes")
- **Ledger**: Individual ledger entries under parent ledgers
- **TallyConfig**: Configuration mapping parent ledgers to functional categories
- **TallyVendorBill/TallyExpenseBill**: Uploaded bill files with workflow status
- **TallyVendorAnalyzedBill/TallyExpenseAnalyzedBill**: AI-analyzed bill data
- **TallyVendorAnalyzedProduct/TallyExpenseAnalyzedProduct**: Individual line items

### Workflow States
Bills progress through these states:
1. **Draft** → Initial upload
2. **Analysed** → AI has extracted data
3. **Verified** → Human has verified/corrected data
4. **Synced** → Data sent to external system

## Setup Instructions

### 1. Prerequisites
- Django application running
- OpenAI API key configured in settings
- Organization created with proper permissions

### 2. Create Dummy Data
```bash
# Create dummy data for default organization
python manage.py create_tally_dummy_data

# Create dummy data for specific organization
python manage.py create_tally_dummy_data --org-id <organization-uuid>

# Clear existing data and create fresh dummy data
python manage.py create_tally_dummy_data --org-id <organization-uuid> --clear-existing
```

### 3. Verify Setup
After creating dummy data, you should have:
- 11 Parent Ledgers (various categories)
- 35+ Ledgers (vendors, taxes, expenses)
- 1 Tally Configuration
- 20 Vendor Bills (various statuses)
- 15 Expense Bills (various statuses)
- Analyzed bills with products/line items

## API Endpoints

### Base URL
All endpoints are under: `/api/module/tally/org/{org_id}/`

### Authentication
- **API Key**: Include in header `X-API-Key: your-api-key`
- **JWT Token**: Include in header `Authorization: Bearer your-jwt-token`

### Ledger Management

#### Get Ledgers
```
GET /ledgers/
```
Returns all ledgers for the organization.

#### Create Ledger
```
POST /ledgers/
Content-Type: application/json

{
    "name": "New Vendor Ltd",
    "parent": "parent-ledger-uuid",
    "master_id": "VEN123456",
    "gst_in": "27AABCU9603R1ZN",
    "company": "New Vendor Ltd",
    "opening_balance": "50000.00"
}
```

### Tally Configuration

#### Get Configuration
```
GET /configs/
```

#### Update Configuration
```
PUT /configs/{config-id}/
Content-Type: application/json

{
    "igst_parents": ["parent-ledger-uuid"],
    "cgst_parents": ["parent-ledger-uuid"],
    "sgst_parents": ["parent-ledger-uuid"],
    "vendor_parents": ["parent-ledger-uuid"],
    "chart_of_accounts_parents": ["parent-ledger-uuid"],
    "chart_of_accounts_expense_parents": ["parent-ledger-uuid"]
}
```

## Vendor Bills Workflow

### 1. Upload Bills
```
POST /vendor-bills/upload/
Content-Type: multipart/form-data

files: [file1.pdf, file2.pdf]
file_type: "Single Invoice/File" or "Multiple Invoice/File"
```

**Response:**
```json
[
    {
        "id": "bill-uuid",
        "bill_munshi_name": "BM-TB-1",
        "file": "/media/bills/file.pdf",
        "file_type": "Single Invoice/File",
        "status": "Draft",
        "process": false,
        "created_at": "2025-09-02T10:00:00Z"
    }
]
```

### 2. Analyze Bills with AI
```
POST /vendor-bills/analyze/
Content-Type: application/json

{
    "bill_id": "bill-uuid"
}
```

**Response:**
```json
{
    "id": "analyzed-bill-uuid",
    "selected_bill": "bill-uuid",
    "vendor": "vendor-ledger-uuid",
    "bill_no": "INV-001",
    "bill_date": "2025-09-01",
    "total": "10000.00",
    "igst": "1800.00",
    "cgst": "0.00",
    "sgst": "0.00",
    "products": [
        {
            "id": "product-uuid",
            "item_name": "Software License",
            "item_details": "Annual software license",
            "quantity": 1,
            "price": "8474.58",
            "amount": "8474.58",
            "product_gst": "18%",
            "igst": "1525.42"
        }
    ]
}
```

### 3. Verify Bills
```
POST /vendor-bills/verify/
Content-Type: application/json

{
    "bill_id": "bill-uuid",
    "vendor_id": "vendor-ledger-uuid",
    "bill_no": "INV-001-CORRECTED",
    "bill_date": "2025-09-01",
    "note": "Verified and corrected",
    "igst": "1800.00",
    "igst_taxes_id": "igst-ledger-uuid",
    "products": [
        {
            "id": "product-uuid",
            "item_name": "Software License - Corrected",
            "quantity": 1,
            "price": "8474.58"
        }
    ]
}
```

### 4. Sync to External System
```
POST /vendor-bills/sync_external/
Content-Type: application/json

{
    "bill_id": "bill-uuid"
}
```

### 5. Get Bills by Status
```
GET /vendor-bills/by_status/?status=Draft
GET /vendor-bills/by_status/?status=Analysed
GET /vendor-bills/by_status/?status=Verified
GET /vendor-bills/by_status/?status=Synced
```

### 6. Get All Synced Bills
```
GET /vendor-bills/sync_bills/
```

## Expense Bills Workflow

### 1. Upload Expense Bills
```
POST /expense-bills/upload/
Content-Type: multipart/form-data

files: [expense1.pdf, expense2.pdf]
file_type: "Single Invoice/File" or "Multiple Invoice/File"
```

### 2. Analyze Expense Bills
```
POST /expense-bills/analyze/
Content-Type: application/json

{
    "bill_id": "expense-bill-uuid"
}
```

**Response:**
```json
{
    "id": "analyzed-expense-uuid",
    "selected_bill": "expense-bill-uuid",
    "vendor": "vendor-ledger-uuid",
    "voucher": "EXP-VOUCHER-001",
    "bill_no": "EXP-001",
    "bill_date": "2025-09-01",
    "total": "5000.00",
    "igst": "900.00",
    "products": [
        {
            "id": "expense-product-uuid",
            "item_details": "Office rent for September",
            "chart_of_accounts": "office-rent-ledger-uuid",
            "amount": "4100.00",
            "debit_or_credit": "debit"
        }
    ]
}
```

### 3. Verify Expense Bills
```
POST /expense-bills/verify/
Content-Type: application/json

{
    "bill_id": "expense-bill-uuid",
    "vendor_id": "vendor-ledger-uuid",
    "voucher": "EXP-VOUCHER-001-CORRECTED",
    "bill_no": "EXP-001",
    "bill_date": "2025-09-01",
    "note": "Verified expense bill",
    "products": [
        {
            "id": "expense-product-uuid",
            "item_details": "Office rent for September - Verified",
            "chart_of_accounts_id": "office-rent-ledger-uuid",
            "amount": "4100.00",
            "debit_or_credit": "debit"
        }
    ]
}
```

### 4. Sync Expense Bills
```
POST /expense-bills/sync_external/
Content-Type: application/json

{
    "bill_id": "expense-bill-uuid"
}
```

## Testing Scenarios

### Scenario 1: Complete Vendor Bill Workflow
1. Upload a vendor bill PDF
2. Analyze with AI (requires OpenAI API key)
3. Verify and correct any errors
4. Sync to external system
5. Verify bill status is "Synced"

### Scenario 2: Complete Expense Bill Workflow
1. Upload an expense bill PDF
2. Analyze with AI
3. Verify and assign correct chart of accounts
4. Sync to external system
5. Verify bill status is "Synced"

### Scenario 3: Multi-Invoice PDF Processing
1. Upload a PDF with multiple invoices (file_type: "Multiple Invoice/File")
2. Verify PDF is split into individual bills
3. Process each bill through the workflow

### Scenario 4: Configuration Management
1. Get current tally configuration
2. Update parent ledger mappings
3. Verify configuration changes affect bill processing

## Error Handling

### Common Error Responses

#### 400 Bad Request
```json
{
    "error": "Bill is not in draft status"
}
```

#### 404 Not Found
```json
{
    "error": "Bill not found"
}
```

#### 500 Internal Server Error
```json
{
    "error": "AI processing failed: API key not configured"
}
```

## Performance Considerations

### File Upload Limits
- Maximum file size: 10MB per file
- Supported formats: PDF, JPG, JPEG, PNG
- Multiple files can be uploaded in a single request

### AI Processing
- Requires OpenAI API key configuration
- Processing time varies by file size and complexity
- Failures are gracefully handled with detailed error messages

### Database Optimization
- Use select_related() and prefetch_related() for related data
- Consider pagination for large datasets
- Index frequently queried fields

## Troubleshooting

### Issue: AI Analysis Fails
**Solution:** Verify OpenAI API key is configured in Django settings:
```python
OPENAI_API_KEY = 'your-openai-api-key'
```

### Issue: File Upload Fails
**Solution:** Check file size and format restrictions. Ensure media directory is writable.

### Issue: Status Transition Error
**Solution:** Verify bill is in correct status for the operation:
- Draft → Analyze
- Analysed → Verify
- Verified → Sync

### Issue: Organization Not Found
**Solution:** Ensure organization UUID is correct and user has access permissions.

## Advanced Features

### PDF Splitting
When uploading with `file_type: "Multiple Invoice/File"`, PDFs are automatically split into individual pages, each creating a separate bill.

### Bulk Operations
Process multiple bills efficiently by:
1. Uploading multiple files at once
2. Using status-based filtering
3. Batch verification operations

### External System Integration
The sync functionality prepares data for external accounting systems like Tally ERP with proper ledger mappings and tax calculations.

## Security Considerations

### API Key Security
- Store API keys securely
- Use environment variables for sensitive configuration
- Implement proper key rotation policies

### File Security
- Validate file types and sizes
- Scan uploaded files for malware
- Implement proper access controls

### Data Privacy
- Ensure bill data is properly scoped to organizations
- Implement audit trails for data changes
- Follow data retention policies
