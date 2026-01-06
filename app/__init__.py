import logging
import logging.config
from flask import Flask
from app.config import Config
from app.extensions import limiter, scheduler
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

    # Apply ProxyFix for Nginx
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

    return app
