"""Consolidation: turning what the model extracted into memory.

The node that does the real work, and the only one where getting it wrong
destroys something. Three outcomes per extracted fact:

  * **duplicate** -- already known. Bump confidence and salience, add a
    `derived_from` edge to the new message, create no node. Without this, saying
    the same thing twice doubles the graph.
  * **contradiction** -- replaces something believed. Create the new fact, end the
    old one, link them with `supersedes`. Nothing is deleted, ever.
  * **new** -- create it.

Then link entities, and relate them to each other.
"""

from __future__ import annotations

import logging

from .db import Database, one, record_str
from .models import ContradictionCheck, Extracted

logger = logging.getLogger("cortex.consolidate")

# Cosine similarity above which two facts are treated as the same thing said
# twice. High on purpose: a false duplicate silently loses information, which is
# worse than a near-duplicate node the decay pass will quieten anyway.
DUPLICATE_THRESHOLD = 0.95


async def _find_duplicate(database: Database, embedding: list[float]) -> dict | None:
    """Find an existing current fact that means the same thing.

    A KNN lookup rather than a full scan: the HNSW index answers this in
    microseconds, which is what makes dedupe affordable on every extracted fact
    rather than something done in a nightly pass.
    """
    rows = await database.query("""
        SELECT id, text, confidence, 1 - vector::distance::knn() AS similarity
        FROM fact
        WHERE embedding <|1,64|> $q AND valid_to = NONE;
    """, {"q": embedding})

    candidate = one(rows)
    if candidate and float(candidate.get("similarity") or 0) >= DUPLICATE_THRESHOLD:
        return candidate
    return None


async def _current_facts_about(database: Database, names: list[str]) -> list[dict]:
    """Fetch the current facts that mention any of these entities.

    This is what makes the cheap model sufficient for contradiction checking. The
    graph has already narrowed the candidates from the entire store to a handful
    of records about the same subjects, so the question asked of the LLM is small
    and concrete rather than open-ended.
    """
    if not names:
        return []
    rows = await database.query("""
        LET $entities = (SELECT VALUE id FROM entity WHERE name IN $names);
        SELECT id, text FROM fact
        WHERE valid_to = NONE AND ->mentions->entity CONTAINSANY $entities
        LIMIT 12;
    """, {"names": names})
    return rows or []


async def _check_contradiction(llm_fast, fact: Extracted, candidates: list[dict]) -> ContradictionCheck:
    """Ask the cheap model whether this fact replaces one of the candidates.

    Structured output with a three-field schema. The model is given ids and told
    to return one, so the answer is checkable rather than prose to be parsed.
    """
    if not candidates:
        return ContradictionCheck(contradicts=False)

    listing = "\n".join(f"{record_str(row['id'])}: {row['text']}" for row in candidates)
    checker = llm_fast.with_structured_output(ContradictionCheck)

    result = await checker.ainvoke(
        "You maintain a memory of facts about people and things. Decide whether "
        "the NEW fact replaces exactly one EXISTING fact.\n\n"
        "Replace when the two state different values for something that can only "
        "hold one value at a time: where someone works, where they live, who they "
        "report to, what something is called, what its current status is. A "
        "person has one employer -- a new employer replaces the old one. This is "
        "the most common case and the one that matters most.\n\n"
        "Do NOT replace when the new fact merely adds detail, is about a "
        "different subject, or can be true alongside the old one -- interests, "
        "skills, relationships, past events.\n\n"
        "Return the id of the single fact being replaced, or contradicts=false. "
        "When genuinely unsure, answer false.\n\n"
        f"NEW: {fact.text}\n\nEXISTING:\n{listing}"
    )
    return result


async def _upsert_entities(database: Database, fact: Extracted) -> list[str]:
    """Create or update the entities this fact is about, returning their ids.

    `UPSERT ... WHERE` against the unique (name, kind) index rather than
    select-then-create, so two concurrent turns mentioning the same person cannot
    produce two records for them.
    """
    if not fact.entities:
        return []

    rows = await database.query("""
        LET $ids = [];
        FOR $e IN $entities {
            UPSERT entity SET name = $e.name, kind = $e.kind, last_seen = time::now()
            WHERE name = $e.name AND kind = $e.kind;
        };
        SELECT VALUE id FROM entity
        WHERE name IN $names AND kind IN $kinds;
    """, {
        "entities": [{"name": e.name, "kind": e.kind} for e in fact.entities],
        "names": [e.name for e in fact.entities],
        "kinds": list({e.kind for e in fact.entities}),
    })
    return [record_str(row) for row in (rows or [])]


