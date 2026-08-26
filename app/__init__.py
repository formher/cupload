import logging
import logging.config
import secrets as secrets_module
from flask import Flask, g, request
from app.config import Config
from app.extensions import limiter, scheduler, metrics
from app.utils import cleanup_old_files

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Configure Logging
    logging.config.dictConfig({
        'version': 1,
        'formatters': {
            'default': {
                'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
            }
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'stream': 'ext://sys.stdout',
                'formatter': 'default'
            }
        },
        'root': {
            'level': 'INFO',
            'handlers': ['console']
        }
    })

    # Initialize Extensions
    limiter.init_app(app)
    metrics.init_app(app)
    
    # Scheduler needs explicit start, but we should clear existing jobs if re-init (rare in this pattern)
    if not scheduler.running:
        scheduler.start()
    
    # Re-add job to ensure it's registered
    # Note: APScheduler persistence is memory-only here, so restarting app restarts schedule
    if not scheduler.get_jobs():
        scheduler.add_job(
            func=cleanup_old_files, 
            trigger="interval", 
            hours=1, 
            args=[app.config['UPLOAD_FOLDER']]
        )

    # Register Blueprints
    from app.routes.misc import misc_bp
    from app.routes.files import files_bp
    from app.routes.secrets import secrets_bp

    app.register_blueprint(misc_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(secrets_bp)

    # Per-request nonce so inline <script> blocks can be allowed by name rather
    # than by opening the policy up with 'unsafe-inline'.
    @app.before_request
    def generate_csp_nonce():
        g.csp_nonce = secrets_module.token_urlsafe(16)

    @app.context_processor
    def inject_csp_nonce():
        return {'csp_nonce': getattr(g, 'csp_nonce', '')}

    # An SVG served as image/svg+xml is a *document*: open the link and any
    # <script> inside it runs on this origin. `sandbox` drops it into an opaque
    # origin with scripting disabled, which is the fix.
    #
    # It is applied only to types a browser will actually parse as a scriptable
    # document. `sandbox` with no tokens disables scripting outright, which also
    # stops Chrome's built-in PDF viewer from rendering at all - a PDF embedded
    # by viewer.html came out blank. PDFs do not need it: PDF JavaScript runs
    # inside the browser's own PDF sandbox and cannot reach this origin's DOM.
    SANDBOXED_TYPES = {
        'image/svg+xml',
        'text/html',
        'application/xhtml+xml',
        'text/xml',
        'application/xml',
    }
    SANDBOXED_CSP = "default-src 'none'; sandbox; frame-ancestors 'self'"
    # frame-ancestors 'self' rather than 'none' because viewer.html embeds PDFs
    # from this same origin via <embed src="?raw=true">. No other directive here,
    # so nothing can interfere with the browser rendering the bytes.
    USER_CONTENT_CSP = "frame-ancestors 'self'"

    PAGE_CSP_TEMPLATE = (
        "default-src 'self'; "
        "script-src 'self' 'nonce-{nonce}' https://www.googletagmanager.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://www.google-analytics.com; "
        "connect-src 'self' https://www.google-analytics.com https://*.google-analytics.com "
        "https://www.googletagmanager.com; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    @app.after_request
    def set_security_headers(response):
        # Keyed on what is actually being returned, not on the route: serve_file
        # hands back both our viewer HTML and raw user bytes.
        if response.mimetype == 'text/html':
            response.headers['Content-Security-Policy'] = PAGE_CSP_TEMPLATE.format(
                nonce=getattr(g, 'csp_nonce', ''))
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        else:
            response.headers['Content-Security-Policy'] = (
                SANDBOXED_CSP if response.mimetype in SANDBOXED_TYPES
                else USER_CONTENT_CSP
            )
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            # A download link is itself the credential. Never send it onward.
            response.headers['Referrer-Policy'] = 'no-referrer'

        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Permissions-Policy'] = (
            'accelerometer=(), autoplay=(), camera=(), display-capture=(), '
            'encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), '
            'magnetometer=(), microphone=(), midi=(), payment=(), '
            'picture-in-picture=(), usb=(), xr-spatial-tracking=()'
        )
        # HSTS is also set by nginx, which is the TLS terminator; setting it here
        # too means it survives a proxy config change. Browsers ignore it on
        # plain HTTP, so it is harmless in local development.
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    # Only the marketing and documentation surface may be indexed. Everything
    # else — uploaded files, QR codes, pretty-printed documents, secrets, the
    # password prompt and every error page — gets noindex.
    #
    # This is an allowlist on purpose: a route added later is non-indexable by
    # default instead of leaking until somebody remembers to update a pattern.
    # It also has to be a header rather than a <meta> tag, because most of what
    # is served here (.log, .json, .sql, .pdf) is not HTML at all.
    PUBLIC_ENDPOINTS = {
        'misc.index',
        'misc.docs',
        'misc.transfer_sh_alternative',
        'misc.robots_txt',
        'misc.sitemap_xml',
        'misc.llms_txt',
        'misc.llms_full_txt',
        'misc.openapi_json',
        'static',
    }

    @app.after_request
    def set_robots_tag(response):
        if request.endpoint not in PUBLIC_ENDPOINTS:
            response.headers['X-Robots-Tag'] = (
                'noindex, nofollow, noarchive, nosnippet, noimageindex'
            )
        return response

    # Apply ProxyFix for Nginx
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

    return app
