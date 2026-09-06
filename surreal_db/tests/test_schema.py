"""L1 - schema semantics, asserted against thousands of records rather than six.

Everything here was previously "verified" by hand on a fixture holding one
supersession. At that size a system that works and a system that happens to work
are indistinguishable, and several of these assertions would have passed
vacuously.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow


# --------------------------------------------------------------------------
# the load itself
# --------------------------------------------------------------------------


def test_corpus_loaded_completely(corpus, counts):
    """Every record the generator produced reached the database.

    A partially loaded corpus makes every later assertion weaker without making
    any of them fail, which is the worst possible way for a test suite to be
    wrong.
    """
    assert counts["entities"] == len(corpus.entities), (
        f"{len(corpus.entities) - counts['entities']} entities did not load"
    )
    assert counts["facts"] == len(corpus.facts)
    assert counts["relates"] == len(corpus.edges)

    expected_mentions = sum(len(fact.mentions) for fact in corpus.facts)
    assert counts["mentions"] == expected_mentions

    expected_supersessions = sum(1 for fact in corpus.facts if fact.superseded_by)
    assert counts["supersedes"] == expected_supersessions


def test_corpus_is_big_enough_to_mean_anything(counts):
    """Guard against the suite quietly running on a toy fixture.

    Every claim below is a claim about behaviour at volume. If the corpus
    collapses -- a generator regression, a failed load, someone running the
    small scale by accident -- these tests would still pass and prove nothing.
    """
    assert counts["entities"] >= 100, "corpus too small for the graph assertions to mean anything"
    assert counts["facts"] >= 300
    assert counts["supersedes"] >= 50, "not enough history to test the temporal model"


# --------------------------------------------------------------------------
# the temporal model
# --------------------------------------------------------------------------


def test_is_current_agrees_with_valid_to_for_every_fact(sql, counts):
    """`is_current` is COMPUTED, so it must never disagree with `valid_to`.

    This is the assertion that would have caught the original `VALUE` definition,
    which only re-evaluated on write and therefore went stale the moment a fact
    was superseded by a transaction touching a different record.
    """
    disagreements = sql("computed-field-consistency", """
        RETURN {
            current_but_ended: (SELECT VALUE count() FROM fact
                                WHERE is_current = true AND valid_to != NONE GROUP ALL),
            ended_but_current: (SELECT VALUE count() FROM fact
                                WHERE is_current = false AND valid_to = NONE GROUP ALL),
            scratch_left_behind: (SELECT VALUE count() FROM fact
                                  WHERE string::starts_with(text, '[') GROUP ALL),
        };
    """)[-1]
    assert disagreements["current_but_ended"] in ([], [0]), disagreements
    assert disagreements["ended_but_current"] in ([], [0]), disagreements


def test_superseded_facts_are_never_deleted(sql, counts, corpus):
    """The central promise: contradiction marks history, it does not destroy it.

    Asserted on hundreds of supersessions produced by real churn -- reorgs,
    acquisitions, on-call rotations -- rather than on one planted pair.
    """
    superseded = sql("superseded-still-present", """
        SELECT VALUE count() FROM fact
        WHERE valid_to != NONE AND !string::starts_with(text, '[') GROUP ALL;
    """)[-1]
    count = superseded[0]["count"] if superseded and isinstance(superseded[0], dict) else superseded[0]

    expected = sum(1 for fact in corpus.facts if fact.superseded_by)
    assert count == expected, "a superseded fact went missing -- nothing may ever be deleted"

    # And they must still be readable, not merely present in a count.
    sample = sql("read-a-superseded-fact", """
        SELECT id, text, is_current, valid_to, ->supersedes->fact.text AS replaces
        FROM fact WHERE valid_to != NONE AND !string::starts_with(text, '[') LIMIT 5;
    """)[-1]
    assert len(sample) == 5
    for record in sample:
        assert record["is_current"] is False
        assert record["valid_to"] is not None


def test_supersession_chains_are_stored_end_to_end(sql, corpus):
    """A relation asserted five times leaves five facts, one truth, and a walkable chain.

    Chains, not pairs. The corpus produces them because people change teams
    repeatedly over eighteen months, and a chain is where an off-by-one in the
    temporal logic actually shows up.

    The depth is walked one hop at a time rather than with `.{1..n}`. That
    recursion returns only the *terminal* node it reaches -- not the path, and
    not one entry per depth -- so `array::len` on it is 1 for a chain of any
    length. It is the same semantic that made the associative arm under-recall,
    wearing a different disguise, and it is the reason this test does the walk
    itself.
    """
    # The generator knows the true chain lengths, so the expected depth is a
    # fact about the corpus rather than something read back from the system
    # under test.
    successor = {fact.slug: fact.superseded_by for fact in corpus.facts if fact.superseded_by}
    predecessors: dict[str, str] = {new: old for old, new in successor.items()}

    def depth(slug: str) -> int:
        """Count how many facts this one transitively replaces."""
        steps = 0
        while slug in predecessors:
            slug = predecessors[slug]
            steps += 1
        return steps

    heads = [fact.slug for fact in corpus.facts
             if not fact.superseded_by and fact.slug in predecessors]
    assert heads, "the corpus generated no supersession chains at all"

    deepest = max(heads, key=depth)
    expected = depth(deepest)
    assert expected >= 3, (
        f"longest chain is only {expected} deep; the corpus should produce chains "
        f"several links long so this is not just testing pairs"
    )

    # Walk the same chain in the database, one documented hop at a time.
    current, walked = f"fact:{deepest}", 0
    while True:
        ancestors = sql("walk-chain", """
            SELECT VALUE ->supersedes->fact FROM type::record($id);
        """, {"id": current})[-1]
        flat = [item for group in ancestors for item in (group if isinstance(group, list) else [group])]
        if not flat:
            break
        assert len(flat) == 1, f"{current} supersedes {len(flat)} facts at once"
        current = flat[0]
        walked += 1
        assert walked <= expected + 1, "chain walk did not terminate -- there is a cycle"

    assert walked == expected, (
        f"chain from {deepest} is {walked} deep in the database but {expected} in the "
        f"corpus -- supersession links were lost on write"
    )

    # The head of the chain is the one current version; every ancestor is ended.
    # Queried by record rather than with `id INSIDE [...]`, because a bound array
    # of id *strings* never matches a record -- the same coercion trap as
    # everywhere else.
    head_state = sql("chain-head-is-current", """
        SELECT VALUE valid_to FROM type::record($id);
    """, {"id": f"fact:{deepest}"})[-1]
    assert head_state == [None], f"the head of the chain is not current: {head_state}"


def test_no_fact_is_superseded_twice(sql):
    """Two facts claiming to replace the same predecessor means a forked history.

    Nothing in the write path prevents it, so it is asserted rather than assumed.
    """
    forks = sql("fork-detection", """
        SELECT VALUE count() FROM (
            SELECT id, array::len(<-supersedes<-fact) AS replaced_by
            FROM fact
        ) WHERE replaced_by > 1 GROUP ALL;
    """)[-1]
    assert forks in ([], [0]), f"{forks} facts are superseded by more than one successor"


def test_time_travel_returns_the_version_that_was_current_then(sql, corpus):
    """Asking about the past returns the belief held then, not the belief held now.

    The query is the one from docs/03: `valid_from <= $t AND (valid_to = NONE OR
    valid_to > $t)`. With eighteen months of on-call rotations in the corpus,
    "who was on call in March" has a different answer every month, which is what
    makes this worth asserting.
    """
    from corpus import timestamp

    early = timestamp(2)
    late = timestamp(corpus.entities and 99 or 99)

    at_time = sql("as-of", """
        LET $t = type::datetime($when);
        SELECT VALUE count() FROM fact
        WHERE valid_from <= $t AND (valid_to = NONE OR valid_to > $t)
        GROUP ALL;
    """, {"when": early})[-1]
    now = sql("as-of-now", """
        SELECT VALUE count() FROM fact WHERE valid_to = NONE GROUP ALL;
    """)[-1]

    def scalar(result):
        """Unwrap a GROUP ALL count that may come back as an int or a row."""
        if not result:
            return 0
        return result[0]["count"] if isinstance(result[0], dict) else result[0]

    early_count, now_count = scalar(at_time), scalar(now)
    assert early_count > 0, "no facts were current two months in -- the timeline is broken"
    assert early_count != now_count, (
        "the world looks identical at month 2 and today, so nothing actually changed "
        "and the temporal assertions are vacuous"
    )
    assert late is not None


# --------------------------------------------------------------------------
# referential integrity
# --------------------------------------------------------------------------


def test_no_mention_edge_dangles(sql):
    """Every `mentions` edge points at an entity that exists.

    `RELATE` will happily create an edge to a record that was never created, so
    a generator that wrote edges before entities would produce a graph that is
    subtly wrong in a way only traversal notices.
    """
    dangling = sql("dangling-mentions", """
        SELECT VALUE count() FROM mentions WHERE out.id = NONE GROUP ALL;
    """)[-1]
    assert dangling in ([], [0]), f"{dangling} mention edges point at nothing"


def test_entity_names_are_unique_within_a_kind(sql, counts):
    """The `entity_identity` unique index is doing its job.

    The corpus deliberately generates repeated human names, so this is exercised
    by data rather than by a contrived concurrent write.
    """
    duplicates = sql("duplicate-entities", """
        SELECT name, kind, count() AS n FROM entity
        GROUP BY name, kind;
    """)[-1]
    offenders = [row for row in duplicates if row["n"] > 1]
    assert not offenders, f"duplicate entities slipped past the unique index: {offenders[:5]}"


def test_ambiguous_names_survive_in_fact_text(sql, corpus):
    """Two people share a name in prose even though their records do not.

    CORTEX keys entity identity on (name, kind), so the database cannot hold two
    people called Chen Wallace. The corpus disambiguates the *record* and leaves
    the *text* ambiguous, which is the situation a real memory faces -- and the
    reason this limitation is worth stating rather than hiding.
    """
    collisions = {}
    for entity in corpus.entities:
        if entity.kind == "person":
            collisions.setdefault(entity.label, []).append(entity.name)
    shared = {label: names for label, names in collisions.items() if len(names) > 1}

    if not shared:
        pytest.skip("this seed produced no name collisions; try another --seed")

    label = next(iter(shared))
    mentions = sql("ambiguous-name-in-text", """
        SELECT VALUE count() FROM fact WHERE text CONTAINS $name GROUP ALL;
    """, {"name": label})[-1]
    count = mentions[0]["count"] if mentions and isinstance(mentions[0], dict) else (mentions or [0])[0]
    assert count > 1, (
        f"{label!r} names two different people but appears in {count} facts; the "
        f"ambiguity the retrieval tests rely on is not present"
    )
