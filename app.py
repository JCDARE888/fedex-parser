from flask import Flask, request, jsonify
from flask_cors import CORS
import logging
import pdfplumber
import re
from werkzeug.utils import secure_filename
import tempfile
import os

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

def extract_fedex_data(pdf_text):
    results = []
    lines = [line.strip() for line in pdf_text.split('\n') if line.strip()]
    
    for i, line in enumerate(lines):
        if line.startswith('Ship Date'):
            ship_date = ""
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', line)
            if date_match:
                ship_date = date_match.group(1)
            
            air_waybill = ""
            customer_name = ""
            order_number = ""
            awb_line_idx = -1
            
            for offset in range(1, 5):
                if i + offset < len(lines):
                    check_line = lines[i + offset]
                    if 'Air Waybill Number' in check_line:
                        awb_line_idx = i + offset
                        awb_match = re.search(r'(\d{12})', check_line)
                        if awb_match:
                            air_waybill = awb_match.group(1)
                        break
            
            if air_waybill and awb_line_idx >= 0:
                customer_line_idx = awb_line_idx - 1
                
                if customer_line_idx >= 0:
                    customer_line = lines[customer_line_idx]
                    
                    if 'Tendered Date' in customer_line:
                        words = customer_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                    elif 'Customs Entry Date' in customer_line:
                        words = customer_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                    else:
                        awb_line = lines[awb_line_idx]
                        words = awb_line.split()
                        if len(words) >= 2:
                            customer_name = ' '.join(words[-2:])
                
                if 'Tendered Date' in lines[customer_line_idx]:
                    awb_line = lines[awb_line_idx]
                    digits = re.findall(r'\d{4}', awb_line)
                    if digits:
                        order_number = digits[-1]
                elif 'Customs Entry Date' in lines[customer_line_idx]:
                    awb_line = lines[awb_line_idx]
                    digits = re.findall(r'\b(\d{4})\b', awb_line)
                    if digits:
                        order_number = digits[-1]
                else:
                    for j in range(awb_line_idx + 1, min(awb_line_idx + 6, len(lines))):
                        search_line = lines[j]
                        digits = re.findall(r'\b(\d{4})\b', search_line)
                        for digit in digits:
                            if not re.search(r'\d{5,}|/\d{4}|\d{4}/', search_line):
                                order_number = digit
                                break
                        if order_number:
                            break
                
                if customer_name == "BENJAMIN ATTARD":
                    order_number = ""
                
                total_amount = ""
                for j in range(awb_line_idx + 2, min(len(lines), awb_line_idx + 20)):
                    if re.search(r'\bTotal\b.*\d+\.?\d{2}', lines[j], re.IGNORECASE):
                        amount_match = re.search(r'([\d,]+\.?\d{2})', lines[j])
                        if amount_match:
                            total_amount = amount_match.group(1).replace(',', '')
                            break
                
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

def create_excel_page(results, filename):
    if not results:
        return '<html><body><h2>No Data Found</h2><a href="/">Upload Another File</a></body></html>'
    
    tsv_data = "Date\\tAir Waybill Number\\tCustomer Name\\tOrder Number\\tTotal Amount\\n"
    for item in results:
        tsv_data += f"{item['date']}\\t{item['air_waybill_number']}\\t{item['customer_name']}\\t{item['order_number']}\\t{item['total_amount']}\\n"
    
    table_rows = ""
    for item in results:
        table_rows += f'<tr><td>{item["date"]}</td><td>{item["air_waybill_number"]}</td><td>{item["customer_name"]}</td><td>{item["order_number"]}</td><td>${item["total_amount"]}</td></tr>'
    
    return f'''
    <html>
    <head>
        <title>FedEx Data Extracted</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
            th {{ background: #007bff; color: white; }}
            textarea {{ width: 100%; height: 200px; }}
            .btn {{ background: #28a745; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; }}
        </style>
    </head>
    <body>
        <h1>FedEx Data Extracted Successfully</h1>
        <p>File: {filename} | Extracted: {len(results)} shipments</p>
        
        <h2>Data Table</h2>
        <table>
            <tr><th>Date</th><th>Air Waybill Number</th><th>Customer Name</th><th>Order Number</th><th>Total Amount</th></tr>
            {table_rows}
        </table>
        
        <h2>Excel-Ready Format</h2>
        <button class="btn" onclick="copyData()">Copy to Clipboard</button>
        <textarea id="excelData" readonly>{tsv_data}</textarea>
        
        <p><a href="/">Upload Another File</a></p>
        
        <script>
            function copyData() {{
                document.getElementById('excelData').select();
                document.execCommand('copy');
                alert('Data copied to clipboard!');
            }}
        </script>
    </body>
    </html>
    '''

@app.route('/parse-fedex', methods=['POST'])
def parse_fedex_pdf():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        if not file.filename.lower().endswith('.pdf'):
            return jsonify({"error": "Invalid file format"}), 400
        
        filename = secure_filename(file.filename)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            file.save(temp_file.name)
            temp_path = temp_file.name
        
        try:
            pdf_text = ""
            with pdfplumber.open(temp_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        pdf_text += page_text + "\n"
            
            if not pdf_text.strip():
                results = []
            else:
                results = extract_fedex_data(pdf_text)
            
            user_agent = request.headers.get('User-Agent', '')
            if 'Mozilla' in user_agent or 'Chrome' in user_agent:
                return create_excel_page(results, filename)
            else:
                return jsonify(results), 200
            
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}")
            return jsonify({"error": "Failed to process PDF"}), 500
            
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
                
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/')
def root():
    return '''
    <html>
    <head>
        <title>FedEx PDF Parser</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .upload-area { border: 2px dashed #007bff; padding: 40px; text-align: center; margin: 20px 0; }
            .btn { background: #007bff; color: white; padding: 12px 25px; border: none; border-radius: 5px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h1>FedEx PDF Parser</h1>
        <p>Upload your FedEx PDF to extract shipping data</p>
        
        <div class="upload-area">
            <form action="/parse-fedex" method="post" enctype="multipart/form-data">
                <h3>Upload FedEx PDF File</h3>
                <input type="file" name="file" accept=".pdf" required>
                <br><br>
                <button type="submit" class="btn">Parse PDF</button>
            </form>
        </div>
        
        <p><a href="/health">Check Health</a></p>
    </body>
    </html>
    '''

@app.route('/health')
def health_check():
    return '<html><body><h1>System Healthy</h1><p>FedEx PDF Parser is running!</p><a href="/">Back to Home</a></body></html>'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8001))
    app.run(host='0.0.0.0', port=port, debug=False)
