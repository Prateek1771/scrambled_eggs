// Arm B / Neo4j: associative traversal only. Fact nodes carry `text` too
// (denormalized from Postgres) so a graph-arm hit needs no fourth round trip
// to hydrate its text -- keeping Arm B at exactly 3 round trips per recall,
// same reasoning as db/schema.surql keeping everything in one engine.

CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT fact_id IF NOT EXISTS FOR (f:Fact) REQUIRE f.id IS UNIQUE;
CREATE INDEX fact_current IF NOT EXISTS FOR (f:Fact) ON (f.valid_to);
