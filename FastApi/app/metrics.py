import time
import os
import psutil
import logging
import uuid
from fastapi import Request, APIRouter, Response
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# --- ENHANCED LOGGING SETUP ---
logger = logging.getLogger("api.metrics")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    # Adding more context to the format: timestamp, level, and the specific logger name
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

router = APIRouter()

# --- THE FOUR GOLDEN SIGNALS (Prometheus) ---
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests (Traffic & Errors)",
    ["method", "endpoint", "status"],
)

# Custom buckets for slow transcription APIs (units in seconds)
LATENCY_BUCKETS = (0.5, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0, float("inf"))
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency (Latency)",
    ["method", "endpoint"],
    buckets=LATENCY_BUCKETS,
)

SYSTEM_CPU_USAGE = Gauge("system_cpu_usage_percent", "System CPU usage (Saturation)")
SYSTEM_MEMORY_USAGE = Gauge("system_memory_usage_bytes", "System memory usage (Saturation)")


# --- UPDATED MIDDLEWARE WITH DETAILED LOGGING ---
async def metrics_middleware(request: Request, call_next):
    """Record request metrics without hiding the original application error.

    Important fix:
    - status is initialized before call_next(). Starlette/AnyIO can raise an
      exception group that may bypass the normal response path, so status must
      exist before the finally block updates Prometheus/logs.
    - use plain `raise` so the real downstream route error is preserved.
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    method = request.method
    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"

    status = "500"
    response = None

    try:
        response = await call_next(request)
        status = str(getattr(response, "status_code", 500))
        return response
    except BaseException as exc:
        # Catch BaseException only to log/record metrics for exception groups too.
        # Re-raise the original exception unchanged so the real route error is visible.
        logger.exception(
            "ID=%s | IP=%s | ERR | %s %s | Error: %r",
            request_id,
            client_ip,
            method,
            path,
            exc,
        )
        raise
    finally:
        duration = time.perf_counter() - start_time

        # Prometheus update must never crash the request path.
        try:
            HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=path, status=status).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, endpoint=path).observe(duration)
        except Exception as metrics_exc:
            logger.error(
                "ID=%s | METRICS_ERR | %s %s | Error: %r",
                request_id,
                method,
                path,
                metrics_exc,
            )

        log_msg = (
            f"ID={request_id} | IP={client_ip} | {method} {path} | "
            f"STATUS={status} | TIME={duration:.4f}s"
        )

        try:
            status_code = int(status)
        except Exception:
            status_code = 500

        if status_code >= 500:
            logger.error(log_msg)
        elif duration > 5.0:
            logger.warning(f"{log_msg} | SLOW_DETECTED")
        else:
            logger.info(log_msg)


@router.get("/metrics")
def metrics():
    SYSTEM_CPU_USAGE.set(psutil.cpu_percent())
    SYSTEM_MEMORY_USAGE.set(psutil.virtual_memory().used)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
