"""Per-lease prefix replay for Claude /v1/messages on a pooled lease.

The proxy app's token-mode handler keeps each Claude conversation's provider
prompt cache byte-stable across turns with per-session state: a
``SessionTrackerStore`` (the previous turn's original and forwarded messages,
and the provider-confirmed cached prefix) and a per-session
``CompressionCache`` (previously compressed tool results). Each turn it swaps
cached compressions in (``prepare_turn``), compresses, replays the previous
turn's forwarded prefix over any drift (``finalize_turn``), re-places the
client's breakpoints, and after the response records what was forwarded.

A pooled lease forwards /v1/messages itself, so it carries the same state,
built from the same classes and keyed the same way (``compute_session_id``
plus the cache-affinity fingerprint), but owned by one lease. Nothing here is
shared across leases, and the lease drops it when it ends. Memory is bounded:
at most ``max_sessions`` session ids per lease, least recently used evicted
first, each with the store's own lineage cap, session TTL and the
compression cache's entry cap.
"""

from __future__ import annotations

import copy
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from headroom.cache.compression_cache import CompressionCache
from headroom.cache.prefix_tracker import (
    PrefixCacheTracker,
    PrefixFreezeConfig,
    SessionTrackerStore,
    segment_fingerprint,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SESSIONS = 16
# The response is parsed for its usage and assistant turn only up to this
# size; a larger response leaves the previous turn's state in place, which the
# replay's own alignment checks then decline to use.
MAX_CAPTURED_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass
class ReplayTurn:
    """One request's view of its conversation's replay state."""

    session_id: str
    tracker: PrefixCacheTracker
    compression_cache: CompressionCache
    previous_original: list[dict[str, Any]]
    previous_forwarded: list[dict[str, Any]]
    frozen_message_count: int
    idle_seconds: float


class LeaseReplayState:
    """One lease's conversations: prefix trackers and compression caches."""

    def __init__(self, config: Any, *, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.trackers = SessionTrackerStore(
            default_config=PrefixFreezeConfig(
                enabled=config.prefix_freeze_enabled,
                session_ttl_seconds=config.prefix_freeze_session_ttl,
            )
        )
        self.compression_caches: OrderedDict[str, CompressionCache] = OrderedDict()
        self.max_sessions = max_sessions

    def begin(
        self,
        request: Any,
        model: str,
        body: dict[str, Any],
        client_messages: list[dict[str, Any]],
    ) -> ReplayTurn:
        """Resolve the request's conversation exactly as the app handler does."""

        system_prompt = body.get("system")
        session_messages = (
            [{"role": "system", "content": system_prompt}, *client_messages]
            if system_prompt is not None
            else client_messages
        )
        session_id = self.trackers.compute_session_id(request, model, session_messages)
        cache_affinity = segment_fingerprint(
            {
                "model": model,
                "tools": body.get("tools"),
                "tool_choice": body.get("tool_choice"),
                "thinking": body.get("thinking"),
                "output_config": body.get("output_config"),
            }
        )
        self._touch(session_id)
        tracker = self.trackers.resolve_tracker(
            session_id, "anthropic", messages=session_messages, cache_affinity=cache_affinity
        )
        return ReplayTurn(
            session_id=session_id,
            tracker=tracker,
            compression_cache=self.compression_caches[session_id],
            previous_original=tracker.get_last_original_messages(),
            previous_forwarded=tracker.get_last_forwarded_messages(),
            frozen_message_count=tracker.get_frozen_message_count(),
            idle_seconds=getattr(tracker, "_idle_seconds_at_fetch", 0.0),
        )

    def _touch(self, session_id: str) -> None:
        cache = self.compression_caches.get(session_id)
        if cache is not None:
            self.compression_caches.move_to_end(session_id)
            return
        while len(self.compression_caches) >= self.max_sessions:
            evicted, _ = self.compression_caches.popitem(last=False)
            self._drop_trackers(evicted)
        self.compression_caches[session_id] = CompressionCache()

    def _drop_trackers(self, session_id: str) -> None:
        # Every lineage of a session id is keyed "<id>" or "<id>\x00<suffix>"
        # (see SessionTrackerStore.resolve_tracker).
        store = self.trackers
        for key in [
            key
            for key in store._trackers
            if key == session_id or key.startswith(session_id + "\x00")
        ]:
            del store._trackers[key]
            store._lineage_affinities.pop(key, None)
        store._lineages.pop(session_id, None)

    @staticmethod
    def record_response(
        turn: ReplayTurn,
        *,
        forwarded: list[dict[str, Any]],
        original: list[dict[str, Any]],
        response: dict[str, Any] | None,
    ) -> None:
        """Record the turn as the app handler does after a successful response."""

        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        next_forwarded = copy.deepcopy(forwarded)
        next_original = copy.deepcopy(original)
        if isinstance(response, dict) and response.get("role") == "assistant":
            assistant = {"role": "assistant", "content": copy.deepcopy(response.get("content", ""))}
            next_forwarded.append(copy.deepcopy(assistant))
            next_original.append(assistant)
        turn.tracker.update_from_response(
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            messages=next_forwarded,
            original_messages=next_original,
        )

    def stats(self) -> dict[str, int]:
        return {
            "sessions": len(self.compression_caches),
            "trackers": self.trackers.active_sessions,
        }

    def clear(self) -> dict[str, int]:
        """Drop every conversation; returns what was held."""

        held = self.stats()
        for session_id in list(self.compression_caches):
            self._drop_trackers(session_id)
        self.compression_caches.clear()
        # Trackers whose session's cache was already evicted cannot remain,
        # but clear the store outright in case of a stray key.
        self.trackers._trackers.clear()
        self.trackers._lineages.clear()
        self.trackers._lineage_affinities.clear()
        return held


def parse_response(raw: bytes, content_type: str, proxy: Any) -> dict[str, Any] | None:
    """The upstream response as the app handler reconstructs it, or None."""

    try:
        if "text/event-stream" in content_type:
            parsed = proxy._parse_sse_to_response(raw.decode("utf-8"), "anthropic")
        else:
            parsed = json.loads(raw)
    except Exception:
        logger.debug("replay: upstream response not parseable", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None
