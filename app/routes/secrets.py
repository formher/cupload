from flask import Blueprint, request, make_response, abort, current_app
import os
import uuid
import shutil
from cryptography.fernet import Fernet
from app.extensions import limiter, uploads_total, downloads_total, expiries_total
from app.utils import is_bot, resolve_upload_file

secrets_bp = Blueprint('secrets', __name__)

@secrets_bp.route('/secret', methods=['POST'])
@limiter.limit("10 per minute")
def create_secret():
    upload_folder = current_app.config['UPLOAD_FOLDER']

    # Secrets are encrypted in memory, so they get their own small ceiling
    # rather than the 500MB one that applies to file uploads.
    max_secret = current_app.config['MAX_SECRET_BYTES']
    if request.content_length and request.content_length > max_secret:
        return f"Secret too large. Max is {max_secret // 1024}KB.\n", 413

    # Read raw text or form data
    data = request.get_data()
    if not data:
        return "No content provided\n", 400
    if len(data) > max_secret:
        return f"Secret too large. Max is {max_secret // 1024}KB.\n", 413
        
    # Generate Key and encryption suite
    # We use Fernet (AES-128 CBC + HMAC) for simplicity and safety
    key = Fernet.generate_key() 
    f = Fernet(key)
    
    # Encrypt
    token = f.encrypt(data)
    
    # Store
    random_id = uuid.uuid4().hex[:24]  # 96 bits; longer ID for secrets
    dir_path = os.path.join(upload_folder, 'secrets', random_id)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, 'secret.enc')
    
    with open(file_path, 'wb') as file:
        file.write(token)
    
    uploads_total.labels(kind='secret').inc()
    current_app.logger.info(f"Secret created: {random_id} from {request.remote_addr}")
    
    # Return URL with Key (Key is URL-safe base64)
    # Fernet key is bytes, need to decode for URL
    key_str = key.decode('utf-8')
    
    return f"Secret Link (Burn after reading): https://qurl.sh/secret/{random_id}/{key_str}\n"

@secrets_bp.route('/secret/<random_id>/<key>', methods=['GET'])
def get_secret(random_id, key):
    # Reading a secret destroys it. A crawler or a chat client unfurling the
    # link would burn it before the intended recipient ever loaded the page,
    # and robots.txt alone does not stop the ones that ignore it.
    if is_bot(request.user_agent.string):
        current_app.logger.info(f"Bot refused secret: {random_id}")
        abort(404)

    try:
        upload_folder = current_app.config['UPLOAD_FOLDER']
        dir_path, file_path = resolve_upload_file(
            os.path.join(upload_folder, 'secrets'), random_id, 'secret.enc')

        if not file_path or not os.path.isfile(file_path):
            current_app.logger.warning(f"Secret not found (404): {random_id} from {request.remote_addr}")
            abort(404)
            
        # Decrypt
        try:
            f = Fernet(key.encode('utf-8'))
            with open(file_path, 'rb') as file:
                token = file.read()
            secret_data = f.decrypt(token)
        except Exception:
            current_app.logger.warning(f"Secret decryption failed: {random_id} from {request.remote_addr}")
            return "Invalid Key or Corrupt Data", 400
            
        # BURN IT
        try:
             shutil.rmtree(dir_path)
             expiries_total.labels(reason='secret_burned').inc()
             current_app.logger.info(f"Secret burned: {random_id} (Accessed by {request.remote_addr})")
        except Exception as e:
            current_app.logger.error(f"Failed to burn secret {random_id}: {e}")
            pass

        downloads_total.labels(mode='secret').inc()
        return make_response(secret_data, {'Content-Type': 'text/plain'})
        
    except Exception as e:
        current_app.logger.error(f"Secret error {random_id}: {e}")
        abort(404)
