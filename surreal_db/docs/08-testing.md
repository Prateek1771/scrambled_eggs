# 08 — Testing

Until this document existed, every claim CORTEX makes had been verified by hand,
once, against a fixture holding **6 facts and 7 entities**. At that size a system
that works and a system that happens to work are indistinguishable — and several
of the checks would have passed vacuously.

The suite replaces that with a simulated organisation, real faults, and numbers.

```bash
docker compose up -d --wait surrealdb && docker compose run --rm migrate
pytest tests --scale medium          # the full run, headed browser
pytest tests --scale small -m "not slow"   # the fast loop
```

---

## The corpus is a company, not a fixture

`tests/corpus.py` simulates an engineering organisation over **18 months**:
people, the teams they move between, the services those teams own, the incidents
those services cause, two acquisitions and a reorganisation.

| | entities | facts | edges | superseded |
|---|---|---|---|---|
| `small` | 148 | 343 | ~1,000 | 67 |
| **`medium`** | **721** | **3,439** | **11,316** | **961** |
| `huge` | ~6,000 | ~28,000 | ~95,000 | ~8,000 |

The domain is not decoration. Every hard case the suite needs falls out of the
simulation rather than being planted one at a time:

- **Supersession with real history.** People change teams and managers, services
  change owners, acquisitions rewrite whole subtrees. Chains reach **5 links**,
  so "nothing is ever deleted" is tested against a timeline instead of a pair.
- **Association no vector search can reach.** *"Who is on call for the services
  that depend on the library Ana maintains?"* The answer shares no vocabulary
  with the question and sits far from it in embedding space. This is the exact
  population the `.{1..2}` recursion bug dropped silently.
- **Lexical needles.** `INC-4417`, `#8812`, `ETIMEDOUT`. Unrecallable by
  embedding, which is why the BM25 arm has to exist.
- **Genuine ambiguity.** Two people named Chen Wallace; both companies have a
  team called Platform.

Deterministic from a seed, so any failure reproduces from one number.

---

## What each layer proves

| Layer | File | Proves |
|---|---|---|
| L1 | `test_schema.py` | Temporal model at volume: `COMPUTED is_current`, chain walks, time travel, referential integrity |
| L2 | `test_security.py` | The browser's token can read and do nothing else; no credential reaches the client |
| L4 | `test_concurrency.py` | **Zero torn reads** under concurrent supersession at 1 / 10 / 50 writers |
| L5 | `test_live.py` | Live-query semantics, including the two traps that cost a day each |
| L8 | `e2e/test_browser.py` | The real page, at scale, surviving the database being restarted underneath it |
| L9 | `test_agent.py` | Rank fusion, the checkpointer, the memory tools, and one real conversation |

**51 tests, 334 seconds** including a full corpus load and three model turns.

L9 is split by cost. Fusion is a pure function, and the checkpointer and tools are
database round trips, so `-m "not llm"` runs most of the layer for free; three
tests call OpenAI and are marked, because a suite that costs a dollar to run is a
suite nobody runs.

---

## Results

Environment: Windows 11, Python 3.14.3, Intel i7-1165G7 family, Docker 29.6.1,
SurrealDB **3.1.6**, corpus `medium` seed 7, single node, RocksDB.

### Corpus load

| Metric | Value |
|---|---|
| Facts written | 3,439 |
| Write rate | **87 facts/s** |
| Total load | 42.6 s |

Slow, and worth stating plainly: each fact carries a 1536-float vector into an
HNSW index, and the loader batches 24 per request because SurrealDB's HTTP
endpoint rejects larger bodies with a bare `413`.

### Torn reads — the claim this project is built on

A split stack writes a supersession across Postgres, Neo4j and Elasticsearch with
no shared transaction, so a reader can observe a half-applied world. One
transactional engine should have no such window.

| Concurrent writers | Committed | Retries | Retries/commit | Gave up | Reads checked | **Torn reads** |
|---|---|---|---|---|---|---|
| 1 | 39 | 0 | 0.00 | 0 | 80 | **0** |
| 10 | 71 | 44 | 0.62 | 1 | 34 | **0** |
| 50 | 73 | 172 | 2.36 | 6 | 16 | **0** |

Zero at every level — but the honest reading includes the right-hand columns.
The guarantee is not free: at 50 concurrent writers, a supersession takes **2.36
retries per commit**, and six transactions exhausted a six-attempt budget
entirely. SurrealDB is refusing to produce the torn state a split stack would
have produced silently, and that refusal costs throughput.

