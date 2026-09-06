"""The shapes that cross a boundary: LLM output, retrieval hits, agent state.

The three response models are bound to the model with `with_structured_output`,
so OpenAI enforces them by constrained decoding rather than by hopeful JSON
parsing. That matters more here than in most places: `reason` returns the answer
*and* the turn's extracted facts in a single call, and a malformed extraction
would either lose the turn's memory or poison it.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

Via = Literal["vector", "text", "graph"]


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------


class Hit(BaseModel):
    """One recalled fact, carrying how it was found.

    `via` survives all the way to the browser -- it is what decides a glowing
    node's colour, so a hit that loses its provenance loses the thing the
    visualisation exists to show.
    """

    fact_id: str
    text: str
    score: float
    via: Via
    confidence: float = 0.5
    snippet: str | None = None


# --------------------------------------------------------------------------
# what the model returns
# --------------------------------------------------------------------------


class Relation(BaseModel):
    """An association between two named entities, as the model asserts it."""

    subject: str = Field(description="Name of the entity the relation starts at")
    predicate: str = Field(description="Snake_case verb phrase, e.g. works_at, lives_in")
    object: str = Field(description="Name of the entity the relation points to")


class EntityRef(BaseModel):
    """An entity a fact is about."""

    name: str = Field(description="The entity's name exactly as a person would write it")
    kind: Literal["person", "place", "org", "concept", "artifact", "event"]


class Extracted(BaseModel):
    """A single atomic fact worth remembering, with what it is about.

    Atomic is the requirement that matters. "Ana moved to Berlin and joined
    Globex" is two facts; stored as one it can never be individually superseded,
    and the temporal model silently degrades.
    """

    text: str = Field(description="One self-contained statement, in the third person")
    confidence: float = Field(ge=0.0, le=1.0,
                              description="How certain this is, given what the user said")
    entities: list[EntityRef] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


class ReasonOutput(BaseModel):
    """The answer and the memory, in one response.

    Asking for both together avoids a second round trip and -- more importantly --
    keeps extraction grounded in what was actually said, rather than in a summary
    of it.
    """

    answer: str = Field(description="The reply to show the user")
    extracted: list[Extracted] = Field(
        default_factory=list,
        description="New facts worth remembering. Empty if the turn taught you nothing.",
    )


class ContradictionCheck(BaseModel):
    """Whether a new fact contradicts one already believed.

    Deliberately tiny. By the time this is asked, the graph has narrowed the
    candidates from the whole store to the few current facts mentioning the same
    entities, which is what makes the cheap model sufficient.
    """

    contradicts: bool
    target_fact_id: str | None = Field(
        default=None, description="The id of the fact being contradicted, if any")
    reason: str = Field(default="", description="One sentence, shown in the UI")


# --------------------------------------------------------------------------
# agent state
# --------------------------------------------------------------------------


class CortexState(TypedDict, total=False):
    """The state carried through the graph.

    `messages` uses `add_messages` so LangGraph merges turns rather than
    replacing them, which is what makes the checkpointer's resumed threads
    coherent.
    """

    session_id: str
    thread_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    query: str
    message_id: str
    query_embedding: list[float]
    recalled: list[dict]
    retrieval_ids: list[str]
    extracted: list[dict]
    outcomes: list[dict]
    answer: str
    loops: int
