#!/usr/bin/env python3
"""
Simple PDF OCR Test Script
Test PDF to image conversion and OpenAI analysis independently

Usage:
    python test_pdf_ocr.py <path_to_pdf> [openai_api_key]

If no API key is provided, it will only test PDF conversion.
"""

import os
import sys
import base64
import json
from io import BytesIO
from pathlib import Path

def test_pdf_conversion(pdf_path):
    """Convert PDF to image and return base64"""
    print(f"\n📄 Testing PDF: {pdf_path}")

    # Check if file exists
    if not os.path.exists(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        return None

    try:
        # Import required libraries
        from pdf2image import convert_from_bytes
        from PIL import Image, ImageEnhance
        print("✅ Required libraries imported successfully")
    except ImportError as e:
        print(f"❌ Missing library: {e}")
        print("💡 Install with: pip install pdf2image pillow")
        return None

    try:
        # Read PDF file
        with open(pdf_path, 'rb') as f:
            pdf_bytes = f.read()

        file_size = len(pdf_bytes)
        print(f"✅ PDF loaded: {file_size:,} bytes")

        # Validate PDF
        if not pdf_bytes.startswith(b'%PDF'):
            print("❌ Invalid PDF file")
            return None

        if file_size < 100:
            print("❌ PDF file too small (corrupted?)")
            return None

        print("✅ PDF validation passed")

        # Convert PDF to image
        print("🔄 Converting PDF to image...")
        page_images = convert_from_bytes(
            pdf_bytes,
            first_page=1,
            last_page=1,
            dpi=200,  # Good balance of quality vs speed
            fmt='jpeg'
        )

        if not page_images:
            print("❌ No images generated from PDF")
            return None

        image = page_images[0]
        print(f"✅ PDF converted successfully")
        print(f"   - Image size: {image.size}")
        print(f"   - Image mode: {image.mode}")

        # Optimize image for OCR
        print("🔄 Optimizing image for OCR...")

        # Convert to RGB if needed
        if image.mode != 'RGB':
            image = image.convert('RGB')

        # Enhance for better OCR
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.2)

        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(1.1)

        # Ensure minimum size
        width, height = image.size
        if width < 1000 or height < 1000:
            scale = max(1000 / width, 1000 / height)
            new_size = (int(width * scale), int(height * scale))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            print(f"   - Upscaled to: {new_size}")

        print("✅ Image optimization completed")

        # Convert to base64
        print("🔄 Converting to base64...")
        image_io = BytesIO()
        image.save(image_io, format='JPEG', quality=95)
        image_io.seek(0)
        image_base64 = base64.b64encode(image_io.read()).decode('utf-8')

        print(f"✅ Base64 conversion completed: {len(image_base64):,} characters")

        # Save test image for inspection
        output_path = pdf_path.replace('.pdf', '_test_output.jpg')
        image.save(output_path, 'JPEG', quality=95)
        print(f"💾 Test image saved: {output_path}")

        return image_base64

    except Exception as e:
        print(f"❌ PDF conversion failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def test_openai_analysis(image_base64, api_key):
    """Test OpenAI analysis with the converted image"""
    print(f"\n🤖 Testing OpenAI Analysis...")

    try:
        # Try to import OpenAI
        try:
            from openai import OpenAI
            print("✅ OpenAI library imported")
        except ImportError:
            print("❌ OpenAI library not found")
            print("💡 Install with: pip install openai")
            return False

        # Initialize client
        if not api_key:
            print("❌ No OpenAI API key provided")
            return False

        client = OpenAI(api_key=api_key)
        print("✅ OpenAI client initialized")

        # Enhanced prompt for Indian invoices
        enhanced_prompt = """
        Analyze this invoice/bill image carefully and extract ALL visible information in JSON format.
        This appears to be an Indian business invoice/bill. Look for:
        
        1. Invoice/Bill Number (may be labeled as Invoice No, Bill No, Receipt No, etc.)
        2. Dates (Invoice Date, Bill Date, Due Date - convert to YYYY-MM-DD format)
        3. Vendor/Company details in "from" section (name and address)
        4. Customer details in "to" section (name and address) 
        5. Line items with descriptions, quantities, and prices
        6. Tax amounts (IGST, CGST, SGST - look for percentages and amounts)
        7. Total amount (may include terms like "Total", "Grand Total", "Amount Payable")
        
        IMPORTANT RULES:
        - Extract EXACT text as it appears on the document
        - For numbers, remove currency symbols (₹, Rs.) and commas
        - If any field is not visible or unclear, use empty string "" or 0 for numbers
        - Look carefully at the entire document, including headers, footers, and margins
        - Pay special attention to tax sections which may be in tables or separate areas
        
        Return data in this JSON structure:
        {
            "invoiceNumber": "Invoice/Bill number as shown on document",
            "dateIssued": "Invoice/Bill date in YYYY-MM-DD format",
            "dueDate": "Due date in YYYY-MM-DD format if mentioned",
            "from": {
                "name": "Vendor/Company name",
                "address": "Vendor address"
            },
            "to": {
                "name": "Customer name", 
                "address": "Customer address"
            },
            "items": [
                {
                    "description": "Item/Service description",
                    "quantity": 0,
                    "price": 0
                }
            ],
            "total": 0,
            "igst": 0,
            "cgst": 0,
            "sgst": 0
        }
        """

        print("🔄 Sending request to OpenAI...")
        print(f"   - Model: gpt-4o")
        print(f"   - Image size: {len(image_base64):,} characters")

        response = client.chat.completions.create(
            model='gpt-4o',
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": enhanced_prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "high"
                        }
                    }
                ]
            }],
            max_tokens=2000,
            temperature=0.1
        )

        print("✅ OpenAI response received")

        # Check response
        if not response.choices or not response.choices[0].message.content:
            print("❌ Empty response from OpenAI")
            return False

        raw_content = response.choices[0].message.content.strip()
        print(f"📄 Raw response length: {len(raw_content)} characters")

        # Parse JSON
        try:
            json_data = json.loads(raw_content)
            print("✅ JSON parsing successful")

            # Pretty print the results
            print("\n" + "="*60)
            print("📊 EXTRACTED DATA:")
            print("="*60)
            print(json.dumps(json_data, indent=2, ensure_ascii=False))
            print("="*60)

            # Analyze extraction quality
            analyze_extraction_quality(json_data)

            return True

        except json.JSONDecodeError as e:
            print(f"❌ JSON parsing failed: {str(e)}")
            print(f"📄 Raw response: {raw_content[:500]}...")
            return False

    except Exception as e:
        print(f"❌ OpenAI analysis failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def analyze_extraction_quality(data):
    """Analyze the quality of extracted data"""
    print("\n🔍 EXTRACTION QUALITY ANALYSIS:")
    print("-" * 40)

    meaningful_fields = 0
    total_fields = 0

    # Check each field
    fields_to_check = [
        ("Invoice Number", data.get('invoiceNumber', '')),
        ("Date Issued", data.get('dateIssued', '')),
        ("Due Date", data.get('dueDate', '')),
        ("Vendor Name", data.get('from', {}).get('name', '')),
        ("Vendor Address", data.get('from', {}).get('address', '')),
        ("Customer Name", data.get('to', {}).get('name', '')),
        ("Customer Address", data.get('to', {}).get('address', '')),
        ("Total Amount", data.get('total', 0)),
        ("IGST", data.get('igst', 0)),
        ("CGST", data.get('cgst', 0)),
        ("SGST", data.get('sgst', 0)),
    ]

    for field_name, value in fields_to_check:
        total_fields += 1
        has_value = False

        if isinstance(value, str) and value.strip() and value.lower() not in ['', 'null', 'none', 'n/a']:
            has_value = True
            meaningful_fields += 1
        elif isinstance(value, (int, float)) and value > 0:
            has_value = True
            meaningful_fields += 1

        status = "✅" if has_value else "❌"
        print(f"{status} {field_name}: {value}")

    # Check items
    items = data.get('items', [])
    items_with_content = 0
    if isinstance(items, list):
        for i, item in enumerate(items):
            if isinstance(item, dict):
                desc = item.get('description', '').strip()
                if desc and desc.lower() not in ['', 'null', 'none', 'n/a']:
                    items_with_content += 1
                    print(f"✅ Item {i+1}: {desc}")
                else:
                    print(f"❌ Item {i+1}: No description")

    # Summary
    print("-" * 40)
    extraction_rate = (meaningful_fields / total_fields) * 100 if total_fields > 0 else 0
    print(f"📈 Extraction Rate: {meaningful_fields}/{total_fields} fields ({extraction_rate:.1f}%)")
    print(f"📦 Items Found: {items_with_content}/{len(items) if isinstance(items, list) else 0}")

    # Quality assessment
    if meaningful_fields >= 6:
        print("🎉 EXCELLENT: High-quality extraction")
    elif meaningful_fields >= 4:
        print("👍 GOOD: Decent extraction")
    elif meaningful_fields >= 2:
        print("⚠️  FAIR: Some data extracted")
    else:
        print("💔 POOR: Very little data extracted")

def main():
    """Main function"""
    print("🚀 PDF OCR Test Script")
    print("=" * 50)

    # Check arguments
    if len(sys.argv) < 2:
        print("❌ Usage: python test_pdf_ocr.py <pdf_path> [openai_api_key]")
        print("📝 Example: python test_pdf_ocr.py invoice.pdf sk-...")
        return

    pdf_path = sys.argv[1]
    api_key = sys.argv[2] if len(sys.argv) > 2 else None

    # Test PDF conversion
    print("🔧 STEP 1: PDF Conversion")
    image_base64 = test_pdf_conversion(pdf_path)

    if not image_base64:
        print("\n💥 PDF conversion failed. Cannot proceed.")
        return

    print("\n✅ PDF conversion successful!")

    # Test OpenAI analysis if API key provided
    if api_key:
        print("\n🔧 STEP 2: OpenAI Analysis")
        success = test_openai_analysis(image_base64, api_key)

        if success:
            print("\n🎉 SUCCESS: Complete PDF OCR pipeline working!")
        else:
            print("\n💥 OpenAI analysis failed.")
    else:
        print("\n⏭️  Skipping OpenAI analysis (no API key provided)")
        print("💡 To test OpenAI: python test_pdf_ocr.py invoice.pdf sk-your-api-key")

    print("\n" + "=" * 50)
    print("🏁 Test completed!")

if __name__ == "__main__":
    main()
