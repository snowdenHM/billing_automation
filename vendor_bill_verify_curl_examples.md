# Vendor Bill Verify API - Curl Examples

## Overview
The `vendor_bill_verify` view accepts a structured JSON payload that allows users to modify analyzed bill data before verification. The code has been updated to use TallyConfig for dynamic ledger selection.

## Endpoint
```
POST /api/organizations/{org_id}/tally/vendor-bills/verify/
```

## Headers Required
```
Content-Type: application/json
Authorization: Bearer YOUR_JWT_TOKEN
# OR
Authorization: Token YOUR_API_TOKEN
```

## Request Payload Structure

### Basic Curl Command
```bash
curl -X POST "http://localhost:8000/api/organizations/{org_id}/tally/vendor-bills/verify/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -d @vendor_bill_verify_payload.json
```

### Example 1: Complete Bill Verification Payload
```json
{
  "bill_id": "123e4567-e89b-12d3-a456-426614174000",
  "analyzed_data": {
    "vendor": {
      "master_id": "V001",
      "name": "ABC Technologies Pvt Ltd",
      "gst_in": "27AABCU9603R1ZX",
      "company": "ABC Tech"
    },
    "bill_details": {
      "bill_number": "INV-2024-001",
      "date": "15-03-2024",
      "total_amount": 11800.00,
      "company_id": "your_company_id"
    },
    "taxes": {
      "igst": {
        "amount": 1800.00,
        "ledger": "IGST @ 18%"
      },
      "cgst": {
        "amount": 0.00,
        "ledger": "No Tax Ledger"
      },
      "sgst": {
        "amount": 0.00,
        "ledger": "No Tax Ledger"
      }
    },
    "line_items": [
      {
        "id": "456e7890-e89b-12d3-a456-426614174001",
        "item_name": "Software License",
        "item_details": "Annual software license subscription",
        "tax_ledger": "IGST @ 18%",
        "price": 10000.00,
        "quantity": 1,
        "amount": 10000.00,
        "gst": "18%",
        "igst": 1800.00,
        "cgst": 0.00,
        "sgst": 0.00
      }
    ]
  }
}
```

### Example 2: CGST+SGST Bill Verification
```json
{
  "bill_id": "123e4567-e89b-12d3-a456-426614174000",
  "analyzed_data": {
    "vendor": {
      "master_id": "V002",
      "name": "Local Supplier Ltd",
      "gst_in": "27AABCU9603R1ZY",
      "company": "Local Supplier"
    },
    "bill_details": {
      "bill_number": "LS-2024-002",
      "date": "16-03-2024",
      "total_amount": 5900.00,
      "company_id": "your_company_id"
    },
    "taxes": {
      "igst": {
        "amount": 0.00,
        "ledger": "No Tax Ledger"
      },
      "cgst": {
        "amount": 450.00,
        "ledger": "CGST @ 9%"
      },
      "sgst": {
        "amount": 450.00,
        "ledger": "SGST @ 9%"
      }
    },
    "line_items": [
      {
        "item_name": "Office Supplies",
        "item_details": "Stationery and office materials",
        "tax_ledger": "CGST @ 9%",
        "price": 2500.00,
        "quantity": 2,
        "amount": 5000.00,
        "gst": "18%",
        "igst": 0.00,
        "cgst": 450.00,
        "sgst": 450.00
      }
    ]
  }
}
```

### Example 3: Multiple Line Items with Mixed GST
```json
{
  "bill_id": "123e4567-e89b-12d3-a456-426614174000",
  "analyzed_data": {
    "vendor": {
      "master_id": "V003",
      "name": "Multi Product Vendor",
      "gst_in": "27AABCU9603R1ZZ",
      "company": "Multi Vendor Corp"
    },
    "bill_details": {
      "bill_number": "MP-2024-003",
      "date": "17-03-2024",
      "total_amount": 15300.00,
      "company_id": "your_company_id"
    },
    "taxes": {
      "igst": {
        "amount": 2300.00,
        "ledger": "IGST Multiple Rates"
      },
      "cgst": {
        "amount": 0.00,
        "ledger": "No Tax Ledger"
      },
      "sgst": {
        "amount": 0.00,
        "ledger": "No Tax Ledger"
      }
    },
    "line_items": [
      {
        "item_name": "Product A",
        "item_details": "High value product with 18% GST",
        "tax_ledger": "IGST @ 18%",
        "price": 10000.00,
        "quantity": 1,
        "amount": 10000.00,
        "gst": "18%",
        "igst": 1800.00,
        "cgst": 0.00,
        "sgst": 0.00
      },
      {
        "item_name": "Product B",
        "item_details": "Low value product with 12% GST",
        "tax_ledger": "IGST @ 12%",
        "price": 2500.00,
        "quantity": 1,
        "amount": 2500.00,
        "gst": "12%",
        "igst": 300.00,
        "cgst": 0.00,
        "sgst": 0.00
      },
      {
        "item_name": "Product C",
        "item_details": "Essential item with 5% GST",
        "tax_ledger": "IGST @ 5%",
        "price": 400.00,
        "quantity": 1,
        "amount": 400.00,
        "gst": "5%",
        "igst": 20.00,
        "cgst": 0.00,
        "sgst": 0.00
      }
    ]
  }
}
```

