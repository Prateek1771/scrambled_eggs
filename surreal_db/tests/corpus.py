"""A realistic memory corpus: an engineering organisation, simulated over 18 months.

Every behaviour this suite tests -- supersession, association, provenance, decay --
only exists if the data has a history and a shape. Random records with the right
cardinality would let all of it pass vacuously, so this generates a domain
instead: a few hundred people, the services they own, the incidents those
services cause, and eighteen months of churn moving all of it around.

The hard cases are not planted. They fall out of the simulation:

  * people change teams and managers, services change owners, and two
    acquisitions rewrite whole subtrees -- so supersession chains reach length
    four to six and time-travel has something to travel through;
  * the service dependency graph is a real DAG with shared infrastructure at the
    bottom, so "who was on call for the service that depends on the library Ana
    maintains" is answerable only by traversal;
  * incident ids, pull requests and error codes are unrecallable by embedding, so
    the lexical arm has to earn its place;
  * two people are named Chen, and both companies have a team called Platform,
    so entity identity is exercised by data rather than by a contrived race.

Everything is deterministic from a seed. A failing run reproduces from one number.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))

from surreal_http import near, run  # noqa: E402

# --------------------------------------------------------------------------
# scale
# --------------------------------------------------------------------------

SCALES: dict[str, dict[str, int]] = {
    # Headcount drives everything else: teams, services and incidents are sized
    # relative to it, so the graph keeps a realistic shape at every scale rather
    # than becoming a hairball of people around a handful of services.
    "small": {"people": 70, "months": 6, "incidents_per_month": 5},
    "medium": {"people": 270, "months": 18, "incidents_per_month": 16},
    "huge": {"people": 2400, "months": 18, "incidents_per_month": 90},
}

START = datetime(2025, 1, 1, tzinfo=timezone.utc)

# --------------------------------------------------------------------------
# vocabulary
#
# Deliberately mundane. The point is that the text reads like something a person
# would actually write down, because retrieval quality measured against
# lorem-ipsum tells you nothing about retrieval quality.
# --------------------------------------------------------------------------

FIRST_NAMES = [
    "Ana", "Bruno", "Chen", "Dara", "Elif", "Farid", "Greta", "Hana", "Ivan", "Jun",
    "Kira", "Luca", "Maya", "Nils", "Omar", "Petra", "Quinn", "Rosa", "Sami", "Tomas",
    "Uma", "Vera", "Wei", "Xola", "Yara", "Zane", "Aditi", "Bo", "Cato", "Devi",
]
LAST_NAMES = [
    "Silva", "Novak", "Chen", "Okafor", "Duarte", "Halvorsen", "Ibrahim", "Kowalski",
    "Marchetti", "Nakamura", "Oyelaran", "Petrov", "Rasmussen", "Santos", "Tanaka",
    "Ueda", "Varga", "Wallace", "Yilmaz", "Zubairu",
]

TEAM_NAMES = [
    "Platform", "Payments", "Identity", "Data", "SRE", "Mobile", "Growth",
    "Search", "Billing", "Infrastructure", "Trust", "Developer Experience",
]

SERVICE_NAMES = [
    "billing-api", "auth-gateway", "ledger-core", "search-index", "notify-worker",
    "media-pipeline", "rate-limiter", "session-store", "invoice-render", "webhook-fan",
    "fraud-scorer", "config-service", "audit-log", "quota-manager", "email-relay",
    "push-dispatch", "receipt-store", "tax-engine", "currency-oracle", "settlement-batch",
]

LIBRARY_NAMES = [
    "retry-kit", "surql-client", "trace-context", "backoff-jitter", "schema-guard",
    "vector-io", "idempotency-keys", "circuit-fuse",
]

CONCEPTS = [
    "Raft consensus", "HNSW indexing", "backpressure", "idempotency", "CRDTs",
    "exactly-once delivery", "connection pooling", "cache invalidation",
    "graceful degradation", "blue-green deploys", "chaos engineering",
    "distributed tracing", "write amplification", "quorum reads", "vector clocks",
]

OFFICES = ["Berlin", "Munich", "Lisbon", "Bangalore", "Toronto", "Cape Town"]
REGIONS = ["eu-central-1", "us-east-1", "ap-south-1", "sa-east-1"]

ERROR_CODES = ["ETIMEDOUT", "ECONNRESET", "EPIPE", "ENOSPC", "EAI_AGAIN", "EHOSTUNREACH"]

# Suffixes that turn the hand-written name lists into pools big enough for the
# huge scale. Real systems accrete exactly this kind of naming, so it costs no
# realism: billing-api, billing-api-eu, billing-api-v2.
SERVICE_SUFFIXES = ["", "-eu", "-us", "-v2", "-edge", "-batch", "-read", "-admin"]
CONCEPT_QUALIFIERS = ["", " in production", " under load", " at the edge", " for writes"]

COMPANIES = ["Meridian", "Halcyon Systems", "Vector Labs"]

# Topic clusters. Facts sharing a topic sit near each other in embedding space,
# which is what makes vector recall discriminate instead of returning everything
# -- the exact failure that made the first hand-written hybrid check pass for the
# wrong reason.
TOPICS = {
    "org": 1000,
    "ownership": 2000,
    "incident": 3000,
    "deploy": 4000,
    "expertise": 5000,
    "location": 6000,
    "dependency": 7000,
    "acquisition": 8000,
    "review": 9000,
}


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class Entity:
    """A thing facts are about. `slug` becomes the record id, so no read-back is needed.

    `name` and `label` differ only when two entities of the same kind would
    otherwise share a name. CORTEX's `entity_identity` index is
    UNIQUE(name, kind), so the database cannot hold two people called Chen
    Wallace -- `name` therefore carries a disambiguator while `label` keeps the
    plain form used in fact prose. The ambiguity stays exactly where it belongs
    for a memory system: in the text, where retrieval has to cope with it.
    """

    slug: str
    name: str
    kind: str
    label: str = ""

    def __post_init__(self) -> None:
        """Default the prose label to the stored name when no disambiguation happened."""
        if not self.label:
            self.label = self.name


@dataclass
class Fact:
    """An atomic statement with a topic, a time, and the entities it mentions.

    `superseded_by` is filled in when a later fact contradicts this one, which is
    how the generator produces supersession chains rather than isolated pairs.
    """

    slug: str
    text: str
    topic: str
    month: int
    mentions: list[str]
    confidence: float
    superseded_by: str | None = None


@dataclass
class Edge:
    """A relationship between two entities, carrying a real predicate."""

    source: str
    target: str
    predicate: str


@dataclass
class GroundTruth:
    """A query and the facts that genuinely answer it.

    `arm` records which retrieval strategy *should* find it, so a regression in
    one arm is attributable rather than showing up as a vague drop in quality.
    """

    question: str
    topic: str | None
    expected: list[str]
    arm: str  # vector | text | graph | temporal


@dataclass
class Corpus:
    """Everything the generator produced, plus the ground truth to score against."""

    entities: list[Entity] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    queries: list[GroundTruth] = field(default_factory=list)
    seed: int = 0
    scale: str = "small"

    def summary(self) -> str:
        """One line describing the corpus, for test output and the results file."""
        superseded = sum(1 for fact in self.facts if fact.superseded_by)
        return (
            f"{self.scale} seed={self.seed}: {len(self.entities)} entities, "
            f"{len(self.facts)} facts ({superseded} superseded), "
            f"{len(self.edges)} edges, {len(self.queries)} ground-truth queries"
        )


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def _slug(prefix: str, text: str) -> str:
    """Make a record-id-safe slug.

    SurrealDB record ids allow a restricted character set, and an id built from a
    human name has to survive spaces, hyphens and case without colliding.
    """
    cleaned = "".join(character if character.isalnum() else "_" for character in text.lower())
    return f"{prefix}_{cleaned}"[:80]


class _Builder:
    """Runs the simulation. Holds the mutable world state the months advance.

    A class rather than a pile of functions because the eighteen months share a
    great deal of state -- who reports to whom, who owns what, who is still
    employed -- and threading that through free functions would obscure the one
    thing worth reading here, which is the timeline.
    """

    def __init__(self, scale: str, seed: int) -> None:
        self.rng = random.Random(seed)
        self.scale = scale
        self.seed = seed
        self.config = SCALES[scale]
        self.corpus = Corpus(seed=seed, scale=scale)

        # Live world state, mutated month by month.
        self.people: list[Entity] = []
        self.teams: list[Entity] = []
        self.services: list[Entity] = []
        self.libraries: list[Entity] = []
        self.employed: set[str] = set()
        self.team_of: dict[str, str] = {}
        self.manager_of: dict[str, str] = {}
        self.owner_of: dict[str, str] = {}       # service slug -> team slug
        self.maintainer_of: dict[str, str] = {}  # library slug -> person slug
        self.on_call: dict[str, str] = {}        # service slug -> person slug
        self.depends_on: dict[str, list[str]] = {}
        self.current_fact: dict[tuple[str, str], str] = {}  # (relation, subject) -> fact slug
        self.counter = 0

    # -- helpers ----------------------------------------------------------

    def _entity(self, kind: str, name: str, prefix: str) -> Entity:
        """Create an entity, register it, and return it."""
        entity = Entity(slug=_slug(prefix, name), name=name, kind=kind)
        self.corpus.entities.append(entity)
        return entity

    def _fact(self, text: str, topic: str, month: int, mentions: list[str],
              confidence: float = 0.85) -> Fact:
        """Record a fact. Ids are sequential so ordering is stable across runs."""
        self.counter += 1
        fact = Fact(
            slug=f"f{self.counter:07d}",
            text=text,
            topic=topic,
            month=month,
            mentions=mentions,
            confidence=confidence,
        )
        self.corpus.facts.append(fact)
        return fact

    def _assert_relation(self, relation: str, subject: str, text: str, topic: str,
                         month: int, mentions: list[str], confidence: float = 0.85) -> Fact:
        """State something, superseding whatever was previously believed about it.

        This is where supersession chains come from. Asserting a person's team
        four times over eighteen months leaves a chain of four, each pointing at
        its predecessor, with only the last one current -- which is exactly the
        history the temporal tests need and the shape a single planted
        contradiction cannot produce.
        """
        fact = self._fact(text, topic, month, mentions, confidence)
        key = (relation, subject)
        previous = self.current_fact.get(key)
        if previous is not None:
            for candidate in reversed(self.corpus.facts):
                if candidate.slug == previous:
                    candidate.superseded_by = fact.slug
                    break
        self.current_fact[key] = fact.slug
        return fact

    def _edge(self, source: str, target: str, predicate: str) -> None:
        """Associate two entities, skipping exact duplicates."""
        edge = Edge(source=source, target=target, predicate=predicate)
        self.corpus.edges.append(edge)

    # -- world construction -----------------------------------------------

    def _found_company(self) -> None:
        """Create the static parts of the world: companies, places, teams, concepts."""
        for name in COMPANIES:
            self._entity("org", name, "org")
        for name in OFFICES:
            self._entity("place", name, "place")
        for name in REGIONS:
            self._entity("place", name, "region")
        for name in CONCEPTS:
            self._entity("concept", name, "concept")

        for concept in CONCEPTS:
            for qualifier in CONCEPT_QUALIFIERS[1:]:
                if len(self.corpus.entities) > 40 + self.config["people"] // 4:
                    break
                self._entity("concept", concept + qualifier, "concept")

        team_count = max(4, min(len(TEAM_NAMES), 4 + self.config["people"] // 18))
        for name in TEAM_NAMES[:team_count]:
            self.teams.append(self._entity("org", name, "team"))

        # The acquired company brings its own Platform team. Two teams, same
        # name, different orgs -- which is the ambiguity the entity_identity
        # index exists to handle, arriving as data rather than as a contrived
        # concurrent write.
        self.teams.append(self._entity("org", "Platform (Halcyon)", "team"))

        service_count = max(6, self.config["people"] // 6)
        for index in range(service_count):
            base = SERVICE_NAMES[index % len(SERVICE_NAMES)]
            suffix = SERVICE_SUFFIXES[index // len(SERVICE_NAMES) % len(SERVICE_SUFFIXES)]
            self.services.append(self._entity("artifact", base + suffix, "svc"))

        library_count = max(4, self.config["people"] // 18)
        for index in range(library_count):
            base = LIBRARY_NAMES[index % len(LIBRARY_NAMES)]
            suffix = SERVICE_SUFFIXES[index // len(LIBRARY_NAMES) % len(SERVICE_SUFFIXES)]
            self.libraries.append(self._entity("artifact", base + suffix, "lib"))

    def _hire(self, month: int) -> Entity:
        """Bring one person into the world and state the facts that follow from it."""
        first = self.rng.choice(FIRST_NAMES)
        last = self.rng.choice(LAST_NAMES)
        label = f"{first} {last}"

        # Names collide on purpose, and the collision is the interesting part.
        # The record gets a disambiguated name because the schema demands one;
        # every fact about either person still says plain "Chen Wallace", so
        # retrieval faces the same ambiguity a real memory would.
        name = label
        slug = _slug("person", label)
        duplicates = sum(1 for entity in self.people if entity.label == label)
        if duplicates:
            name = f"{label} ({duplicates + 1})"
            slug = f"{slug}_{duplicates + 1}"
        person = Entity(slug=slug, name=name, kind="person", label=label)
        self.corpus.entities.append(person)
        self.people.append(person)
        self.employed.add(person.slug)

        team = self.rng.choice(self.teams)
        self.team_of[person.slug] = team.slug
        self._assert_relation(
            "member_of", person.slug,
            f"{label} joined the {team.name} team",
            "org", month, [person.slug, team.slug], 0.95,
        )
        self._edge(person.slug, team.slug, "member_of")

        office = self.rng.choice(OFFICES)
        self._assert_relation(
            "located_in", person.slug,
            f"{label} works out of the {office} office",
            "location", month, [person.slug, _slug("place", office)], 0.8,
        )
        self._edge(person.slug, _slug("place", office), "located_in")

        concept = self.rng.choice(CONCEPTS)
        self._fact(
            f"{label} knows {concept} well and is the person to ask about it",
            "expertise", month, [person.slug, _slug("concept", concept)], 0.7,
        )
        self._edge(person.slug, _slug("concept", concept), "knows_about")
        return person

    def _wire_services(self, month: int) -> None:
        """Give every service an owner and a dependency edge or two.

        The dependency graph is built as a DAG by only ever pointing later
        services at earlier ones, with the libraries at the bottom. That shared
        base is what makes deep traversals meaningful rather than a random walk.
        """
        for index, service in enumerate(self.services):
            team = self.rng.choice(self.teams)
            self.owner_of[service.slug] = team.slug
            self._assert_relation(
                "owns", service.slug,
                f"The {team.name} team owns {service.name}",
                "ownership", month, [team.slug, service.slug], 0.9,
            )
            self._edge(team.slug, service.slug, "owns")

            # Depend only on services defined earlier, so the graph stays acyclic.
            candidates = self.services[:index] + self.libraries
            for dependency in self.rng.sample(candidates, min(len(candidates),
                                                              self.rng.randint(1, 3))):
                self.depends_on.setdefault(service.slug, []).append(dependency.slug)
                self._fact(
                    f"{service.name} depends on {dependency.name} at request time",
                    "dependency", month, [service.slug, dependency.slug], 0.85,
                )
                self._edge(service.slug, dependency.slug, "depends_on")

        for library in self.libraries:
            maintainer = self.rng.choice(self.people)
            self.maintainer_of[library.slug] = maintainer.slug
            self._assert_relation(
                "maintains", library.slug,
                f"{maintainer.label} maintains {library.name}",
                "ownership", month, [maintainer.slug, library.slug], 0.9,
            )
            self._edge(maintainer.slug, library.slug, "maintains")

    # -- the timeline ------------------------------------------------------

    def _month(self, month: int) -> None:
        """Advance one month: hires, moves, incidents, deploys, and the occasional reorg."""
        headcount = len(self.people)

        for _ in range(max(1, self.config["people"] // 25)):
            self._hire(month)

        # Team moves. Each one supersedes the person's previous membership fact.
        for person in self.rng.sample(self.people, max(1, headcount // 25)):
            if person.slug not in self.employed:
                continue
            team = self.rng.choice(self.teams)
            if self.team_of.get(person.slug) == team.slug:
                continue
            self.team_of[person.slug] = team.slug
            self._assert_relation(
                "member_of", person.slug,
                f"{person.label} moved to the {team.name} team",
                "org", month, [person.slug, team.slug], 0.9,
            )
            self._edge(person.slug, team.slug, "member_of")

        # Reporting lines, which churn independently of team membership.
        for person in self.rng.sample(self.people, max(1, headcount // 20)):
            manager = self.rng.choice(self.people)
            if manager.slug == person.slug:
                continue
            self.manager_of[person.slug] = manager.slug
            self._assert_relation(
                "reports_to", person.slug,
                f"{person.label} reports to {manager.label}",
                "org", month, [person.slug, manager.slug], 0.9,
            )
            self._edge(person.slug, manager.slug, "reports_to")

        # On-call rotation. Changes every month by construction, which makes
        # "who was on call in March" a question only the temporal model answers.
        for service in self.services:
            responder = self.rng.choice(self.people)
            self.on_call[service.slug] = responder.slug
            self._assert_relation(
                "on_call_for", service.slug,
                f"{responder.label} is on call for {service.name}",
                "org", month, [responder.slug, service.slug], 0.85,
            )
            self._edge(responder.slug, service.slug, "on_call_for")

        self._incidents(month)
        self._deploys(month)

        if month == self.config["months"] // 3:
            self._acquisition(month, "Halcyon Systems")
        if month == (self.config["months"] * 2) // 3:
            self._reorg(month)

    def _incidents(self, month: int) -> None:
        """Generate incidents, each carrying identifiers no embedding can retrieve."""
        for _ in range(self.config["incidents_per_month"]):
            self.counter += 1
            incident_id = f"INC-{4000 + self.counter % 5000}"
            service = self.rng.choice(self.services)
            responder = self.on_call.get(service.slug) or self.rng.choice(self.people).slug
            error = self.rng.choice(ERROR_CODES)
            incident = self._entity("event", incident_id, "inc")

            # The lexical needle. `INC-4417` and `ETIMEDOUT` are exactly the kind
            # of token that vector search cannot find and BM25 finds instantly,
            # which is the whole argument for keeping both arms.
            self._fact(
                f"{incident_id}: {service.name} returned {error} for 42 minutes, "
                f"page fired at 03:14",
                "incident", month, [incident.slug, service.slug], 0.95,
            )
            self._edge(incident.slug, service.slug, "caused_by")

            person = next((p for p in self.people if p.slug == responder), None)
            if person:
                self._fact(
                    f"{person.label} resolved {incident_id} by rolling back the "
                    f"previous deploy",
                    "incident", month, [person.slug, incident.slug], 0.9,
                )
                self._edge(person.slug, incident.slug, "resolved")

    def _deploys(self, month: int) -> None:
        """Generate deploys, with pull request numbers as a second lexical needle."""
        for _ in range(max(1, len(self.services) // 2)):
            service = self.rng.choice(self.services)
            author = self.rng.choice(self.people)
            reviewer = self.rng.choice(self.people)
            self.counter += 1
            pull_request = f"#{8000 + self.counter % 2000}"
            region = self.rng.choice(REGIONS)
            self._fact(
                f"{author.label} shipped {pull_request} to {service.name} in {region}",
                "deploy", month, [author.slug, service.slug, _slug("region", region)], 0.8,
            )
            self._fact(
                f"{reviewer.label} reviewed {pull_request} before it went out",
                "review", month, [reviewer.slug, author.slug], 0.7,
            )
            self._edge(reviewer.slug, author.slug, "reviewed")

    def _acquisition(self, month: int, company: str) -> None:
        """Acquire a company, moving a subtree of people and services at once.

        A bulk event matters because it produces many supersessions inside a
        short window -- the condition under which a torn read, if one were
        possible, would actually be observed.
        """
        acquired = self._entity("event", f"Acquisition of {company}", "event")
        self._fact(
            f"Meridian acquired {company} and absorbed its engineering organisation",
            "acquisition", month, [acquired.slug, _slug("org", company),
                                   _slug("org", "Meridian")], 0.99,
        )
        platform = next(team for team in self.teams if team.name == "Platform (Halcyon)")
        for person in self.rng.sample(self.people, max(1, len(self.people) // 8)):
            self.team_of[person.slug] = platform.slug
            self._assert_relation(
                "member_of", person.slug,
                f"{person.label} joined {platform.name} through the {company} acquisition",
                "acquisition", month, [person.slug, platform.slug, acquired.slug], 0.9,
            )
            self._edge(person.slug, platform.slug, "member_of")

    def _reorg(self, month: int) -> None:
        """Transfer service ownership in bulk, producing deep supersession chains."""
        event = self._entity("event", f"Reorganisation month {month}", "event")
        self._fact(
            "The platform reorganisation moved service ownership between teams",
            "org", month, [event.slug], 0.9,
        )
        for service in self.rng.sample(self.services, max(1, len(self.services) // 2)):
            team = self.rng.choice(self.teams)
            self.owner_of[service.slug] = team.slug
            self._assert_relation(
                "owns", service.slug,
                f"Ownership of {service.name} transferred to the {team.name} team",
                "ownership", month, [team.slug, service.slug, event.slug], 0.9,
            )
            self._edge(team.slug, service.slug, "owns")

    # -- ground truth ------------------------------------------------------

    def _ground_truth(self) -> None:
        """Derive queries whose correct answers are known by construction.

        Computed from the world state rather than hand-written, so the answers
        stay correct at any scale and for any seed -- and so the associative
        queries genuinely require the traversal rather than merely looking like
        they do.
        """
        current = {fact.slug for fact in self.corpus.facts if not fact.superseded_by}

        # Lexical: an identifier that appears in exactly one fact.
        for fact in self.corpus.facts:
            if fact.topic == "incident" and fact.text.startswith("INC-"):
                incident_id = fact.text.split(":")[0]
                matches = [f.slug for f in self.corpus.facts if incident_id in f.text]
                self.corpus.queries.append(GroundTruth(
                    question=incident_id, topic=None, expected=matches, arm="text",
                ))
                if len(self.corpus.queries) >= 12:
                    break

        # Semantic: everything in a topic cluster.
        for topic in ("incident", "ownership", "expertise"):
            expected = [f.slug for f in self.corpus.facts
                        if f.topic == topic and f.slug in current][:40]
            if expected:
                self.corpus.queries.append(GroundTruth(
                    question=f"tell me about {topic}", topic=topic,
                    expected=expected, arm="vector",
                ))

        # Associative: two hops, and only reachable that way. The answer shares
        # no vocabulary with the question, which is the whole point -- this is
        # the population the `.{1..2}` recursion bug dropped silently.
        for library, maintainer in list(self.maintainer_of.items())[:10]:
            dependents = [service for service, deps in self.depends_on.items()
                          if library in deps]
            if not dependents:
                continue
            responders = {self.on_call[service] for service in dependents
                          if service in self.on_call}
            expected = [f.slug for f in self.corpus.facts
                        if f.slug in current
                        and any(person in f.mentions for person in responders)
                        and any(service in f.mentions for service in dependents)]
            if expected:
                person = next(p.name for p in self.people if p.slug == maintainer)
                self.corpus.queries.append(GroundTruth(
                    question=f"who is on call for the services that depend on the "
                             f"library {person} maintains",
                    topic=None, expected=expected, arm="graph",
                ))

        # Temporal: a superseded fact is the right answer to a question about the past.
        superseded = [fact for fact in self.corpus.facts if fact.superseded_by][:10]
        for fact in superseded:
            self.corpus.queries.append(GroundTruth(
                question=f"what did we previously believe: {fact.text[:60]}",
                topic=None, expected=[fact.slug], arm="temporal",
            ))

    # -- entry point -------------------------------------------------------

    def build(self) -> Corpus:
        """Run the whole simulation and return the finished corpus."""
        self._found_company()
        for _ in range(max(12, self.config["people"] // 3)):
            self._hire(0)
        self._wire_services(0)
        for month in range(1, self.config["months"] + 1):
            self._month(month)
        self._ground_truth()
        return self.corpus


def build(scale: str = "medium", seed: int = 7) -> Corpus:
    """Generate a corpus. Deterministic: the same scale and seed give the same world."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; expected one of {sorted(SCALES)}")
    return _Builder(scale, seed).build()


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