### The browser at scale

| Metric | Value |
|---|---|
| Nodes rendered | 4,160 (3,439 facts + 721 entities) |
| Edges rendered | 11,316 |
| WebSocket frames to load it | 17 |
| **Data received** | **0.4 MB** |
| Frame rate under continuous zoom | 47 fps |
| JS heap | 42 MB |

0.4 MB is the number worth pausing on. The same graph with embeddings included
would be roughly **50 MB**, and nothing on screen would look different. The live
subscriptions project explicit field lists, and `test_no_embedding_ever_crosses_the_websocket`
reads the actual frames to prove it.

---

## What the suite found

None of these were visible on a six-node fixture.

### 1. The graph was unreadable above ~1,200 nodes

Not a performance failure — 47 fps at 4,160 nodes — a **legibility** one. Every
node drew a label, producing a solid block of overlapping text.
`docs/05-ui.md` specified a salience floor above ~2,000 nodes; it had never been
implemented. Now facts earn a label only when zoomed in, entities keep theirs,
and quiet facts recede without being truncated.

### 2. The page went permanently stale behind a green indicator

The worst bug found, and the chaos test found it on its first run. Restarting the
database underneath a live page left the browser **one fact behind, for 120
seconds, still showing "live"**.

The cause: the SurrealDB JS SDK reconnects its socket transparently. The
application never learned the transport had failed, so it never re-registered its
live queries — which do not survive the new socket — and never resynced. The page
looked healthy and had simply stopped listening.

The fix is to subscribe to the SDK's `disconnected` / `reconnecting` / `connected`
events and turn a transparent recovery into an explicit re-register and resync.
A stale graph that looks healthy is worse than one that flickers.

### 3. `signin` does not always end live queries

`docs/02` states flatly that "signin invalidates live queries on that session".
Measured against 3.1.6 that is too strong. Re-authenticating as the **same**
principal leaves the subscriptions intact; only a change of *identity* ends them.
Both halves are now asserted, because the difference decides whether token
refresh needs a full re-register.

### 4. The Python SDK discards the live-query action

`subscribe_live()` yields `ret["result"]` and throws the rest of the envelope
away — including `action`. A Python consumer using the documented helper
**cannot tell a CREATE from an UPDATE from a DELETE**. The raw envelope
(`action`, `id`, `record`, `result`, `session`) is available on the underlying
queue, which is what these tests read.

### 5. A filtered live query cannot report a record leaving it

Confirmed, and now pinned from both sides: an unfiltered `LIVE SELECT` delivers
the UPDATE that ends a fact; `WHERE valid_to = NONE` delivers nothing, because a
live query says nothing about a record that has left its result set. Subscribing
the way `docs/05` originally specified would freeze every superseded node on
screen looking current — at precisely the moment the demo is built around.

### 6. Two people cannot share a name

`entity_identity` is `UNIQUE(name, kind)`, so the schema cannot hold two people
called Chen Wallace. A real memory system meets this constantly. The corpus
disambiguates the *record* and leaves the *text* ambiguous, which is the
situation retrieval actually faces — but the limitation is the schema's, and it
is recorded here rather than hidden.

### 7. Tests that pass for the wrong reason

Worth recording because it nearly shipped. `subscribe_live` is a coroutine that
*returns* the generator; iterating it un-awaited yields nothing, silently. Every
assertion in the live layer that checks for the **absence** of a notification
passed while the collector was not listening at all. The presence tests are what
prove the collector works, and they are why the absence tests can be believed.

The same class of error appeared in the concurrency layer: reading only the first
failing statement classified retryable conflicts as hard errors, because a
transaction reports a generic "not executed" for its other statements — which
turned a contention experiment into a measurement of nothing.

---

### 8. The Python SDK's `query()` silently discards all but the first statement

The one that cost the most. `AsyncSurreal.query()` returns
`response["result"][0]` — the result of the **first** statement. Every
multi-statement query in CORTEX is shaped `LET …; LET …; RETURN …`, so each one
returned the opening `LET`'s empty result and threw the `RETURN` away.

The symptom is not an error. It is a `None` that travels: a session id becomes
the string `"None"`, which fails three calls later inside `type::record()` with a
message naming neither the statement nor the cause. `Database.query` goes through
`query_raw` and returns the last statement's result, checking every statement's
status on the way.

