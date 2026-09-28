"""Orchestrates the M6 benchmark end to end: seeds both arms, runs the query
set, the write-latency sample and the consistency test at 1/10/50 concurrency,
computes quality metrics, and writes the results into docs/06-benchmark.md.

`make bench-quick` runs this at `--scale tiny --runs 1` to validate the harness
cheaply; `make bench` runs the full `--scale bench --runs 3` per
docs/06-benchmark.md. Both assume the Makefile has already brought up
`surrealdb` (root docker-compose.yml) and Arm B's four stores
(bench/docker-compose.yml).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "api"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "db"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus as corpus_module  # noqa: E402

import arm_a  # noqa: E402
import embeddings as embeddings_module  # noqa: E402
import metrics  # noqa: E402
from arm_b import consistency as arm_b_consistency  # noqa: E402
from arm_b import pool as arm_b_pool  # noqa: E402
from arm_b import retrieval as arm_b_retrieval  # noqa: E402
from arm_b import writer as arm_b_writer  # noqa: E402
import consistency as consistency_module  # noqa: E402

from cortex.config import Settings  # noqa: E402
from cortex.db import Database  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SEED = 7


# --------------------------------------------------------------------------
# per-run benchmarks
# --------------------------------------------------------------------------


async def _run_arm_a(corpus: corpus_module.Corpus, fact_embeddings: dict,
                      query_embeddings: list, write_samples: int) -> dict:
    os.environ["SURREAL_NS"] = "cortex_bench"
    os.environ["SURREAL_DB"] = "bench"
    surreal_http = os.environ.get("SURREAL_HTTP_BENCH", "http://localhost:8000")

    print("  [A] applying schema to cortex_bench/bench ...")
    arm_a.apply_bench_schema(
        surreal_http,
        root_user=os.environ.get("SURREAL_ROOT_USER", "root"),
        root_pass=os.environ.get("SURREAL_ROOT_PASS", "root"),
    )

    print("  [A] seeding SurrealDB ...")
    load_timings = arm_a.seed_database(
        corpus, fact_embeddings,
        endpoint=surreal_http, namespace="cortex_bench", database="bench",
        user=os.environ.get("SURREAL_ROOT_USER", "root"),
        password=os.environ.get("SURREAL_ROOT_PASS", "root"),
    )
    print(f"  [A] loaded: {load_timings}")

    print("  [A] querying + write-latency sample ...")
    result = await arm_a.run(corpus, query_embeddings, write_samples=write_samples)
    result["load_timings"] = load_timings
    return result


async def _run_arm_b(corpus: corpus_module.Corpus, fact_embeddings: dict,
                      query_embeddings: list, write_samples: int) -> dict:
    pools = await arm_b_pool.connect()
    try:
        print("  [B] applying schema + resetting ...")
        await arm_b_pool.apply_schema(pools)
        await _reset_arm_b(pools)

        print("  [B] bulk-loading ...")
        load_timings = await arm_b_writer.bulk_load(pools, corpus, fact_embeddings, progress=True)
        print(f"  [B] loaded: {load_timings}")

        print("  [B] querying ...")
        query_results = []
        for query, embedding in zip(corpus.queries, query_embeddings):
            started = time.perf_counter()
            hits, round_trips = await arm_b_retrieval.recall(pools, embedding, query.question)
            latency_ms = (time.perf_counter() - started) * 1000
            query_results.append({
                "fact_ids": [hit.fact_id for hit in hits],
                "latency_ms": latency_ms,
                "round_trips": round_trips,
            })

        print("  [B] write-latency sample ...")
        write_latencies = await _benchmark_writes_arm_b(pools, corpus, n=write_samples)

        return {"queries": query_results, "write_latencies_ms": write_latencies,
                "load_timings": load_timings}
    finally:
        await pools.close()


async def _reset_arm_b(pools: arm_b_pool.ArmBPools) -> None:
    async with pools.pg.acquire() as conn:
        await conn.execute(
            "TRUNCATE fact, entity, mentions, relates, supersedes RESTART IDENTITY CASCADE")
    async with pools.neo4j.session() as session:
        result = await session.run("MATCH (n) DETACH DELETE n")
        await result.consume()
    if await pools.es.indices.exists(index="fact"):
        await pools.es.indices.delete(index="fact")
    mapping = json.loads((Path(__file__).resolve().parent / "arm_b" / "es_mapping.json")
                         .read_text(encoding="utf-8"))
    await pools.es.indices.create(index="fact", **mapping)


async def _benchmark_writes_arm_b(pools: arm_b_pool.ArmBPools, corpus: corpus_module.Corpus,
                                   n: int, seed: int = SEED) -> list[float]:
    import random

    from surreal_http import embedding as fake_embedding

    rng = random.Random(seed)
    latencies = []
    for i in range(n):
        sample = rng.sample(corpus.entities, k=min(2, len(corpus.entities)))
        fact_id = f"bench_write_{i}"
        started = time.perf_counter()
        await arm_b_writer.consolidate_fact(
            pools, fact_id=fact_id,
            text=f"bench synthetic fact {i}: {sample[0].name} did something noteworthy",
            embedding=fake_embedding(900_000 + i), confidence=0.8,
            entities=[(e.slug, e.name, e.kind) for e in sample],
        )
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


# --------------------------------------------------------------------------
# consistency test, both arms, at 1/10/50 concurrency
# --------------------------------------------------------------------------


async def _consistency_arm_a(concurrency: int, cycles: int) -> consistency_module.ConsistencyResult:
    settings = Settings.from_env()
    database = Database(settings)
    await database.connect()
    try:
        seed_slug = f"consistency_seed_a_c{concurrency}"
        await arm_a.create_seed_fact(database, seed_slug)
        return await consistency_module.run_consistency_test(
            supersede=lambda old, new: arm_a.supersede_probe(database, old, new),
            check_torn=lambda old, new: arm_a.check_torn(database, old, new),
            seed_fact_id=seed_slug, concurrency=concurrency, cycles=cycles,
        )
    finally:
        await database.close()


async def _consistency_arm_b(concurrency: int, cycles: int) -> consistency_module.ConsistencyResult:
    pools = await arm_b_pool.connect()
    try:
        seed_id = f"consistency_seed_b_c{concurrency}"
        await arm_b_consistency.create_seed_fact(pools, seed_id)
        return await consistency_module.run_consistency_test(
            supersede=lambda old, new: arm_b_consistency.supersede(pools, old, new),
            check_torn=lambda old, new: arm_b_consistency.check_torn(pools, old, new),
            seed_fact_id=seed_id, concurrency=concurrency, cycles=cycles,
        )
    finally:
        await pools.close()


# --------------------------------------------------------------------------
# footprint
# --------------------------------------------------------------------------

ARM_A_SERVICES = ["surrealdb"]
ARM_B_SERVICES = ["postgres", "neo4j", "elasticsearch", "redis"]


def _docker_stats(container_names: list[str]) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", *container_names],
            capture_output=True, text=True, timeout=30,
        )
        stats = {}
        for line in result.stdout.strip().splitlines():
            if "\t" in line:
                name, mem = line.split("\t", 1)
                stats[name] = mem
        return stats
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}


def _cold_start_seconds(compose_args: list[str], services: list[str]) -> float:
    """Stop then start a stack, timing until `--wait` reports healthy. Data
    volumes are untouched (`stop`, not `down -v`), so this can run after
    seeding without losing anything."""
    subprocess.run(["docker", "compose", *compose_args, "stop", *services],
                    capture_output=True, timeout=120)
    started = time.perf_counter()
    subprocess.run(["docker", "compose", *compose_args, "up", "-d", "--wait", *services],
                    capture_output=True, timeout=300)
    return time.perf_counter() - started


def _data_access_loc() -> dict[str, int]:
    arm_a_files = [REPO_ROOT / "api" / "cortex" / f for f in ("db.py", "retrieval.py", "consolidate.py")]
    arm_b_files = sorted((REPO_ROOT / "bench" / "arm_b").glob("*.py"))
    return {
        "arm_a": metrics.data_access_loc(arm_a_files),
        "arm_b": metrics.data_access_loc(arm_b_files),
    }


def _environment_info() -> dict:
    import platform

    def _sh(*args: str) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown"

    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "docker_version": _sh("docker", "version", "--format", "{{.Server.Version}}"),
        "git_sha": _sh("git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"),
    }


# --------------------------------------------------------------------------
# quality metrics
# --------------------------------------------------------------------------


def _bare_id(fact_id: str) -> str:
    """Strip a `table:` prefix. Arm A's hits carry SurrealDB's full record id
    (`fact:f0000123`); Arm B's carry the bare slug already (`f0000123`), which is
    also exactly what `GroundTruth.expected` (tests/corpus.py) holds -- so both
    arms compare on the same bare form."""
    return fact_id.split(":", 1)[-1] if ":" in fact_id else fact_id


def _quality(query_results: list[dict], corpus: corpus_module.Corpus) -> dict[str, float]:
    ndcgs, recalls = [], []
    for result, query in zip(query_results, corpus.queries):
        relevant = set(query.expected)
        ranked = [_bare_id(fact_id) for fact_id in result["fact_ids"]]
        ndcgs.append(metrics.ndcg_at_k(ranked, relevant, k=10))
        recalls.append(metrics.recall_at_k(ranked, relevant, k=10))
    n = max(len(ndcgs), 1)
    return {"ndcg_at_10": sum(ndcgs) / n, "recall_at_10": sum(recalls) / n}


# --------------------------------------------------------------------------
# docs/06-benchmark.md table filling
# --------------------------------------------------------------------------


def _fill_table(doc_text: str, heading: str, rows: list[tuple[str, str, str]]) -> str:
    """Replace the fenced code block right after `### {heading}` with a freshly
    formatted table. `rows` is (metric name, arm A value, arm B value)."""
    import re

    name_width = max(len(row[0]) for row in rows + [("Metric", "", "")])
    a_width = max(len(row[1]) for row in rows + [("", "CORTEX (SurrealDB)", "")])
    b_width = max(len(row[2]) for row in rows + [("", "", "Conventional stack")])

    lines = [f"{'Metric':<{name_width}} | {'CORTEX (SurrealDB)':<{a_width}} | Conventional stack"]
    lines.append("-" * name_width + "-|-" + "-" * a_width + "-|-" + "-" * b_width)
    for name, a_value, b_value in rows:
        lines.append(f"{name:<{name_width}} | {a_value:<{a_width}} | {b_value}")
    block = "\n".join(lines)

    pattern = re.compile(rf"(### {re.escape(heading)}\n\n```\n).*?(\n```)", re.DOTALL)
    replacement = rf"\g<1>{block}\g<2>"
    new_text, count = pattern.subn(replacement, doc_text)
    if count != 1:
        raise ValueError(f"could not find a unique '### {heading}' code block in docs/06-benchmark.md")
    return new_text


def _fmt(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}"


def write_results_doc(summary: dict) -> None:
    doc_path = REPO_ROOT / "docs" / "06-benchmark.md"
    text = doc_path.read_text(encoding="utf-8")

    lat = summary["latency"]
    text = _fill_table(text, "Latency", [
        ("Hybrid recall p50 (ms)", _fmt(lat["a"]["p50"]), _fmt(lat["b"]["p50"])),
        ("Hybrid recall p95 (ms)", _fmt(lat["a"]["p95"]), _fmt(lat["b"]["p95"])),
        ("Hybrid recall p99 (ms)", _fmt(lat["a"]["p99"]), _fmt(lat["b"]["p99"])),
        ("Consolidate write p50 (ms)", _fmt(lat["a"]["write_p50"]), _fmt(lat["b"]["write_p50"])),
        ("Round trips per retrieval", "1", "3"),
    ])

    fp = summary["footprint"]
    text = _fill_table(text, "Footprint", [
        ("Containers", "1", str(len(ARM_B_SERVICES))),
        ("Idle RSS (MB)", str(fp["idle_rss"].get("a", "n/a")), str(fp["idle_rss"].get("b", "n/a"))),
        ("Data-access LOC", str(fp["loc"]["arm_a"]), str(fp["loc"]["arm_b"])),
        ("Cold start to first query (s)", _fmt(fp["cold_start"]["a"]), _fmt(fp["cold_start"]["b"])),
    ])

    quality = summary["quality"]
    text = _fill_table(text, "Quality", [
        ("nDCG@10", _fmt(quality["a"]["ndcg_at_10"], 3), _fmt(quality["b"]["ndcg_at_10"], 3)),
        ("Recall@10", _fmt(quality["a"]["recall_at_10"], 3), _fmt(quality["b"]["recall_at_10"], 3)),
    ])

    # Consistency table has its own column layout (concurrency + a window column).
    import re

    consistency_lines = [
        "Concurrency | Arm A torn reads | Arm B torn reads | Arm B window (ms) p95",
        "------------|------------------|------------------|----------------------",
    ]
    for row in summary["consistency"]:
        consistency_lines.append(
            f"{row['concurrency']:<11} | {row['a_torn']:<16} | {row['b_torn']:<16} | "
            f"{_fmt(row['b_window_p95'])}"
        )
    consistency_block = "\n".join(consistency_lines)
    pattern = re.compile(r"(```\nConcurrency \| Arm A torn reads.*?\n```)", re.DOTALL)
    text, count = pattern.subn(f"```\n{consistency_block}\n```", text)
    if count != 1:
        raise ValueError("could not find the consistency table in docs/06-benchmark.md")

    doc_path.write_text(text, encoding="utf-8")
    print(f"wrote results into {doc_path}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


async def _one_run(corpus, fact_embeddings, query_embeddings, write_samples: int) -> dict:
    arm_a_result = await _run_arm_a(corpus, fact_embeddings, query_embeddings, write_samples)
    arm_b_result = await _run_arm_b(corpus, fact_embeddings, query_embeddings, write_samples)
    return {"a": arm_a_result, "b": arm_b_result}


async def _consistency_all(cycles: int, skip: bool) -> list[dict]:
    if skip:
        return []
    rows = []
    for concurrency in (1, 10, 50):
        print(f"  consistency @ concurrency={concurrency}")
        a_result = await _consistency_arm_a(concurrency, cycles)
        b_result = await _consistency_arm_b(concurrency, cycles)
        rows.append({
            "concurrency": concurrency,
            "a_torn": a_result.torn_reads,
            "b_torn": b_result.torn_reads,
            "b_window_p95": b_result.max_window_ms,  # single-run max stands in for p95 at this cycle count
        })
    return rows


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    # docker-compose.yml's SURREAL_URL (ws://surrealdb:8000/rpc) only resolves
    # inside the compose network. This script runs on the host, reaching the
    # same server through its published port instead.
    os.environ.setdefault("SURREAL_URL", "ws://localhost:8000/rpc")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", default="tiny", choices=sorted(corpus_module.SCALES))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--write-samples", type=int, default=200)
    parser.add_argument("--consistency-cycles", type=int, default=50)
    parser.add_argument("--skip-consistency", action="store_true")
    parser.add_argument("--idle-wait", type=float, default=300.0,
                        help="seconds to wait idle before measuring RSS (metric 5)")
    parser.add_argument("--skip-footprint", action="store_true")
    arguments = parser.parse_args()

    print(f"building corpus (scale={arguments.scale}, seed={SEED}) ...")
    corpus = corpus_module.build(arguments.scale, SEED)
    print(corpus.summary())
    from collections import Counter
    query_mix = dict(Counter(q.arm for q in corpus.queries))
    print(f"query mix (vector/text/graph/temporal): {query_mix}")

    print("computing/loading embeddings (OpenAI, cached) ...")
    fact_embeddings = embeddings_module.embed_corpus(corpus)
    query_embeddings = embeddings_module.embed_queries(corpus)

    run_results = []
    for run_index in range(arguments.runs):
        print(f"=== run {run_index + 1}/{arguments.runs} ===")
        run_results.append(asyncio.run(
            _one_run(corpus, fact_embeddings, query_embeddings, arguments.write_samples)))

    print("=== consistency test (1/10/50 concurrency) ===")
    consistency_rows = asyncio.run(
        _consistency_all(arguments.consistency_cycles, arguments.skip_consistency))

    # -- aggregate latency across runs: percentiles per run, median across runs --
    def _percentiles_per_run(arm: str) -> dict[str, list[float]]:
        out = {"p50": [], "p95": [], "p99": [], "write_p50": []}
        for run in run_results:
            latencies = [q["latency_ms"] for q in run[arm]["queries"]]
            out["p50"].append(metrics.percentile(latencies, 50))
            out["p95"].append(metrics.percentile(latencies, 95))
            out["p99"].append(metrics.percentile(latencies, 99))
            out["write_p50"].append(metrics.percentile(run[arm]["write_latencies_ms"], 50))
        return out

    def _median_each(values_by_metric: dict[str, list[float]]) -> dict[str, float]:
        return {k: metrics.median_and_variance(v)[0] for k, v in values_by_metric.items()}

    latency_summary = {
        "a": _median_each(_percentiles_per_run("a")),
        "b": _median_each(_percentiles_per_run("b")),
    }

    quality_summary = {
        "a": _quality(run_results[-1]["a"]["queries"], corpus),
        "b": _quality(run_results[-1]["b"]["queries"], corpus),
    }

    footprint_summary = {"idle_rss": {}, "loc": _data_access_loc(),
                         "cold_start": {"a": 0.0, "b": 0.0}}
    if not arguments.skip_footprint:
        print(f"=== footprint (idle wait {arguments.idle_wait:.0f}s) ===")
        footprint_summary["cold_start"]["a"] = _cold_start_seconds([], ARM_A_SERVICES)
        footprint_summary["cold_start"]["b"] = _cold_start_seconds(
            ["-f", "bench/docker-compose.yml"], ARM_B_SERVICES)
        time.sleep(arguments.idle_wait)
        stats = _docker_stats(ARM_A_SERVICES + ARM_B_SERVICES)
        footprint_summary["idle_rss"] = {
            "a": stats.get("surrealdb", "n/a"),
            "b": ", ".join(stats.get(name, "n/a") for name in ARM_B_SERVICES),
        }

    summary = {
        "scale": arguments.scale, "runs": arguments.runs, "seed": SEED,
        "corpus": corpus.summary(), "query_mix": query_mix,
        "environment": _environment_info(),
        "latency": latency_summary, "quality": quality_summary,
        "footprint": footprint_summary, "consistency": consistency_rows,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / f"{int(time.time())}_{arguments.scale}.json"
    result_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"raw results written to {result_path}")

    write_results_doc(summary)


if __name__ == "__main__":
    main()
