"""Real embeddings for the M6 benchmark, computed once and cached.

docs/06-benchmark.md: "Both arms use identical, pre-computed embeddings loaded
from a file ... No model is called during measurement." That applies to the
1000 query strings as much as to the 50k facts -- embedding a query live would
put an OpenAI round trip inside the timed section -- so both get embedded here,
once, and every other bench module only ever reads the cache this writes.
"""

from __future__ import annotations

import hashlib
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from cortex.config import Settings  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "data"
BATCH = 500  # request size, not a round-trip budget -- this only ever runs once per corpus.


def _hash(texts: list[str]) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _cache_paths(cache_name: str) -> tuple[Path, Path]:
    stem = CACHE_DIR / cache_name
    return stem.with_suffix(".bin"), stem.with_suffix(".keys.txt")


def _embed(items: list[tuple[str, str]], cache_name: str, settings: Settings) -> dict[str, list[float]]:
    """Embed (key, text) pairs, keyed by `key` in the returned dict. Cache-first."""
    vectors_path, keys_path = _cache_paths(cache_name)
    if vectors_path.exists() and keys_path.exists():
        keys = keys_path.read_text(encoding="utf-8").splitlines()
        raw = array("f")
        raw.frombytes(vectors_path.read_bytes())
        dim = settings.embed_dim
        if len(raw) != len(keys) * dim:
            raise ValueError(f"cache corrupt: {vectors_path}")
        return {key: raw[index * dim:(index + 1) * dim].tolist() for index, key in enumerate(keys)}

    from langchain_openai import OpenAIEmbeddings

    client = OpenAIEmbeddings(
        model=settings.embed_model, api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    keys = [key for key, _ in items]
    texts = [text for _, text in items]
    out: dict[str, list[float]] = {}
    for start in range(0, len(texts), BATCH):
        vectors = client.embed_documents(texts[start:start + BATCH])
        for key, vector in zip(keys[start:start + BATCH], vectors):
            out[key] = vector
        print(f"  embedded {min(start + BATCH, len(texts))}/{len(texts)} ({cache_name})")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw = array("f")
    for key in keys:
        raw.extend(out[key])
    vectors_path.write_bytes(raw.tobytes())
    keys_path.write_text("\n".join(keys) + "\n", encoding="utf-8")
    return out


def embed_corpus(corpus, settings: Settings | None = None) -> dict[str, list[float]]:
    """Return {fact slug: embedding}, cached per (scale, seed, fact texts)."""
    settings = settings or Settings.from_env()
    cache_name = f"facts_{corpus.scale}_{corpus.seed}_{_hash([f.text for f in corpus.facts])}"
    return _embed([(fact.slug, fact.text) for fact in corpus.facts], cache_name, settings)


def embed_queries(corpus, settings: Settings | None = None) -> list[list[float]]:
    """Return one embedding per `corpus.queries` entry, in order, cached the same way."""
    settings = settings or Settings.from_env()
    cache_name = f"queries_{corpus.scale}_{corpus.seed}_{_hash([q.question for q in corpus.queries])}"
    result = _embed([(str(i), q.question) for i, q in enumerate(corpus.queries)], cache_name, settings)
    return [result[str(i)] for i in range(len(corpus.queries))]


def main() -> None:
    """Precompute and cache embeddings for a scale/seed without running the rest of the benchmark."""
    import argparse

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    import corpus as corpus_module

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", default="tiny", choices=sorted(corpus_module.SCALES))
    parser.add_argument("--seed", type=int, default=7)
    arguments = parser.parse_args()

    built = corpus_module.build(arguments.scale, arguments.seed)
    print(built.summary())
    facts = embed_corpus(built)
    queries = embed_queries(built)
    print(f"{len(facts)} fact embeddings, {len(queries)} query embeddings ready")


if __name__ == "__main__":
    main()
