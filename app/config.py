import os

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    # Assuming app/ is the base, uploads is at root/uploads (../uploads) relative to app/
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or os.path.abspath(os.path.join(BASE_DIR, '../uploads'))
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-please-change'
    MAX_TTL_SECONDS = 604800  # 7 days
    MAX_DOWNLOADS = 100
    # Ceilings for the paths that must read a whole file into memory to
    # render it (the code/markdown viewer, the pretty printer) or to encrypt
    # it. Anything larger is streamed or rejected instead.
    # Browser-viewable uploads (code, markdown, images, PDFs) get a different
    # default: for these, *viewing is downloading* - the viewer's own ?raw=true
    # fetch consumes the counter - so a default of one download meant the link
    # was dead the moment anyone opened it, including the sender checking it.
    # Explicit X-TTL / X-Downloads headers still win.
    VIEWABLE_DEFAULT_TTL_SECONDS = 604800   # 7 days
    VIEWABLE_DEFAULT_DOWNLOADS = 100

    MAX_VIEWER_BYTES = 5 * 1024 * 1024      # 5 MB
    MAX_SECRET_BYTES = 1024 * 1024          # 1 MB
