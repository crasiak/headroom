import gc
import json
import logging
import os
import time
import tracemalloc
from headroom.transport.diagnostics import Diagnostics, log_event, logger

# Isolate actual diagnostic encoding and synchronous local-file writes from
# provider/compression latency; no external calls, payloads or production logs.
fd = os.open('/tmp/headroom-diagnostics-benchmark.jsonl', os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
stream = os.fdopen(fd, 'w')
handler = logging.StreamHandler(stream)
logger.handlers = [handler]
logger.propagate = False
count = 10000
baseline_start = time.perf_counter()
for _ in range(count):
    pass
baseline = time.perf_counter() - baseline_start
d = Diagnostics(run_id='benchmark', lease_id='benchmark', sink=log_event)
gc.collect()
tracemalloc.start()
start = time.perf_counter()
for _ in range(count):
    record = d.begin('anthropic_messages')
    for stage in ('validating', 'upstream_connect', 'awaiting_headers', 'streaming'):
        record.stage(stage)
    record.finish('completed')
elapsed = time.perf_counter() - start
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
stream.close()
size = os.stat('/tmp/headroom-diagnostics-benchmark.jsonl').st_size
print(json.dumps({'requests': count, 'events_per_request': 6,
  'disabled_loop_us_per_request': baseline / count * 1e6,
  'enabled_encoding_flush_us_per_request_with_tracemalloc': elapsed / count * 1e6,
  'retained_bytes': current, 'peak_bytes': peak,
  'log_bytes_per_request': size/count, 'active_at_end':d.snapshot()['active_requests']}, indent=2))
os.unlink('/tmp/headroom-diagnostics-benchmark.jsonl')
