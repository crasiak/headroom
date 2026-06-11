"""Company-Bedrock (Tailscale aperture) passthrough handler.

The corelight aperture speaks the Bedrock invoke wire protocol:

* request bodies are Anthropic Messages JSON (model in the URL path, no
  top-level ``model``),
* ``POST /model/{id}/invoke`` returns ``application/json`` plus
  ``X-Amzn-Bedrock-*`` token headers,
* ``POST /model/{id}/invoke-with-response-stream`` returns AWS binary
  event-stream (``application/vnd.amazon.eventstream``) — NOT SSE.

We compress the *request* body (where the token savings live) via the existing
Anthropic compression pipeline and stream the *response* bytes back verbatim,
so the binary event-stream is never re-framed or corrupted. No auth header is
injected — the gateway authorizes by tailnet identity.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)

# RFC 7230 §6.1 hop-by-hop headers + host + content-length: never forwarded.
_HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class BedrockHandlerMixin:
    """Mixin providing the company-Bedrock aperture passthrough for HeadroomProxy."""

    async def handle_bedrock_invoke(
        self,
        request: "Request",
        model_id: str,
        stream: bool = False,
    ) -> "Response | StreamingResponse | JSONResponse":
        """Compress the request body, forward to the aperture, stream bytes back verbatim."""
        from fastapi.responses import JSONResponse, StreamingResponse

        target = getattr(self.config, "bedrock_base_url", None)
        if not target:
            return JSONResponse(
                status_code=501,
                content={
                    "error": {
                        "type": "not_configured",
                        "message": (
                            "Bedrock passthrough is not configured. Start the proxy "
                            "with --bedrock-base-url or set BEDROCK_TARGET_API_URL."
                        ),
                    }
                },
            )

        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError):
            body = None

        bypass = request.headers.get("x-headroom-bypass", "").lower() == "true"
        policy = getattr(self.config, "bedrock_compression", "aggressive")

        original_tokens = 0
        optimized_tokens = 0
        tokens_saved = 0
        outbound = raw

        should_compress = (
            body is not None
            and isinstance(body.get("messages"), list)
            and not bypass
            and self.config.optimize
            and policy != "off"
        )
        if should_compress:
            try:
                context_limit = self.anthropic_provider.get_context_limit(model_id)
                result = self.anthropic_pipeline.apply(
                    messages=body["messages"],
                    model=model_id,
                    model_limit=context_limit,
                )
                if result.messages is not None:
                    body["messages"] = result.messages
                    outbound = json.dumps(body).encode("utf-8")
                original_tokens = result.tokens_before
                optimized_tokens = result.tokens_after
                tokens_saved = max(0, original_tokens - optimized_tokens)
            except Exception as exc:  # never block the request on a compression error
                logger.warning("bedrock compression failed for %s: %r", model_id, exc)
                outbound = raw

        url = f"{target.rstrip('/')}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        outbound_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
        }

        assert self.http_client is not None, "http_client must be initialized before streaming"
        upstream_req = self.http_client.build_request(
            "POST", url, content=outbound, headers=outbound_headers
        )
        upstream = await self.http_client.send(upstream_req, stream=True)

        # Relay upstream headers verbatim (Content-Type drives event-stream vs
        # json on the client side; X-Amzn-Bedrock-* carry token accounting).
        # Drop content-encoding: httpx has already decoded the body it hands us.
        passthrough_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
        }

        # Request-side savings always; response-side token counts only on the
        # non-stream path (binary event-stream frames are not re-parsed in v1).
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        if not stream:
            output_tokens = _int_header(upstream.headers, "x-amzn-bedrock-output-token-count")
            cache_read = _int_header(upstream.headers, "x-amzn-bedrock-cache-read-input-token-count")
            cache_write = _int_header(
                upstream.headers, "x-amzn-bedrock-cache-write-input-token-count"
            )

        request_id = await self._next_request_id()
        await self._record_bedrock_outcome(
            request_id=request_id,
            model=model_id,
            original_tokens=original_tokens,
            optimized_tokens=optimized_tokens,
            tokens_saved=tokens_saved,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

        async def _body_stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _body_stream(),
            status_code=upstream.status_code,
            headers=passthrough_headers,
            media_type=upstream.headers.get("content-type"),
        )

    async def _record_bedrock_outcome(
        self,
        *,
        request_id: str,
        model: str,
        original_tokens: int,
        optimized_tokens: int,
        tokens_saved: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> None:
        """Best-effort request-outcome recording for the /stats dashboard."""
        from headroom.proxy.outcome import RequestOutcome

        try:
            await self._record_request_outcome(
                RequestOutcome(
                    request_id=request_id,
                    provider="bedrock",
                    model=model,
                    original_tokens=original_tokens,
                    optimized_tokens=optimized_tokens,
                    output_tokens=output_tokens,
                    tokens_saved=tokens_saved,
                    attempted_input_tokens=original_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
            )
        except Exception as exc:  # metrics must never break the request path
            logger.debug("bedrock outcome recording skipped: %r", exc)


def _int_header(headers, name: str) -> int:
    """Parse an integer header value, defaulting to 0 on absence/garbage."""
    try:
        return int(headers.get(name, 0) or 0)
    except (TypeError, ValueError):
        return 0
