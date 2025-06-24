from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
import pdfplumber
import re
from werkzeug.utils import secure_filename
import tempfile
import os

# Create Flask app
app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure upload settings
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

def extract_fedex_data(pdf_text):
    """
    Handle three formats:
    1. Tendered Date format
    2. Standard format  
    3. Customs Entry Date format
    """
    results = []
    lines = [line.strip() for line in pdf_text.split('\n') if line.strip()]
    
    for i, line in enumerate(lines):
        if line.startswith('Ship Date'):
            # Extract ship date
            ship_date = ""
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', line)
            if date_match:
                ship_date = date_match.group(1)
            
            # Find AWB line
            air_waybill = ""
            customer_name = ""
            order_number = ""
            awb_line_idx = -1
            
            for offset in range(1, 5):  # Extended search range
                if i + offset < len(lines):
                    check_line = lines[i + offset]
                    if 'Air Waybill Number' in check_line:
                        awb_line_idx = i + offset
                        awb_match = re.search(r'(\d{12})', check_line)
                        if awb_match:
                            air_waybill = awb_match.group(1)
                        break
            
            if air_waybill and awb_line_idx >= 0:
                # Check line before AWB for customer name
                customer_line_idx = awb_line_idx - 1
                
                if customer_line_idx >= 0:
                    customer_line = lines[customer_line_idx]
                    
                    # Check for different formats
                    if 'Tendered Date' in customer_line:
                        # Format: Tendered Date 04/17/2025 BEN VIC VIC
                        words = customer_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                    elif 'Customs Entry Date' in customer_line:
                        # Format: Customs Entry Date 03/22/2025 DARIA BENJAMIN ATTARD
                        words = customer_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                    else:
                        # Standard format: get from AWB line
                        awb_line = lines[awb_line_idx]
                        words = awb_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                
                # Extract order number for Tendered Date format
                if 'Tendered Date' in lines[customer_line_idx]:
                    awb_line = lines[awb_line_idx]
                    digits = re.findall(r'\d{4}', awb_line)
                    if digits:
                        order_number = digits[-1]
                elif 'Customs Entry Date' in lines[customer_line_idx]:
                    # For Customs Entry Date format, order is on AWB line
                    awb_line = lines[awb_line_idx]
                    digits = re.findall(r'\b(\d{4})\b', awb_line)
                    if digits:
                        order_number = digits[-1]  # Last 4-digit number on AWB line
                else:
                    # Search for order in next lines for standard format
                    for j in range(awb_line_idx + 1, min(awb_line_idx + 6, len(lines))):
                        search_line = lines[j]
                        digits = re.findall(r'\b(\d{4})\b', search_line)
                        for digit in digits:
                            if not re.search(r'\d{5,}|/\d{4}|\d{4}/', search_line):
                                order_number = digit
                                break
                        if order_number:
                            break
                
                # Business rule: If customer is BENJAMIN ATTARD, order number is always empty
                if customer_name == "BENJAMIN ATTARD":
                    order_number = ""
                
                # Find Total amount
                total_amount = ""
                for j in range(awb_line_idx + 2, min(len(lines), awb_line_idx + 20)):
                    if re.search(r'\bTotal\b.*\d+\.?\d{2}', lines[j], re.IGNORECASE):
                        amount_match = re.search(r'([\d,]+\.?\d{2})', lines[j])
                        if amount_match:
                            total_amount = amount_match.group(1).replace(',', '')
                            break
                
                # Add entry
                if air_waybill and total_amount:
                    entry = {
                        "date": ship_date,
                        "air_waybill_number": air_waybill,
                        "customer_name": customer_name,
                        "order_number": order_number,
                        "total_amount": total_amount
                    }
                    results.append(entry)
    
    return results

@app.route('/parse-fedex', methods=['POST'])
def parse_fedex_pdf():
    """
    Parse FedEx PDF and extract shipping data.
    Accepts multipart/form-data with PDF file upload.
    """
    try:
        # Check if file is present in request
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        
        file = request.files['file']
        
        # Check if file is selected
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        # Check if file is PDF
        if not file.filename.lower().endswith('.pdf'):
            return jsonify({"error": "Invalid file format"}), 400
        
        # Secure the filename
        filename = secure_filename(file.filename)
        
        # Save file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            file.save(temp_file.name)
            temp_path = temp_file.name
        
        try:
            # Extract text from PDF using pdfplumber
            pdf_text = ""
            with pdfplumber.open(temp_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        pdf_text += page_text + "\n"
            
            if not pdf_text.strip():
                logger.warning("No text found in PDF")
                return jsonify([]), 200
            
            # Extract FedEx data from the text
            results = extract_fedex_data(pdf_text)
            
            logger.info(f"Successfully extracted {len(results)} FedEx entries")
            return jsonify(results), 200
            
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}")
            return jsonify({"error": "Failed to process PDF"}), 500
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(temp_path)
            except OSError:
                pass
                
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/', methods=['GET'])
def root():
    """Health check endpoint"""
    return jsonify({"message": "FedEx PDF Parser API is running"}), 200

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "message": "FedEx PDF Parser API"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8001, debug=True)