"""The agent: a five-node LangGraph state machine.

Deliberately small. All the interesting behaviour lives in the queries, not in
the orchestration -- if this file were complicated it would mean the memory
design had failed to carry its weight.

    perceive -> recall -> reason -> consolidate -> respond

`reason` may loop back to `recall` at most twice. Associative recall gets much
better on a second pass once the model knows what it is looking for, but an
uncapped loop is how demos hang.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.graph import END, START, StateGraph

from .checkpointer import SurrealCheckpointSaver
from .config import Settings
from .consolidate import consolidate
from .db import Database, one, record_str
from .models import CortexState, Extracted, ReasonOutput
from .retrieval import recall
from .telemetry import (
    GEN_AI_INPUT_TOKENS, GEN_AI_MODEL, GEN_AI_OUTPUT_TOKENS, GEN_AI_SYSTEM, tracer,
)
from .tools import build_tools

logger = logging.getLogger("cortex.graph")

MAX_LOOPS = 2

SYSTEM_PROMPT = """You are CORTEX. Your memory is a database, and everything you \
remember is visible to the user as a graph while you talk.

You are given the facts recalled for this turn, each tagged with how it was found:
  vector - semantically similar
  text   - matched an exact word or identifier
  graph  - reached by association, not by similarity

Answer using what you were given. If the recalled facts do not answer the \
question, say so rather than inventing something -- a wrong fact becomes a node \
on screen and gets remembered.

Then extract what is worth remembering from what the USER said. Rules:
  - One statement per fact. "Ana moved to Berlin and joined Globex" is two facts. \
A fact that bundles two claims can never be individually corrected later.
  - Write facts in the third person, self-contained, so they still make sense \
read alone in a year.
  - Extract only what the user asserted, never what you inferred or replied.
  - Nothing worth remembering is a perfectly good answer. Return an empty list.
  - Name the entities each fact is about, and any relation between them.
