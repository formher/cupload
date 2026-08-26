import logging
import logging.config
from flask import Flask
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
        from flask import request
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
