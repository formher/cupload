import os

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    # Assuming app/ is the base, uploads is at root/uploads (../uploads) relative to app/
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or os.path.abspath(os.path.join(BASE_DIR, '../uploads'))
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-please-change'