"""


def build_agent(settings: Settings, database: Database):
    """Wire the models, tools and graph together, and compile it.

    Returns the compiled graph. Both chat models and the embedding client are
    constructed once here rather than per request -- they hold connection pools,
    and rebuilding them per turn is a slow way to achieve nothing.
    """
    llm = ChatOpenAI(model=settings.llm_model, temperature=0.3,
                     api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    llm_fast = ChatOpenAI(model=settings.llm_model_fast, temperature=0.0,
                          api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    # Two arguments here exist only so OPENAI_BASE_URL is a real switch rather
    # than a decoration.
    #
    # check_embedding_ctx_length=False: by default OpenAIEmbeddings tokenises
    # with tiktoken and posts *token ids* instead of strings. api.openai.com
    # accepts that; Gemini, Ollama, vLLM and TEI reject it, and the failure
    # arrives as an opaque 400 from inside the SDK rather than anything naming
    # the cause. Sending raw text costs nothing against OpenAI, which tokenises
    # server-side anyway.
    #
    # dimensions: the HNSW index declares its width at definition time and can
    # never be widened afterwards, so the embedding client has to be *asked* for
    # EMBED_DIM rather than trusted to emit it. text-embedding-3-small happens to
    # emit 1536 natively; gemini-embedding-001 emits 3072 unless told otherwise,
    # and silently writing 3072-wide vectors at a 1536-wide index is a migration
    # failure discovered on the first message.
    embed = OpenAIEmbeddings(model=settings.embed_model, api_key=settings.openai_api_key,
                             base_url=settings.openai_base_url,
                             dimensions=settings.embed_dim,
                             check_embedding_ctx_length=False)

    tools = build_tools(database, embed)
    # include_raw keeps the underlying AIMessage alongside the parsed model, which
    # is the only way to read token usage -- and "tokens per completed task" is a
    # metric docs/06 asks for and nothing could answer.
    reasoner = llm.bind_tools(tools).with_structured_output(ReasonOutput, include_raw=True)

    async def perceive(state: CortexState) -> dict:
        """Write the user's turn and embed it.

        This node is why the user's own message appears in the graph before the
        model has done anything -- the first sign of life on screen, and it costs
        one write.
        """
        question = state["query"]
        rows = await database.query("""
            LET $sess = type::record($session_id);
            LET $m = (CREATE ONLY message SET session = $sess, role = "user", text = $text);
            RETURN $m.id;
        """, {"session_id": state["session_id"], "text": question})

        vector = (await embed.aembed_documents([question]))[0]
        return {
            "message_id": record_str(one(rows)),
            "query_embedding": vector,
            "messages": [HumanMessage(content=question)],
            "loops": state.get("loops", 0),
        }

    async def recall_node(state: CortexState) -> dict:
        """Run hybrid recall and record it.

        Writing the `retrieval` record is what makes the nodes glow in the
        browser and what fires the decay event, so this node has visible
        consequences beyond its return value.
        """
        hits, retrieval_id = await recall(
            database,
            state["query_embedding"],
            state["query"],
            state["session_id"],
        )
        return {
            "recalled": [hit.model_dump() for hit in hits],
            "retrieval_ids": [*state.get("retrieval_ids", []), retrieval_id],
        }

    async def reason(state: CortexState) -> dict:
        """Produce the answer and the turn's extracted facts in one call.

        Structured output means the schema is enforced by constrained decoding
        rather than by parsing hopefully. That is what makes the single-call
        design safe: without it a malformed extraction would either lose the
        turn's memory or write nonsense into it.
        """
        recalled = state.get("recalled") or []
        if recalled:
            context = "\n".join(
                f"  [{hit['via']}] ({hit['fact_id']}) {hit['text']}" for hit in recalled)
        else:
            context = "  (nothing recalled -- this may be the first thing you are told)"

        with tracer().start_as_current_span("reason") as span:
            span.set_attribute(GEN_AI_SYSTEM, "openai")
            span.set_attribute(GEN_AI_MODEL, settings.llm_model)
            span.set_attribute("cortex.recalled", len(recalled))
            span.set_attribute("cortex.loop", state.get("loops", 0))

            envelope = await reasoner.ainvoke([
                ("system", SYSTEM_PROMPT),
                ("human", f"Recalled facts:\n{context}\n\nUser said: {state['query']}"),
            ])
            result: ReasonOutput = envelope["parsed"]

            # Token counts only. Prompt and completion text stay out of the span
            # table, which anyone holding a viewer token can read -- and the
            # browser holds one.
            usage = getattr(envelope.get("raw"), "usage_metadata", None) or {}
            span.set_attribute(GEN_AI_INPUT_TOKENS, usage.get("input_tokens", 0))
            span.set_attribute(GEN_AI_OUTPUT_TOKENS, usage.get("output_tokens", 0))
            span.set_attribute("cortex.extracted", len(result.extracted))

        return {
            "answer": result.answer,
            "extracted": [fact.model_dump() for fact in result.extracted],
            "messages": [AIMessage(content=result.answer)],
            "loops": state.get("loops", 0) + 1,
        }

    async def consolidate_node(state: CortexState) -> dict:
        """Write the turn's facts into memory.

        All embeddings for the turn are produced in one batched call. Embedding
        inside the per-fact loop is the easy mistake, and it shows up directly as
        a graph that animates in slow steps instead of at once.
        """
        raw = state.get("extracted") or []
        if not raw:
            return {"outcomes": []}

        with tracer().start_as_current_span("consolidate") as span:
            facts = [Extracted(**item) for item in raw]
            span.set_attribute("cortex.facts", len(facts))

            vectors = await embed.aembed_documents([fact.text for fact in facts])
            span.set_attribute(GEN_AI_MODEL, settings.embed_model)

            outcomes = await consolidate(
                database, llm_fast, facts, vectors,
                state["message_id"],  # type: ignore[typeddict-item]
            )
            for action in ("create", "supersede", "duplicate", "failed"):
                span.set_attribute(f"cortex.{action}",
                                   sum(1 for o in outcomes if o.get("action") == action))
        return {"outcomes": outcomes}

    async def respond(state: CortexState) -> dict:
        """Write the assistant's turn, so the conversation is in the memory too."""
        await database.query("""
            LET $sess = type::record($session_id);
            CREATE message SET session = $sess, role = "assistant", text = $text;
        """, {"session_id": state["session_id"], "text": state.get("answer", "")})
        return {}

    def needs_more_context(state: CortexState) -> str:
        """Decide whether to recall again before answering.

        Loops only when the model produced no answer at all -- a genuine sign it
        could not work with what it was given. Anything richer would need the
        model to ask for it explicitly, and an uncapped or eager loop is how a
        demo hangs on stage.
        """
        if not (state.get("answer") or "").strip() and state.get("loops", 0) < MAX_LOOPS:
            return "recall"
        return "consolidate"

    graph = StateGraph(CortexState)
    graph.add_node("perceive", perceive)
    graph.add_node("recall", recall_node)
    graph.add_node("reason", reason)
    graph.add_node("consolidate", consolidate_node)
    graph.add_node("respond", respond)

    graph.add_edge(START, "perceive")
    graph.add_edge("perceive", "recall")
    graph.add_edge("recall", "reason")
    graph.add_conditional_edges("reason", needs_more_context,
                                {"recall": "recall", "consolidate": "consolidate"})
    graph.add_edge("consolidate", "respond")
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=SurrealCheckpointSaver(database))
