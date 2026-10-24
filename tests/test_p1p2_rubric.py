"""Regression tests for the lexical TF-IDF cosine key-point similarity
signal in studybuddy/mock_interview.py (P2 interview-rubric item).

The signal is LEXICAL (word-overlap statistics) — it is NOT embeddings
and must never be described as semantic similarity. These tests pin that
behavior: a paraphrase sharing no words scores 0, while an answer reusing
key-point vocabulary scores above 0.
"""
from studybuddy.mock_interview import (
    _key_point_similarity,
    _tfidf_cosine,
    score_answer,
)

_Q = {
    "id": "beh-001",
    "question": "Tell me about a time you fixed a production outage.",
    "key_points": [
        "Describe the failing state concretely with specifics",
        "List the specific actions you took to mitigate",
        "Quantify the turnaround and recovery time",
    ],
}

_STRONG = (
    "Last quarter our payment service started failing concretely: error rates "
    "spiked to 12% and checkout conversions dropped. I triaged the logs, "
    "identified a bad deploy, and rolled it back within 20 minutes. "
    "I then added a canary analysis step which reduced bad deploys by 80%. "
    "The turnaround was measured: error rates recovered to 0.1% and conversions "
    "rebounded fully. I also considered the trade-off between speed and safety, "
    "because rushing the rollback could have caused data loss. Edge cases like "
    "partial rollouts were validated with tests."
)


def test_tfidf_cosine_bounds_and_identity():
    assert _tfidf_cosine(["a"], ["a"]) == 1.0
    assert _tfidf_cosine(["alpha", "beta"], ["gamma", "delta"]) == 0.0
    assert _tfidf_cosine([], ["alpha"]) == 0.0
    assert 0.0 <= _tfidf_cosine(["alpha", "beta"], ["alpha", "gamma"]) <= 1.0


def test_paraphrase_with_no_shared_words_scores_zero():
    # Lexical-only proof: no shared vocabulary -> zero similarity, even
    # though the sentence is topically similar in meaning.
    sim, _ = _key_point_similarity(
        _Q["key_points"],
        "I handled a really tough situation at work and made everything "
        "better afterwards, learning a lot about teamwork.")
    assert sim == 0.0


def test_shared_vocabulary_scores_above_zero():
    sim, per_point = _key_point_similarity(
        _Q["key_points"],
        "The failing service recovered concretely: I mitigated the outage "
        "with specific actions and quantified the turnaround time.")
    assert sim > 0.0
    assert len(per_point) == 3
    assert all(0.0 <= s <= 1.0 for s in per_point)


def test_score_answer_reports_similarity_keys():
    scored = score_answer(_Q, _STRONG)
    assert "key_point_similarity" in scored
    assert "key_point_similarities" in scored
    assert 0.0 <= scored["key_point_similarity"] <= 1.0
    assert scored["total"] >= 55
    empty = score_answer(_Q, "")
    assert empty["key_point_similarity"] == 0.0
    assert empty["key_point_similarities"] == []
    assert empty["total"] == 0


def test_similarity_is_deterministic():
    first = score_answer(_Q, _STRONG)
    second = score_answer(_Q, _STRONG)
    assert first == second


def test_no_key_points_leaves_correctness_unchanged():
    # Mirrors coverage semantics: nothing to miss -> similarity 1.0, and
    # the bounded bonus (+2 max) still applies, capped at 40.
    scored = score_answer({"id": "x", "key_points": []}, _STRONG)
    assert scored["key_point_similarity"] == 1.0
    assert scored["scores"]["correctness"] <= 40.0
