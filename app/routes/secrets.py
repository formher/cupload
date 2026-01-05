from flask import Blueprint, request, make_response, abort, current_app
import os
import uuid
import shutil
from cryptography.fernet import Fernet
from app.extensions import limiter

secrets_bp = Blueprint('secrets', __name__)

@secrets_bp.route('/secret', methods=['POST'])
@limiter.limit("10 per minute")
def create_secret():
    upload_folder = current_app.config['UPLOAD_FOLDER']

    # Read raw text or form data
    data = request.get_data()
    if not data:
        return "No content provided\n", 400
        
    # Generate Key and encryption suite
    # We use Fernet (AES-128 CBC + HMAC) for simplicity and safety
    key = Fernet.generate_key() 
    f = Fernet(key)
    
    # Encrypt
    token = f.encrypt(data)
    
    # Store
    random_id = str(uuid.uuid4())[:12] # Longer ID for secrets
    dir_path = os.path.join(upload_folder, 'secrets', random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, 'secret.enc')
    
    with open(file_path, 'wb') as file:
        file.write(token)
    
    # Return URL with Key (Key is URL-safe base64)
    # Fernet key is bytes, need to decode for URL
    key_str = key.decode('utf-8')
    
    return f"Secret Link (Burn after reading): https://qurl.sh/secret/{random_id}/{key_str}\n"

@secrets_bp.route('/secret/<random_id>/<key>', methods=['GET'])
def get_secret(random_id, key):
    try:
        upload_folder = current_app.config['UPLOAD_FOLDER']
        dir_path = os.path.join(upload_folder, 'secrets', random_id)
        file_path = os.path.join(dir_path, 'secret.enc')
        
        if not os.path.exists(file_path):
            abort(404)
            
        # Decrypt
        try:
            f = Fernet(key.encode('utf-8'))
            with open(file_path, 'rb') as file:
                token = file.read()
            secret_data = f.decrypt(token)
        except Exception:
            return "Invalid Key or Corrupt Data", 400
            
        # BURN IT
        try:
             shutil.rmtree(dir_path)
             # Logging needed here
        except Exception as e:
            # Logging needed here
            pass
            
        return make_response(secret_data, {'Content-Type': 'text/plain'})
        
    except Exception as e:
        # Logging needed here
        abort(404)