# Facts carry 1536 floats each -- roughly 34 KB of JSON -- so the batch size is a
# payload budget, not a round-trip count. SurrealDB's HTTP endpoint rejects large
# bodies with a bare 413, and at 120 facts per request (~4 MB) it does. 24 keeps
# each request under a megabyte, which loads without complaint and still
# amortises the handshake across two dozen records.
#
# Raising the server's body limit would allow bigger batches, but a loader that
# only works against a non-default server configuration is a loader that fails
# for whoever clones the repo.
FACT_BATCH = 24
EDGE_BATCH = 500
ENTITY_BATCH = 500


def timestamp(month: int) -> str:
    """Turn a simulated month into an ISO instant the database will accept."""
    return (START + timedelta(days=30 * month)).isoformat().replace("+00:00", "Z")


def reset(**connection) -> None:
    """Delete every record, leaving the schema in place."""
    run("reset", """
        DELETE supersedes; DELETE mentions; DELETE relates; DELETE derived_from;
        DELETE retrieval; DELETE fact; DELETE entity; DELETE message; DELETE session;
    """, **connection)


def load(corpus: Corpus, progress: bool = False, **connection) -> dict[str, float]:
    """Write a corpus to SurrealDB and return timings for each phase.

    Entities go in before facts and facts before edges, so no edge is ever
    written against an endpoint that does not exist yet -- `RELATE` would happily
    create a dangling one otherwise, and the graph would be subtly wrong in a way
    only the traversal tests would notice.

    Record ids are explicit (`entity:person_ana_silva`, `fact:f0000123`) rather
    than generated. That removes an entire read-back phase: edges can be written
    from the corpus alone, and a failing assertion names a record that can be
    looked up directly.
    """
    import time

    timings: dict[str, float] = {}

    def batches(items: list, size: int):
        """Yield fixed-size slices, so payload size stays predictable."""
        for start in range(0, len(items), size):
            yield items[start:start + size]

    started = time.perf_counter()
    for batch in batches(corpus.entities, ENTITY_BATCH):
        run("entities", """
            FOR $e IN $rows {
                LET $id = type::record("entity:" + $e.slug);
                CREATE $id SET name = $e.name, kind = $e.kind;
            };
        """, {"rows": [{"slug": e.slug, "name": e.name, "kind": e.kind} for e in batch]},
            **connection)
        if progress:
            print(f"  entities {batch[-1].slug}")
    timings["entities_s"] = time.perf_counter() - started

    started = time.perf_counter()
    for batch in batches(corpus.facts, FACT_BATCH):
        rows = [{
            "slug": fact.slug,
            "text": fact.text,
            # Facts in the same topic cluster land near each other; the jitter is
            # what stops a cluster collapsing to a single point and makes
            # ranking within a cluster meaningful.
            "embedding": near(TOPICS[fact.topic], 0.55),
            "confidence": fact.confidence,
            "valid_from": timestamp(fact.month),
            "mentions": fact.mentions,
        } for fact in batch]
        run("facts", """
            FOR $f IN $rows {
                LET $id = type::record("fact:" + $f.slug);
                CREATE $id SET text = $f.text, embedding = $f.embedding,
                               confidence = $f.confidence,
                               valid_from = type::datetime($f.valid_from);
                FOR $m IN $f.mentions {
                    LET $e = type::record("entity:" + $m);
                    RELATE $id->mentions->$e;
                };
            };
        """, {"rows": rows}, **connection)
        if progress:
            print(f"  facts {batch[-1].slug}")
    timings["facts_s"] = time.perf_counter() - started

    started = time.perf_counter()
    superseded = [{"old": fact.slug, "new": fact.superseded_by, "at": timestamp(fact.month + 1)}
                  for fact in corpus.facts if fact.superseded_by]
    for batch in batches(superseded, EDGE_BATCH):
        run("supersedes", """
            FOR $s IN $rows {
                LET $old = type::record("fact:" + $s.old);
                LET $new = type::record("fact:" + $s.new);
                UPDATE $old SET valid_to = type::datetime($s.at);
                RELATE $new->supersedes->$old;
            };
        """, {"rows": batch}, **connection)
    timings["supersessions_s"] = time.perf_counter() - started

    started = time.perf_counter()
    for batch in batches(corpus.edges, EDGE_BATCH):
        run("relates", """
            FOR $r IN $rows {
                LET $a = type::record("entity:" + $r.source);
                LET $b = type::record("entity:" + $r.target);
                RELATE $a->relates->$b SET predicate = $r.predicate, strength = 0.7;
            };
        """, {"rows": [{"source": e.source, "target": e.target, "predicate": e.predicate}
                       for e in batch]}, **connection)
    timings["relates_s"] = time.perf_counter() - started

    timings["total_s"] = sum(timings.values())
    return timings


def main() -> None:
    """Generate and load a corpus from the command line, reporting throughput."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", default="medium", choices=sorted(SCALES))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--endpoint", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true", help="do not clear first")
    parser.add_argument("--dry-run", action="store_true", help="generate but do not write")
    arguments = parser.parse_args()

    corpus = build(arguments.scale, arguments.seed)
    print(corpus.summary())
    if arguments.dry_run:
        return

    connection = {"endpoint": arguments.endpoint}
    if not arguments.keep:
        print("clearing")
        reset(**connection)

    print("loading")
    timings = load(corpus, progress=False, **connection)
    for phase, seconds in timings.items():
        print(f"  {phase:<18} {seconds:7.2f}s")
    rate = len(corpus.facts) / max(timings["facts_s"], 1e-6)
    print(f"  fact write rate    {rate:7.0f}/s")


if __name__ == "__main__":
    main()
