"""Small, synchronous diagnostics; never log HTTP bodies, cookies or headers."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import logging
import os
import sys
import time
from urllib.parse import urlsplit

LOGGER = logging.getLogger("visual_benchmark")


def positive(value):
    number = float(value)
    if not 0 < number <= 3600:
        raise argparse.ArgumentTypeError("必须大于0且不超过3600秒")
    return number


def add_arguments(parser, *, poll=True):
    parser.add_argument("--log-level", choices=("INFO", "DEBUG"), default=os.environ.get("BENCH_LOG_LEVEL", "INFO"))
    parser.add_argument("--heartbeat-interval", type=positive,
                        default=float(os.environ.get("BENCH_HEARTBEAT_INTERVAL", "15")))
    parser.add_argument("--http-timeout", type=positive, default=None,
                        help="单次生成平台 HTTP 请求超时；留空沿用各接口原有值")
    if poll:
        parser.add_argument("--poll-interval", type=positive, default=5,
                            help="生成阶段查询平台状态的间隔秒数")


def configure(args):
    LOGGER.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(message)s", "%Y-%m-%d %H:%M:%S"))
    LOGGER.addHandler(handler)
    LOGGER.propagate = False
    level = getattr(args, "log_level", "INFO")
    LOGGER.setLevel(level)
    os.environ["BENCH_LOG_LEVEL"] = level
    os.environ["BENCH_HEARTBEAT_INTERVAL"] = str(getattr(args, "heartbeat_interval", 15))
    if getattr(args, "http_timeout", None) is not None:
        os.environ["BENCH_HTTP_TIMEOUT_SECONDS"] = str(args.http_timeout)


def emit(event, *, debug=False, **fields):
    if not LOGGER.handlers:
        configure(argparse.Namespace(log_level=os.environ.get("BENCH_LOG_LEVEL", "INFO"),
                  heartbeat_interval=float(os.environ.get("BENCH_HEARTBEAT_INTERVAL", "15")), http_timeout=None))
    LOGGER.log(logging.DEBUG if debug else logging.INFO, "%s %s", event,
               " ".join(f"{key}={value}" for key, value in fields.items()))


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def http_timeout(default):
    return float(os.environ.get("BENCH_HTTP_TIMEOUT_SECONDS", default))


@contextmanager
def trace_http(method, url, timeout):
    address = urlsplit(url)
    # Strip userinfo, query and fragment, which can contain credentials.
    endpoint = f"{address.scheme}://{address.hostname}" + (f":{address.port}" if address.port else "") + address.path
    state = {}
    started = time.monotonic()
    emit("HTTP_START", debug=method == "GET", method=method, endpoint=endpoint, timeout_seconds=timeout)
    try:
        yield state
    except Exception as exc:
        emit("HTTP_FAILED", method=method, endpoint=endpoint, elapsed_seconds=round(time.monotonic() - started, 3),
             error=type(exc).__name__, status=getattr(exc, "code", "unknown"))
        raise
    else:
        emit("HTTP_END", debug=method == "GET", method=method, endpoint=endpoint,
             elapsed_seconds=round(time.monotonic() - started, 3), **state)