### 9. An empty `OPENAI_BASE_URL` is worse than an absent one

`docker-compose.yml` passes `${OPENAI_BASE_URL:-}`, which sets the variable to an
empty string. The OpenAI SDK reads that variable itself, and an empty value
overrides its default with a relative URL. Every call then fails as
`APIConnectionError: Connection error.` — pointing at the network, while an httpx
request to the same host from the same container had succeeded a second earlier.

The real error is two `__cause__` levels down: *"Request URL is missing an
'http://' or 'https://' protocol"*. `config.py` now removes the variable when it
is blank.

### 10. `RETURN` inside a transaction is discarded

`BEGIN … RETURN $new.id; COMMIT` returns nothing: the commit is the last
statement and its result is empty, so every consolidated fact reported an id of
`None`. `LET` bindings outlive the commit within the same batch, so the `RETURN`
belongs *after* `COMMIT TRANSACTION`.

### 11. The model returns record ids without their table prefix

Contradiction detection is shown ids as `fact:2smn9…` and answers `2smn9…`. The
guard that rejects unknown ids — there so a hallucinated id cannot end a random
fact — was rejecting correct answers, which silently switched contradiction
detection off and looked exactly like the model failing to notice. Both forms are
accepted now, and still only if they name a candidate that was actually offered.

### 12. Transaction conflicts have to be retried in the application, too

SurrealDB reports contention as *"Transaction conflict … can be retried"*. The
concurrency layer already retried them; the agent did not, and failed whole turns
over a conflict the database expected it to retry. `Database.query` now separates
three cases: a conflict is retried with jittered backoff, any other query error is
raised immediately (retrying a rejected write is how one bad statement becomes
two), and a transport failure reconnects once.

### 13. SurrealDB 3.1.6 segfaults on one specific sequence

**The finding this suite exists to produce.** Reproducible:

1. restart the database while a browser holds live queries and reconverges;
2. immediately run an agent turn against it — hybrid recall plus a consolidate
   transaction.

The container exits **139 (SIGSEGV)**. Not OOM-killed (`OOMKilled: false`).

What does *not* reproduce it, each tried alone: restarting with no live queries;
restarting with live queries active; a KNN query immediately after restart; the
same agent turn without a preceding restart. Only the combination.

The chaos test is now ordered last, which a destructive test deserves regardless,
and the `connection` fixture reads the container's exit code so a crash is
reported as a crash — otherwise the symptom is eleven unrelated-looking fixture
errors and a message telling you to start a database that was already running.

This is recorded rather than worked around. It is a database bug, not an
application one, and the sequence above is the reproduction.

### 14. Two tests that were accidentally measuring table size

The live-query tests created scratch facts and then addressed them with
`WHERE text CONTAINS …`. At six facts that is instant; at 3,439 it is a full scan
that outlives the notification window. They began failing when the corpus grew,
which reads as a live-query bug and is a missing index. They address records by
explicit id now.

## Screenshots

Captured by the tests that assert the behaviour they depict, so an image cannot
drift from the thing it claims to show.

| File | Taken by |
|---|---|
| `screenshots/01-full-graph.png` | `test_the_browser_renders_the_whole_corpus` |
| `screenshots/02-superseded-history.png` | `test_superseded_facts_are_shown_dimmed_not_removed` |
| `screenshots/03-reconnecting.png` | `test_the_graph_reconverges_after_the_database_restarts`, mid-outage |
| `screenshots/04-reconverged.png` | the same test, after convergence |
| `screenshots/05-agent-conversation.png` | `test_a_conversation_becomes_memory_on_screen` |

---

## Still to build

Stated plainly rather than left as an implication:

- **L3 — retrieval quality.** The corpus already emits ground truth (34 queries
  tagged by the arm that should answer them). nDCG@10 and recall@10 per arm, and
  the depth-union expansion measured against the naive `.{1..2}` form, are not
  yet asserted.
- **L6 — property-based convergence.** The chaos test drives one scripted fault.
  The stronger version is a seeded random write program interleaved with random
  faults, run over many seeds, asserting the browser's graph always equals a
  fresh `SELECT`.
- **L7 — delivery latency under sustained load.** p50/p95/p99 from commit to
  notification, and the write rate at which a subscriber falls behind.
- **`huge` has not been run.** The 60k-fact figures in the scale table are
  projections. At 87 facts/s a full load is roughly ten minutes, and the browser
  is expected to struggle — that result belongs here once measured, not before.