async def consolidate_fact(
    database: Database,
    llm_fast,
    fact: Extracted,
    embedding: list[float],
    message_id: str,
) -> dict:
    """Write one extracted fact into memory, and report what happened to it.

    Returns `{"action": "duplicate" | "supersede" | "create", "fact": id}` so the
    caller can tell the user what the turn actually changed -- and so the tests
    can assert on the decision rather than only on the resulting row count.
    """
    duplicate = await _find_duplicate(database, embedding)
    if duplicate:
        # Reinforce rather than duplicate. The second `derived_from` edge is the
        # point: provenance now records both occasions the fact was asserted.
        fact_id = record_str(duplicate["id"])
        await database.query("""
            LET $f = type::record($fact_id);
            LET $m = type::record($message_id);
            UPDATE $f SET confidence = math::min([confidence + 0.05, 1.0]),
                          salience = math::min([salience * 1.1, 3.0]);
            RELATE $f->derived_from->$m;
        """, {"fact_id": fact_id, "message_id": message_id})
        logger.info("duplicate: %s", fact.text[:60])
        return {"action": "duplicate", "fact": fact_id}

    entity_ids = await _upsert_entities(database, fact)
    candidates = await _current_facts_about(database, [e.name for e in fact.entities])
    verdict = await _check_contradiction(llm_fast, fact, candidates)

    # Match on the bare id as well as the full `fact:abc` form. The model is shown
    # ids with their table prefix and returns them without it often enough to
    # matter -- and rejecting a correct answer over a missing prefix silently
    # turns off contradiction detection, which looks exactly like the model
    # failing to notice.
    known = {}
    for row in candidates:
        full = record_str(row["id"])
        known[full] = full
        known[full.split(":", 1)[-1]] = full

    target = None
    if verdict.contradicts and verdict.target_fact_id:
        target = known.get(verdict.target_fact_id.strip())
        if target is None:
            # Named something that was never on the list. Treated as no
            # contradiction rather than trusted: an id that does not exist would
            # end a random fact, or fail the whole transaction.
            logger.warning("contradiction target %r was not a candidate; ignoring",
                           verdict.target_fact_id)

    params = {
        "text": fact.text,
        "embedding": embedding,
        "confidence": fact.confidence,
        "message_id": message_id,
        "entity_ids": entity_ids,
        "relations": [
            {"subject": r.subject, "predicate": r.predicate, "object": r.object}
            for r in fact.relations
        ],
        "old_id": target,
    }

    # One transaction. A reader must never see the new fact current alongside the
    # old one, nor a supersedes edge pointing at a fact that is not yet ended --
    # the property tests/test_concurrency.py measures at fifty concurrent writers.
    rows = await database.query("""
        BEGIN TRANSACTION;
        LET $m = type::record($message_id);
        LET $new = (CREATE ONLY fact SET text = $text, embedding = $embedding,
                                        confidence = $confidence);
        RELATE $new->derived_from->$m;

        FOR $eid IN $entity_ids {
            LET $e = type::record($eid);
            RELATE $new->mentions->$e;
        };

        IF $old_id != NONE {
            LET $old = type::record($old_id);
            UPDATE $old SET valid_to = time::now();
            RELATE $new->supersedes->$old;
        };

        FOR $r IN $relations {
            LET $a = (SELECT * FROM ONLY entity WHERE name = $r.subject LIMIT 1);
            LET $b = (SELECT * FROM ONLY entity WHERE name = $r.object LIMIT 1);
            IF $a != NONE AND $b != NONE {
                RELATE $a->relates->$b SET predicate = $r.predicate, strength = 0.7;
            };
        };

        COMMIT TRANSACTION;
        -- After the commit, not before. The transaction's own result is empty, so
        -- a RETURN placed inside it is discarded and every caller sees None.
        -- LET bindings outlive the COMMIT within the same batch, so the id is
        -- still reachable here.
        RETURN $new.id;
    """, params)

    fact_id = record_str(one(rows))
    action = "supersede" if target else "create"
    logger.info("%s: %s", action, fact.text[:60])
    return {"action": action, "fact": fact_id, "superseded": target}


async def consolidate(
    database: Database,
    llm_fast,
    facts: list[Extracted],
    embeddings: list[list[float]],
    message_id: str,
) -> list[dict]:
    """Write every fact from a turn, in order.

    Embeddings arrive pre-computed as a list because they are produced in a
    single batched call upstream. Embedding inside this loop would be one HTTP
    round trip per fact -- easy to write by accident, and visible to the user as a
    graph that animates in slow steps instead of all at once.
    """
    outcomes = []
    for fact, embedding in zip(facts, embeddings):
        try:
            outcomes.append(await consolidate_fact(
                database, llm_fast, fact, embedding, message_id))
        except Exception as error:  # noqa: BLE001 - one bad fact must not lose the turn
            logger.exception("failed to consolidate %r", fact.text[:60])
            outcomes.append({"action": "failed", "error": str(error)})
    return outcomes
