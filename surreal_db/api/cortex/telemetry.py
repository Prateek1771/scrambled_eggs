"""OpenTelemetry, with the spans stored in SurrealDB.

The header of this application renders `1 database (vs 5)`. Standing up a
separate trace store to observe it would quietly contradict the only claim the
project makes, so spans go into the same engine as everything else — and because
the browser already subscribes to that engine, a turn can be watched tracing
itself live, in the same UI as the memory it is building.

OpenTelemetry is a specification rather than a stack, so this costs no new
container. Attributes follow the GenAI semantic conventions, which means any OTLP
backend — Langfuse included — understands the output without a second
integration. Set `OTEL_EXPORTER_OTLP_ENDPOINT` and spans go there as well.

**The exporter never touches `Database`.** It writes over plain HTTP with the
standard library. Exporting a span through the instrumented client would produce
a write, which would produce a span, which would produce a write: the recursion
is the obvious way to build this and it does not terminate.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from .config import Settings

logger = logging.getLogger("cortex.telemetry")

# GenAI semantic convention attribute names, spelled once. Using the standard
# names is what lets a stock OTLP backend chart token spend without being told
# anything about CORTEX.
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_MODEL = "gen_ai.request.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


def tracer() -> trace.Tracer:
    """The tracer every module here uses.

    Fetched per call rather than cached at import: a module imported before
    `setup_telemetry` runs would otherwise hold a no-op tracer forever, and its
    spans would vanish with no error anywhere.
    """
    return trace.get_tracer("cortex")


class SurrealSpanExporter(SpanExporter):
    """Write finished spans into the `span` table over plain HTTP.

    Synchronous and standard-library only, on purpose. `BatchSpanProcessor` calls
    exporters from its own thread, so an async client would need bridging into a
    loop it does not belong to — and going through `Database` would make every
    export generate the span that triggers the next export.

    A failed export is logged and swallowed. Losing telemetry is acceptable;
    failing a user's turn because the telemetry could not be written is not.
    """

    def __init__(self, settings: Settings) -> None:
        self._url = f"{settings.surreal_http}/sql"
        credentials = f"{settings.root_user}:{settings.root_pass}".encode()
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {base64.b64encode(credentials).decode()}",
            "surreal-ns": settings.namespace,
            "surreal-db": settings.database,
        }

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Persist a batch of spans as `span` records."""
        rows = [self._row(span) for span in spans]
        if not rows:
            return SpanExportResult.SUCCESS

        statement = (
            f"LET $rows = {json.dumps(rows)};\n"
            "FOR $s IN $rows {\n"
            "    CREATE span SET trace_id = $s.trace_id, span_id = $s.span_id,\n"
            "        parent_span_id = $s.parent_span_id, name = $s.name,\n"
            "        started_at = type::datetime($s.started_at),\n"
            "        duration_ms = $s.duration_ms, status = $s.status,\n"
            "        attributes = $s.attributes;\n"
            "};"
        )

        request = urllib.request.Request(self._url, data=statement.encode(),
                                         headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                envelopes = json.load(response)
            failed = [e for e in envelopes if e.get("status") != "OK"]
            if failed:
                logger.warning("span export rejected: %s", failed[0].get("result"))
                return SpanExportResult.FAILURE
        except (urllib.error.URLError, OSError, ValueError) as error:
            logger.warning("span export failed: %s", error)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    @staticmethod
    def _row(span: ReadableSpan) -> dict[str, Any]:
        """Flatten one span into a record the schema accepts.

        Ids are rendered as hex strings rather than integers: they are 128 and 64
        bits, and JSON numbers are doubles, so anything else silently loses
        precision and breaks the parent links.
        """
        context = span.get_span_context()
        started = span.start_time or 0
        ended = span.end_time or started

        return {
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
            "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
            "name": span.name,
            # Nanoseconds since the epoch, as an RFC 3339 instant.
            "started_at": _rfc3339(started),
            "duration_ms": round((ended - started) / 1e6, 3),
            "status": span.status.status_code.name if span.status else "UNSET",
            "attributes": {key: _plain(value) for key, value in (span.attributes or {}).items()},
        }

    def shutdown(self) -> None:
        """Nothing to release: each export opens and closes its own connection."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Exports are synchronous, so there is never anything buffered here."""
        return True


def _rfc3339(nanoseconds: int) -> str:
    """Render epoch nanoseconds as an instant SurrealDB will accept."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(nanoseconds / 1e9, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _plain(value: Any) -> Any:
    """Coerce an attribute value into something JSON and SurrealDB both accept."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def setup_telemetry(settings: Settings) -> TracerProvider | None:
    """Install the tracer provider, or do nothing if telemetry is switched off.

    Returns the provider so the caller can shut it down cleanly -- an abrupt exit
    drops whatever is still batched, which is how the last few spans of a crashed
    run go missing exactly when they are most wanted.
    """
    if os.environ.get("OTEL_ENABLED", "true").lower() in ("false", "0", "no"):
        logger.info("telemetry disabled (OTEL_ENABLED)")
        return None

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "cortex-api"),
        "service.version": "0.1.0",
    }))

    provider.add_span_processor(BatchSpanProcessor(SurrealSpanExporter(settings)))
    exporters = ["surrealdb"]

    # Any OTLP backend, Langfuse included, is an extra exporter rather than a
    # second integration -- the instrumentation above does not change.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            exporters.append(endpoint)
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not "
                "installed; add opentelemetry-exporter-otlp-proto-http")

    trace.set_tracer_provider(provider)
    logger.info("telemetry exporting to: %s", ", ".join(exporters))
    return provider