## Complete Curl Commands

### 1. Using JWT Token
```bash
curl -X POST "http://localhost:8000/api/organizations/123e4567-e89b-12d3-a456-426614174000/tally/vendor-bills/verify/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..." \
  -d '{
    "bill_id": "123e4567-e89b-12d3-a456-426614174000",
    "analyzed_data": {
      "vendor": {
        "master_id": "V001",
        "name": "ABC Technologies Pvt Ltd",
        "gst_in": "27AABCU9603R1ZX",
        "company": "ABC Tech"
      },
      "bill_details": {
        "bill_number": "INV-2024-001",
        "date": "15-03-2024",
        "total_amount": 11800.00,
        "company_id": "your_company_id"
      },
      "taxes": {
        "igst": {
          "amount": 1800.00,
          "ledger": "IGST @ 18%"
        },
        "cgst": {
          "amount": 0.00,
          "ledger": "No Tax Ledger"
        },
        "sgst": {
          "amount": 0.00,
          "ledger": "No Tax Ledger"
        }
      },
      "line_items": [
        {
          "id": "456e7890-e89b-12d3-a456-426614174001",
          "item_name": "Software License",
          "item_details": "Annual software license subscription",
          "tax_ledger": "IGST @ 18%",
          "price": 10000.00,
          "quantity": 1,
          "amount": 10000.00,
          "gst": "18%",
          "igst": 1800.00,
          "cgst": 0.00,
          "sgst": 0.00
        }
      ]
    }
  }'
```

### 2. Using API Token
```bash
curl -X POST "http://localhost:8000/api/organizations/123e4567-e89b-12d3-a456-426614174000/tally/vendor-bills/verify/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b" \
  -d @vendor_bill_verify_payload.json
```

### 3. Using External JSON File
First create a file called `vendor_bill_verify_payload.json` with your JSON payload, then:

```bash
curl -X POST "http://localhost:8000/api/organizations/123e4567-e89b-12d3-a456-426614174000/tally/vendor-bills/verify/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -d @vendor_bill_verify_payload.json
```

## Expected Response

### Success Response (200 OK)
```json
{
  "message": "Bill verified successfully",
  "analyzed_data": {
    "vendor": {
      "master_id": "V001",
      "name": "ABC Technologies Pvt Ltd",
      "gst_in": "27AABCU9603R1ZX",
      "company": "ABC Tech"
    },
    "bill_details": {
      "bill_number": "INV-2024-001",
      "date": "15-03-2024",
      "total_amount": 11800.0,
      "company_id": "your_company_id"
    },
    "taxes": {
      "igst": {
        "amount": 1800.0,
        "ledger": "IGST @ 18%"
      },
      "cgst": {
        "amount": 0.0,
        "ledger": "No Tax Ledger"
      },
      "sgst": {
        "amount": 0.0,
        "ledger": "No Tax Ledger"
      }
    },
    "line_items": [
      {
        "id": "456e7890-e89b-12d3-a456-426614174001",
        "item_name": "Software License",
        "item_details": "Annual software license subscription",
        "tax_ledger": "IGST @ 18%",
        "price": 10000.0,
        "quantity": 1,
        "amount": 10000.0,
        "gst": "18%",
        "igst": 1800.0,
        "cgst": 0.0,
        "sgst": 0.0
      }
    ]
  }
}
```

### Error Responses

#### 400 Bad Request - Missing bill_id
```json
{
  "error": "bill_id is required"
}
```

#### 404 Not Found - Bill not found
```json
{
  "error": "Bill or analyzed data not found"
}
```

#### 400 Bad Request - Wrong status
```json
{
  "error": "Bill is not in analyzed status"
}
```

## Dynamic Ledger Selection

The updated code now uses TallyConfig to dynamically determine which parent ledgers to use for:

1. **Vendor Ledgers**: Uses `vendor_parents` from TallyConfig
2. **IGST Ledgers**: Uses `igst_parents` from TallyConfig  
3. **CGST Ledgers**: Uses `cgst_parents` from TallyConfig
4. **SGST Ledgers**: Uses `sgst_parents` from TallyConfig

If no TallyConfig is found for the organization, it falls back to default parent ledgers:
- Vendors: "Sundry Creditors"
- Taxes: "Duties & Taxes"

The system will automatically create new ledgers under the appropriate parent ledgers if they don't exist, following the TallyConfig mappings.

## Testing Steps

1. First, ensure you have a bill in "Analysed" status
2. Use the `vendor_bill_detail` endpoint to get the current analyzed data structure
3. Modify the data as needed in your verify payload
4. Send the verify request with the modified data
5. Check that the bill status changes to "Verified"
6. Verify that new ledgers are created under the correct parent ledgers based on your TallyConfig

## Notes

- The `bill_id` is required in the request payload
- The `analyzed_data` structure follows the same format as returned by the detail view
- Line items can include an `id` field to update existing products or omit it to create new ones
- Date format should be "DD-MM-YYYY"
- All monetary amounts should be numeric values
- The system will automatically determine GST type based on the tax amounts provided
