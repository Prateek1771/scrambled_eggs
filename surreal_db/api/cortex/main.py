"""FastAPI application.

Three responsibilities:

  * `/viewer-token` mints the short-lived, read-only SurrealDB credential the
    browser uses to hold its **own** WebSocket to the database. The memory graph
    is pushed to the screen by SurrealDB directly -- it does not pass through this
    process. That is the entire demo, and this endpoint is the only reason the API
    is involved in it at all.
  * `/chat` runs the agent and streams its progress over SSE.
  * `/health` reports whether the database is reachable.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import ConfigError, Settings
from .db import Database, one, record_str
from .graph import build_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cortex")


class ViewerToken(BaseModel):
    """A browser-usable SurrealDB credential and the deadline for replacing it.

    `expires_at` is a wall-clock unix timestamp rather than a duration because
    the client refreshes *ahead* of expiry, and a duration would need re-anchoring
    to the moment of receipt on every hop.
    """

    token: str
    namespace: str
    database: str
    expires_at: float


class ChatRequest(BaseModel):
    """One turn of conversation.

    `session_id` scopes the memory; `thread_id` scopes the agent's checkpointed
    state. They are separate because a thread can be branched or replayed without
    forking the memory it wrote.
    """

    message: str
    session_id: str | None = None
    thread_id: str | None = None


def _decode_expiry(token: str) -> float:
    """Read the `exp` claim out of a JWT without verifying it.

    Verification is the database's job -- this process only needs to know when to
    hand out a new one. The payload is base64url with the padding stripped, which
    Python's decoder rejects, so it is added back.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def _event(name: str, data: dict) -> str:
    """Format one Server-Sent Event."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Takes settings as an argument so tests can construct an app without a real
    environment, and resolves them from os.environ otherwise.
    """
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open every connection the process needs, and prove each one works.

        Failing here stops the container. A process that starts happily and only
        discovers a broken database, a bad key or a misspelled model on the first
        request looks healthy to everything watching it, which is worse than not
        starting.
        """
        async with httpx.AsyncClient(base_url=settings.surreal_http, timeout=10.0) as client:
            app.state.http = client
            app.state.settings = settings

            try:
                (await client.get("/health")).raise_for_status()
            except httpx.HTTPError as error:
                raise RuntimeError(
                    f"cannot reach SurrealDB at {settings.surreal_http}: {error}"
                ) from error

            await settings.verify_models()

            database = Database(settings)
            await database.connect()
            app.state.db = database
            app.state.agent = build_agent(settings, database)

            logger.info("cortex ready: %s, %s / %s",
                        settings.surreal_http, settings.llm_model, settings.llm_model_fast)
            try:
                yield
            finally:
                await database.close()

    app = FastAPI(title="CORTEX", lifespan=lifespan)

    # The browser talks to two origins -- this API and the database -- so it
    # needs CORS here. The database's own CORS is permissive by default.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report whether the API can still reach the database."""
        try:
            (await app.state.http.get("/health")).raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(status_code=503, detail=f"database unreachable: {error}")
        return {"status": "ok"}

    @app.get("/viewer-token", response_model=ViewerToken)
    async def viewer_token() -> ViewerToken:
        """Mint a short-lived read-only SurrealDB token for the browser.

        Signs in as the database-level VIEWER user and hands the resulting JWT
        out. The viewer *password* never leaves this process; the browser only
        ever holds a token that expires, and that token can `SELECT` and
        `LIVE SELECT` and nothing else -- a write issued with it is silently
        discarded by the database rather than applied.

        This is the only credential the browser is ever given. `OPENAI_API_KEY`
        exists solely in this container's environment.
        """
        try:
            response = await app.state.http.post("/signin", json={
                "ns": settings.namespace,
                "db": settings.database,
                "user": settings.viewer_user,
                "pass": settings.viewer_pass,
            }, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HTTPException(status_code=503, detail=f"signin failed: {error}")

        token = response.json().get("token")
        if not token:
            raise HTTPException(status_code=503, detail="signin returned no token")

        try:
            expires_at = _decode_expiry(token)
        except (KeyError, ValueError, IndexError):
            # A token whose expiry cannot be read is still usable; the client just
            # needs *some* deadline to refresh against. An hour is SurrealDB's
            # default and erring short only costs an extra refresh.
            expires_at = time.time() + 3600.0

        return ViewerToken(token=token, namespace=settings.namespace,
                           database=settings.database, expires_at=expires_at)

    @app.post("/session")
    async def create_session(title: str | None = None) -> dict[str, str]:
        """Start a conversation and return the ids that identify it."""
        rows = await app.state.db.query("""
            LET $s = (CREATE ONLY session SET title = $title);
            RETURN $s.id;
        """, {"title": title or "chat"})
        return {"session_id": record_str(one(rows)), "thread_id": str(uuid.uuid4())}

    @app.post("/chat")
    async def chat(request: ChatRequest) -> StreamingResponse:
        """Run one turn and stream the agent's progress.

        **Stage events, not tokens.** `reason` returns the answer and the turn's
        extracted facts in a single structured-output call, and constrained
        decoding does not produce useful intermediate tokens -- so streaming
        characters would mean either a second uncached model call or faking it in
        the client.

        What is streamed instead is more informative for this particular product:
        each node reports what it actually did, so the user sees `recalled 6` and
        `wrote 2, superseded 1` as they happen. The memory operations are the
        subject here; the prose is the by-product.
        """
        session_id = request.session_id
        thread_id = request.thread_id or str(uuid.uuid4())

        if not session_id:
            rows = await app.state.db.query("""
                LET $s = (CREATE ONLY session SET title = "chat");
                RETURN $s.id;
            """)
            session_id = record_str(one(rows))

        async def stream() -> AsyncIterator[str]:
            """Drive the graph, emitting one SSE per completed node."""
            yield _event("start", {"session_id": session_id, "thread_id": thread_id})
            answer = ""
            try:
                async for update in app.state.agent.astream(
                    {"query": request.message, "session_id": session_id,
                     "thread_id": thread_id},
                    config={"configurable": {"thread_id": thread_id}},
                    stream_mode="updates",
                ):
                    for node, payload in update.items():
                        payload = payload or {}
                        if node == "recall":
                            yield _event("recalled", {
                                "count": len(payload.get("recalled") or []),
                                "hits": payload.get("recalled") or [],
                                "retrieval_id": (payload.get("retrieval_ids") or [""])[-1],
                            })
                        elif node == "reason":
                            answer = payload.get("answer", "")
                            yield _event("answer", {"text": answer})
                        elif node == "consolidate":
                            outcomes = payload.get("outcomes") or []
                            yield _event("written", {
                                "created": sum(1 for o in outcomes if o.get("action") == "create"),
                                "superseded": sum(1 for o in outcomes if o.get("action") == "supersede"),
                                "duplicates": sum(1 for o in outcomes if o.get("action") == "duplicate"),
                                "outcomes": outcomes,
                            })
                        else:
                            yield _event("stage", {"node": node})
            except Exception as error:  # noqa: BLE001 - the turn fails, the memory stands
                # Memory already committed is not rolled back by a failed
                # response. That is deliberate: the facts were true when written.
                logger.exception("turn failed")
                yield _event("error", {"message": str(error)})
                return

            yield _event("done", {"answer": answer})

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    return app


def main() -> FastAPI:
    """Entry point for uvicorn, translating a config error into a readable exit.

    Without this, a missing environment variable surfaces as a traceback inside
    uvicorn's import machinery, which buries the one line that matters.
    """
    try:
        return create_app()
    except ConfigError as error:
        raise SystemExit(f"cortex: {error}")


app = main()
