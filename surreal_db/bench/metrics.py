"""Pure-Python metrics for the M6 benchmark: no numpy, matching the project's
existing dependency set (see api/pyproject.toml).

Everything here is a fixed list in, a number out -- which is what makes it
testable without a database, same reasoning as api/cortex/retrieval.py's fuse().
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method)."""
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * (p / 100)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return data[int(rank)]
    return data[lower] + (data[upper] - data[lower]) * (rank - lower)


def dcg(relevances: list[float]) -> float:
    """Discounted cumulative gain, position 1-indexed inside the log discount."""
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(relevances))


def ndcg_at_k(ranked_ids: list[str], relevant: set[str], k: int = 10) -> float:
    """Normalized DCG@k: 1.0 is a perfect ranking, 0.0 is nothing relevant retrieved."""
    relevances = [1.0 if fid in relevant else 0.0 for fid in ranked_ids[:k]]
    ideal_count = min(len(relevant), k)
    ideal = [1.0] * ideal_count + [0.0] * (k - ideal_count)
    idcg = dcg(ideal)
    return dcg(relevances) / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int = 10) -> float:
    """Fraction of the relevant set found in the top k."""
    if not relevant:
        return 0.0
    return len(set(ranked_ids[:k]) & relevant) / len(relevant)


def median_and_variance(values: list[float]) -> tuple[float, float]:
    """Median and (max-min) spread across repeated runs, for the doc's '3 runs, median
    reported, variance shown' rule."""
    if not values:
        return 0.0, 0.0
    data = sorted(values)
    mid = len(data) // 2
    med = data[mid] if len(data) % 2 else (data[mid - 1] + data[mid]) / 2
    return med, max(data) - min(data)


def data_access_loc(paths: list[Path]) -> int:
    """Lines of data-access code, via `cloc` if installed, else a stdlib fallback
    that counts non-blank lines -- close enough for a relative comparison between
    the two arms, which is all metric 4 needs."""
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return 0
    try:
        result = subprocess.run(
            ["cloc", "--json", *existing], capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            return int(data.get("SUM", {}).get("code", 0))
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    total = 0
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                total += 1
    return total


def demo() -> None:
    """Assert-based self-check: the smallest thing that fails if this math breaks."""
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 90) == 10
    assert percentile([], 50) == 0.0

    # Perfect ranking: all relevant docs first -> nDCG 1.0.
    assert abs(ndcg_at_k(["a", "b", "c"], {"a", "b", "c"}, k=3) - 1.0) < 1e-9
    # Nothing relevant retrieved -> 0.0.
    assert ndcg_at_k(["x", "y"], {"a"}, k=10) == 0.0
    # Relevant doc in position 2 scores between 0 and a perfect rank-1 score.
    mid = ndcg_at_k(["x", "a"], {"a"}, k=10)
    assert 0 < mid < 1.0

    assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=10) == 1.0
    assert recall_at_k(["a"], {"a", "b"}, k=10) == 0.5
    assert recall_at_k(["a"], set(), k=10) == 0.0

    assert median_and_variance([1, 2, 3]) == (2, 2)
    assert median_and_variance([5, 1]) == (3, 4)

    print("bench/metrics.py: all checks passed")


if __name__ == "__main__":
    demo()
