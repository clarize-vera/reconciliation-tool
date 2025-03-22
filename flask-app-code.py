import os
import io
import tempfile
from flask import Flask, render_template, request, redirect, url_for, session, send_file
import pandas as pd
import pdfplumber
import re
from fuzzywuzzy import process
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from flask_session import Session

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# OAuth 2.0 configuration
CLIENT_SECRETS_FILE = 'client_secret.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly', 
          'https://www.googleapis.com/auth/drive.file']
API_SERVICE_NAME = 'drive'
API_VERSION = 'v3'

def extract_transactions_from_pdfs(folder_path):
    transactions = []
    latest_date = None
    for filename in os.listdir(folder_path):
        if filename.endswith(".pdf"):
            pdf_path = os.path.join(folder_path, filename)
            with pdfplumber.open(pdf_path) as pdf:
                statement_year = None
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        lines = text.split('\n')
                        if i == 0:  # Extract year from first page
                            date_match = re.search(r'Statement Date\s+(\d{1,2} \w+ (\d{4}))', text)
                            if date_match:
                                statement_year = date_match.group(2)
                        for line in lines:
                            match = re.search(r'(\d{1,2} \w{3})\s+([\w\s\'\-#]+)\s+([A-Za-z0-9@.]+)?\s+([0-9,.]+)\s*(Cr)?', line)
                            if match and statement_year:
                                date, description, vendor, amount, credit_indicator = match.groups()
                                full_date = f"{date} {statement_year}"
                                amount_value = float(amount.replace(',', ''))
                                if credit_indicator:
                                    amount_value = abs(amount_value)  # Positive for credits
                                else:
                                    amount_value = -abs(amount_value)  # Negative for debits
                                transactions.append({
                                    'Transaction Date': full_date,
                                    'Transaction Details': description.strip(),
                                    'Amount': amount_value
                                })
                                # Update latest date
                                parsed_date = pd.to_datetime(full_date, format='%d %b %Y')
                                latest_date = max(latest_date, parsed_date) if latest_date else parsed_date
    return pd.DataFrame(transactions), latest_date

def reconcile_transactions(pdf_transactions, excel_transactions):
    pdf_transactions['Match'] = pdf_transactions['Amount'].apply(
        lambda amt: process.extractOne(str(amt), excel_transactions['Amount'].astype(str), score_cutoff=90))
    pdf_transactions['Matched Amount'] = pdf_transactions['Match'].apply(lambda x: x[0] if x else 'No Match')
    pdf_transactions['Match Score'] = pdf_transactions['Match'].apply(lambda x: x[1] if x else 0)
    pdf_transactions.drop(columns=['Match'], inplace=True)
    
    only_in_xero = excel_transactions[~excel_transactions['Amount'].astype(str).isin(pdf_transactions['Amount'].astype(str))]
    only_in_statement = pdf_transactions[~pdf_transactions['Amount'].astype(str).isin(excel_transactions['Amount'].astype(str))]
    
    return pdf_transactions, only_in_xero, only_in_statement

def get_drive_service():
    credentials = Credentials(**session['credentials'])
    return build(API_SERVICE_NAME, API_VERSION, credentials=credentials)

def download_file(service, file_id, local_path):
    request = service.files().get_media(fileId=file_id)
    with open(local_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()

def upload_file(service, file_path, filename, mime_type, folder_id=None):
    file_metadata = {'name': filename}
    if folder_id:
        file_metadata['parents'] = [folder_id]
    
    media = MediaFileUpload(file_path, mimetype=mime_type)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

@app.route('/')
def index():
    if 'credentials' not in session:
        return redirect('authorize')
    return render_template('index.html')

@app.route('/authorize')
def authorize():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session['state']
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    
    authorization_response = request.url
    flow.fetch_token(authorization_response=authorization_response)
    
    credentials = flow.credentials
    session['credentials'] = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }
    
    return redirect(url_for('index'))

@app.route('/browse_drive')
def browse_drive():
    if 'credentials' not in session:
        return redirect('authorize')
    
    service = get_drive_service()
    
    # Get folders and files
    query = "mimeType='application/vnd.google-apps.folder' or mimeType='application/pdf' or mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'"
    results = service.files().list(q=query, fields="files(id, name, mimeType, parents)").execute()
    items = results.get('files', [])
    
    return render_template('browse_drive.html', items=items)

@app.route('/reconcile', methods=['POST'])
def reconcile():
    if 'credentials' not in session:
        return redirect('authorize')
    
    pdf_folder_id = request.form.get('pdf_folder_id')
    excel_file_id = request.form.get('excel_file_id')
    
    service = get_drive_service()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Get PDF files in the selected folder
        query = f"'{pdf_folder_id}' in parents and mimeType='application/pdf'"
        pdf_files = service.files().list(q=query).execute().get('files', [])
        
        # Download PDF files
        pdf_folder_path = os.path.join(temp_dir, 'pdfs')
        os.makedirs(pdf_folder_path, exist_ok=True)
        for pdf_file in pdf_files:
            download_file(service, pdf_file['id'], os.path.join(pdf_folder_path, pdf_file['name']))
        
        # Download Excel file
        excel_path = os.path.join(temp_dir, 'transactions.xlsx')
        download_file(service, excel_file_id, excel_path)
        
        # Process files
        pdf_transactions, latest_date = extract_transactions_from_pdfs(pdf_folder_path)
        excel_transactions = pd.read_excel(excel_path)
        excel_transactions['Amount'] = excel_transactions['Amount'].astype(float)
        
        reconciled_df, only_in_xero, only_in_statement = reconcile_transactions(pdf_transactions, excel_transactions)
        
        # Calculate balance
        total_pdf = pdf_transactions['Amount'].sum()
        total_xero = excel_transactions['Amount'].sum()
        balance_difference = total_xero - total_pdf
        
        balance_df = pd.DataFrame({
            'Date': [latest_date.strftime('%d %b %Y')],
            'Description': ['Balance Difference'],
            'Amount': [balance_difference]
        })
        
        # Save results to Excel
        result_path = os.path.join(temp_dir, "Reconciliation_Result.xlsx")
        with pd.ExcelWriter(result_path) as writer:
            reconciled_df.to_excel(writer, sheet_name='Matched Transactions', index=False)
            only_in_xero.to_excel(writer, sheet_name='In Xero Only', index=False)
            only_in_statement.to_excel(writer, sheet_name='On Statement Only', index=False)
            balance_df.to_excel(writer, sheet_name='Balance Differences', index=False)
        
        extracted_path = os.path.join(temp_dir, "Extracted_Transactions.xlsx")
        pdf_transactions.to_excel(extracted_path, index=False)
        
        # Upload results back to Google Drive
        result_id = upload_file(service, result_path, "Reconciliation_Result.xlsx", 
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        extracted_id = upload_file(service, extracted_path, "Extracted_Transactions.xlsx", 
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        
        session['result_file_id'] = result_id
        session['extracted_file_id'] = extracted_id
        
    return redirect(url_for('results'))

@app.route('/results')
def results():
    result_file_id = session.get('result_file_id')
    extracted_file_id = session.get('extracted_file_id')
    return render_template('results.html', result_id=result_file_id, extracted_id=extracted_file_id)

@app.route('/download/<file_id>')
def download(file_id):
    if 'credentials' not in session:
        return redirect('authorize')
    
    service = get_drive_service()
    file = service.files().get(fileId=file_id).execute()
    file_name = file.get('name')
    
    temp_path = tempfile.mktemp()
    download_file(service, file_id, temp_path)
    
    return send_file(temp_path, as_attachment=True, download_name=file_name)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
