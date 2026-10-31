"""Rubric calibration report (issue #55 / GFI #6) — HYBRID.

Prints the human-labelled calibration set with three columns:

- legacy TF-IDF (before): the flag-off scorer, deterministic, no network.
- improved TF-IDF (hybrid fallback): studybuddy.rubric_tfidf — n-grams,
  sublinear TF, better stemming. Used when the flag is on but the API
  is unavailable. Deterministic, no network, no key.
- mocked API: a clearly-labelled TEST DOUBLE (hand-written concept
  groups, same as the calibration test). Proves the column FORMAT, not
  real embedding semantics — it is ALSO fooled by the scrambled pair.
- live Gemini: runs ONLY when GEMINI_API_KEY is configured; otherwise
  honestly reports "not configured". Never invents numbers.

Usage (from the repo root):
    PYTHONPATH=. .venv/bin/python scripts/rubric_calibration_report.py

The pairs live in tests/test_rubric_embeddings_calibration.py (PAIRS) so
the report and the tests can never drift apart.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from studybuddy.mock_interview import _key_point_similarity  # noqa: E402
from studybuddy.rubric_embeddings import (  # noqa: E402
    cosine_vectors,
    gemini_embed,
    memoized_embed,
)
from studybuddy.rubric_tfidf import (  # noqa: E402
    improved_key_point_similarity,
)
from test_rubric_embeddings_calibration import (  # noqa: E402
    PAIRS,
    _stub_embed,
)


def main() -> int:
    print("Rubric calibration — legacy TF-IDF vs improved TF-IDF "
          "vs Gemini API")
    print("=" * 92)
    embed = memoized_embed(gemini_embed)
    live = _key_configured()
    print(f"Gemini embeddings live: {live} "
          f"(set GEMINI_API_KEY to enable the live column)")
    print("mocked-API column uses the calibration test's TEST DOUBLE — "
          "format only, not real semantics.")
    print()
    print(f"{'pair':<20} {'judgment':<15} {'legacy':>7} {'improved':>9} "
          f"{'mocked':>8} {'live':>10}")
    print("-" * 92)
    for pair in PAIRS:
        legacy, _ = _key_point_similarity(
            [pair["key_point"]], pair["candidate"])
        improved, _ = improved_key_point_similarity(
            [pair["key_point"]], pair["candidate"])
        # mocked API via the test double (concept groups)
        va, vb = _stub_embed(pair["key_point"]), _stub_embed(pair["candidate"])
        mocked = f"{cosine_vectors(va, vb):.4f}"
        live_str = "n/a"
        if live:
            ea = embed(pair["key_point"])
            eb = embed(pair["candidate"])
            if ea and eb:
                live_str = f"{cosine_vectors(ea, eb):.4f}"
            else:
                live_str = "failed"
        print(f"{pair['id']:<20} {pair['judgment']:<15} {legacy:>7.4f} "
              f"{improved:>9.4f} {mocked:>8} {live_str:>10}")
    print()
    print("Judgments are human labels from the calibration file. Legacy and")
    print("improved TF-IDF are deterministic. Live Gemini numbers appear")
    print("only with a real key — run with GEMINI_API_KEY to fill that")
    print("column (see the follow-up note in the issue #55 report).")
    return 0


def _key_configured() -> bool:
    try:
        from studybuddy.ai_gateway import gemini_configured

        return bool(gemini_configured())
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
