#!/bin/bash
# Quick setup script for Ollama OCR

echo "🚀 BillMunshi Ollama OCR Setup"
echo "================================"
echo ""

# Check if Ollama is installed
echo "1️⃣ Checking Ollama installation..."
if command -v ollama &> /dev/null; then
    echo "✅ Ollama is installed: $(ollama --version)"
else
    echo "❌ Ollama is not installed"
    echo "📥 Install from: https://ollama.ai"
    exit 1
fi

echo ""
echo "2️⃣ Checking if Ollama is running..."
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "✅ Ollama server is running"
else
    echo "⚠️  Ollama server is not running"
    echo "Starting Ollama..."
    ollama serve &
    sleep 3
fi

echo ""
echo "3️⃣ Checking for vision models..."
MODELS=$(ollama list 2>/dev/null | grep -i "llava\|bakllava")
if [ -n "$MODELS" ]; then
    echo "✅ Vision models found:"
    echo "$MODELS"
else
    echo "⚠️  No vision models found"
    echo "📥 Downloading llava:latest (recommended)..."
    ollama pull llava:latest
fi

echo ""
echo "4️⃣ Testing Ollama API..."
RESPONSE=$(curl -s http://localhost:11434/api/tags)
if [ $? -eq 0 ]; then
    echo "✅ Ollama API is accessible"
    echo "Available models:"
    echo "$RESPONSE" | grep -o '"name":"[^"]*"' | cut -d'"' -f4
else
    echo "❌ Ollama API is not accessible"
    exit 1
fi

echo ""
echo "5️⃣ Checking .env configuration..."
if [ -f ".env" ]; then
    if grep -q "OLLAMA_API_URL" .env; then
        echo "✅ Ollama configuration found in .env"
        echo ""
        echo "Current settings:"
        grep "OCR_" .env
        grep "OLLAMA_" .env
    else
        echo "⚠️  Ollama configuration missing in .env"
        echo "Adding default configuration..."
        cat >> .env << EOF

# ==== OCR Configuration ====
# Primary OCR engine: 'openai' or 'ollama' (default: openai)
OCR_PRIMARY_ENGINE=openai
# Enable automatic fallback from OpenAI to Ollama if OpenAI fails (default: true)
OCR_ENABLE_FALLBACK=true
# Ollama configuration
OLLAMA_API_URL=http://localhost:11434
OLLAMA_VISION_MODEL=llava:latest
EOF
        echo "✅ Configuration added to .env"
    fi
else
    echo "❌ .env file not found"
    exit 1
fi

echo ""
echo "================================"
echo "✅ Setup Complete!"
echo ""
echo "📋 Current Configuration:"
echo "  • Primary Engine: $(grep OCR_PRIMARY_ENGINE .env | cut -d'=' -f2)"
echo "  • Fallback Enabled: $(grep OCR_ENABLE_FALLBACK .env | cut -d'=' -f2)"
echo "  • Ollama Model: $(grep OLLAMA_VISION_MODEL .env | cut -d'=' -f2)"
echo ""
echo "🎯 Recommendations:"
echo "  1. Keep Ollama running: ollama serve"
echo "  2. Start Django: python manage.py runserver"
echo "  3. Start RQ worker: python manage.py rqworker default"
echo "  4. Upload a test bill to verify OCR works"
echo ""
echo "📖 For detailed configuration, see: docs/OLLAMA_OCR_SETUP.md"
echo ""
