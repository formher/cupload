from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Counter, Histogram

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
    # Emit Retry-After and X-RateLimit-* on a 429. Crawlers use Retry-After to
    # reschedule instead of treating the block as a site-level failure.
    headers_enabled=True,
)

scheduler = BackgroundScheduler()

metrics = PrometheusMetrics.for_app_factory()

uploads_total = Counter(
    'cupload_uploads_total',
    'Total uploads',
    ['kind']  # file | secret | pretty
)
downloads_total = Counter(
    'cupload_downloads_total',
    'Total file downloads served',
    ['mode']  # raw | viewer
)
expiries_total = Counter(
    'cupload_expiries_total',
    'Total folders removed by expiry/cleanup',
    ['reason']  # ttl | downloads_exhausted | secret_burned | sweep
)
upload_bytes = Histogram(
    'cupload_upload_bytes',
    'Size of uploaded files in bytes',
    buckets=(
        1024,
        10*1024,
        100*1024,
        1024*1024,
        10*1024*1024,
        50*1024*1024,
        100*1024*1024,
        250*1024*1024,
        500*1024*1024,
    )
)
