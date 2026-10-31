"""Calibration set + wiring tests for rubric embeddings (issue #55 / GFI #6).

PAIRS are hand-written by a human (the author of this file) with explicit
human judgments — never generated-and-labelled-as-human, never scraped.
Each pair states what a human judge says about the candidate relative to
the key point; the tests then report what each signal actually scores.

Two columns exist in this file:
- TF-IDF (before): computed deterministically from the real rubric code
  (mock_interview._key_point_similarity). No network, no key needed.
- Embeddings (after): proven here only as WIRING, using a clearly-labelled
  TEST DOUBLE stub embedder below (hand-written concept groups). The stub
  is NOT Gemini and its numbers are NOT embedding scores — it exists to
  prove the flag-on path uses the embedder's values, labels them
  "embeddings", and falls back to TF-IDF when the embedder fails.
  Real Gemini before/after numbers come from scripts/rubric_calibration_report.py,
  which requires GEMINI_API_KEY (absent in this environment, so its
  embeddings column honestly reports "not configured" instead of numbers).

Judgment vocabulary: "paraphrase" (same meaning, different words),
"near-duplicate" (same meaning, mostly same words), "partial" (covers
part of the key point), "unrelated" (different meaning), "scrambled"
(same words, different meaning — TF-IDF is fooled by word order),
"no-answer" (empty candidate).
"""

from studybuddy.eng_interview_v2 import score_answer as score_answer_v2
from studybuddy.mock_interview import (
    _content_words,
    _key_point_similarity,
    _key_point_similarity_with_signal,
    score_answer,
)
from studybuddy.rubric_embeddings import (
    cosine_vectors,
    gemini_embed,
    get_rubric_embed,
)
from studybuddy.rubric_tfidf import (
    improved_key_point_similarity,
    improved_tfidf_cosine,
    stem_improved,
)

PAIRS = [
    {
        "id": "paraphrase-01",
        "key_point": "Use a hash map to count frequencies",
        "candidate": "Tally occurrences with a dictionary",
        "judgment": "paraphrase",
        "note": "Same meaning (count via key-value structure), zero shared "
                "content words after filtering/stemming — TF-IDF must be 0.",
    },
    {
        "id": "paraphrase-02",
        "key_point": "Explain binary search on a sorted array",
        "candidate": "Find an element in an ordered list by halving the range",
        "judgment": "paraphrase",
        "note": "Same algorithm, no shared content words — TF-IDF must be 0.",
    },
    {
        "id": "near-duplicate-01",
        "key_point": "Roll back the faulty deploy to restore service",
        "candidate": "We rolled back the faulty deploy and service was restored",
        "judgment": "near-duplicate",
        "note": "Same meaning, mostly same words — TF-IDF must be high.",
    },
    {
        "id": "unrelated-01",
        "key_point": "Use a hash map to count frequencies",
        "candidate": "The quarterly budget review approved hiring for two teams",
        "judgment": "unrelated",
        "note": "Different meaning, no shared words — every signal must be 0.",
    },
    {
        "id": "partial-01",
        "key_point": "Add database indexes to speed up slow queries",
        "candidate": "We added an index to the database and queries ran faster",
        "judgment": "partial",
        "note": "Covers the indexing idea, misses the speed-up framing — "
                "TF-IDF mid-range.",
    },
    {
        "id": "scrambled-01",
        "key_point": "Cache results to avoid repeated computation",
        "candidate": "Repeated computation avoids cached results",
        "judgment": "scrambled",
        "note": "Same words, flipped meaning — TF-IDF is fooled (high); a real "
                "embedding signal should score this below a true paraphrase.",
    },
    {
        "id": "paraphrase-03",
        "key_point": "Quantify the turnaround and recovery time",
        "candidate": "I measured how fast service came back",
        "judgment": "paraphrase",
        "note": "Same meaning (how fast recovery took), zero shared content "
                "words — TF-IDF must be 0.",
    },
    {
        "id": "no-answer-01",
        "key_point": "Describe the failing state concretely with specifics",
        "candidate": "",
        "judgment": "no-answer",
        "note": "Empty candidate — every signal must be 0.",
    },
]


# ---------------------------------------------------------------------------
# TEST DOUBLE — NOT Gemini. Hand-written concept groups, used ONLY to prove
# the flag-on path consumes the embedder's values (and labels them).
# ---------------------------------------------------------------------------
_CONCEPT_GROUPS = [
    {"hash", "map", "dictionary", "tally", "count", "frequency", "occurrenc"},
    {"binary", "search", "find", "element", "order", "sort", "array", "list",
     "halv", "range"},
    {"roll", "back", "faulty", "deploy", "restore", "restor", "service"},
    {"database", "index", "query", "speed", "slow", "add", "added", "ran",
     "faster"},
    {"cache", "cach", "result", "avoid", "repeat", "computa"},
    {"turnaround", "recovery", "time", "measur", "fast", "came"},
    {"quarter", "budget", "review", "approv", "hir", "teams"},
]


def _stub_embed(text):
    """Test-double embedder: 1.0 per hit concept group, else 0.0.

    Deliberately crude (bag-of-concepts) — it proves wiring, not
    semantics. In particular it is ALSO fooled by scrambled-01, exactly
    like TF-IDF; only real Gemini embeddings could distinguish that pair.
    """
    toks = set(_content_words(text))
    return [1.0 if toks & group else 0.0 for group in _CONCEPT_GROUPS]


def _failing_embed(text):
    raise RuntimeError("simulated gateway failure")


def _none_embed(text):
    return None


def _tfidf_for(pair):
    mean, _ = _key_point_similarity([pair["key_point"]], pair["candidate"])
    return mean


def _by_id(pid):
    return next(p for p in PAIRS if p["id"] == pid)


def test_calibration_tfidf_matches_human_judgments():
    """Before column: real TF-IDF scores from the rubric code."""
    assert _tfidf_for(_by_id("paraphrase-01")) == 0.0
    assert _tfidf_for(_by_id("paraphrase-02")) == 0.0
    assert _tfidf_for(_by_id("paraphrase-03")) == 0.0
    assert _tfidf_for(_by_id("near-duplicate-01")) > 0.5
    assert _tfidf_for(_by_id("unrelated-01")) == 0.0
    partial = _tfidf_for(_by_id("partial-01"))
    assert 0.1 < partial < 0.6
    assert _tfidf_for(_by_id("scrambled-01")) > 0.5
    assert _tfidf_for(_by_id("no-answer-01")) == 0.0


def test_embedding_path_uses_embedder_values_and_labels_them():
    """Wiring proof (test double): flag-on path consumes embedder output."""
    pair = _by_id("paraphrase-01")
    mean, _sims, signal = _key_point_similarity_with_signal(
        [pair["key_point"]], pair["candidate"], embed=_stub_embed)
    assert signal == "embeddings"
    # The stub sees both texts in the counting concept group -> cosine 1.
    assert mean == 1.0
    # And the TF-IDF value for the same pair is 0 — the two signals differ,
    # which is the whole point of the feature.
    assert _tfidf_for(pair) == 0.0
    scored = score_answer(
        {"id": "cal-01", "key_points": [pair["key_point"]]},
        pair["candidate"], embed=_stub_embed)
    assert scored["similarity_signal"] == "embeddings"


def test_embedding_failure_falls_back_to_improved_tfidf():
    """Fallback discipline: embedder failure -> improved TF-IDF, never
    500s, never blocks. (Hybrid 2026-10-31: the fallback is the improved
    scorer, not the legacy one.)"""
    pair = _by_id("paraphrase-01")
    improved, _ = improved_key_point_similarity(
        [pair["key_point"]], pair["candidate"])
    for bad in (_failing_embed, _none_embed):
        mean, _sims, signal = _key_point_similarity_with_signal(
            [pair["key_point"]], pair["candidate"], embed=bad)
        assert signal == "tfidf"
        assert mean == improved
        scored = score_answer(
            {"id": "cal-01", "key_points": [pair["key_point"]]},
            pair["candidate"], embed=bad)
        assert scored["similarity_signal"] == "tfidf"
        assert scored["similarity_method"] == "tfidf"
        assert scored["key_point_similarity"] == improved


def test_flag_off_output_is_byte_identical():
    """No embed passed -> output has exactly today's keys, no new ones."""
    scored = score_answer(
        {"id": "cal-01",
         "key_points": [_by_id("paraphrase-01")["key_point"]]},
        _by_id("paraphrase-01")["candidate"])
    assert "similarity_signal" not in scored
    assert set(scored) == {
        "question_id", "scores", "total", "key_points_hit",
        "key_points_total", "key_point_similarity", "key_point_similarities",
        "filler_hits", "word_count", "depth_signals", "edge_signals",
    }


def test_gemini_embed_returns_none_without_key_and_never_raises():
    """Graceful degradation: no key in this environment -> None, no raise."""
    assert gemini_embed("Use a hash map to count frequencies") is None
    assert gemini_embed("") is None


def test_get_rubric_embed_returns_none_when_flag_off():
    """Default flag state (OFF, no env) -> no embed callable, no network."""
    assert get_rubric_embed() is None


def test_cosine_vectors_bounds():
    assert cosine_vectors([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_vectors([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_vectors([], [1.0]) == 0.0
    assert 0.0 <= cosine_vectors([1.0, 1.0], [1.0, 0.0]) <= 1.0


def test_v2_signal_labels():
    """eng_interview_v2: embed used -> 'embeddings', failed -> 'tfidf',
    absent -> no key; engine stays rule-based (embed is not the LLM judge)."""
    pair = _by_id("paraphrase-01")
    question = "How would you count frequencies in a list?"
    res = score_answer_v2(question, pair["candidate"],
                          [pair["key_point"]], embed=_stub_embed)
    assert res["similarity_signal"] == "embeddings"
    assert res["similarity_method"] == "gemini"
    assert res["engine"] == "rule-based"
    res = score_answer_v2(question, pair["candidate"],
                          [pair["key_point"]], embed=_failing_embed)
    assert res["similarity_signal"] == "tfidf"
    assert res["similarity_method"] == "tfidf"
    res = score_answer_v2(question, pair["candidate"], [pair["key_point"]])
    assert "similarity_signal" not in res
    assert "similarity_method" not in res


# ---------------------------------------------------------------------------
# HYBRID tests (user decision 2026-10-31): improved TF-IDF fallback +
# explicit method field. The improved scorer must be genuinely better
# than the legacy one — these tests lock in the specific wins.
# ---------------------------------------------------------------------------

def _improved_for(pair):
    mean, _ = improved_key_point_similarity(
        [pair["key_point"]], pair["candidate"])
    return mean


def test_improved_tfidf_less_fooled_by_scrambled():
    """The headline improvement: n-gram order factor punishes scrambling.
    Legacy 0.6694 -> improved must be strictly lower."""
    legacy = _tfidf_for(_by_id("scrambled-01"))
    improved = _improved_for(_by_id("scrambled-01"))
    assert improved < legacy
    assert improved < 0.65


def test_improved_tfidf_near_duplicate_stays_high():
    """Better stemming must not cost us the easy wins: near-duplicates
    stay clearly above 0.5 (legacy 0.7168)."""
    assert _improved_for(_by_id("near-duplicate-01")) > 0.5


def test_improved_tfidf_paraphrase_limitation_is_honest():
    """Lexical methods cannot see paraphrase — the improved scorer must
    still score 0.0 here (documented limit; only the API path helps)."""
    for pid in ("paraphrase-01", "paraphrase-02", "paraphrase-03"):
        assert _improved_for(_by_id(pid)) == 0.0
    assert _improved_for(_by_id("unrelated-01")) == 0.0
    assert _improved_for(_by_id("no-answer-01")) == 0.0


def test_improved_tfidf_sublinear_tf_dampens_repetition():
    """Repeating a keyword 10x must not get 10x weight (anti-gaming)."""
    base = improved_tfidf_cosine("hash map", "we used a hash map")
    assert base > 0.0
    repeated = improved_tfidf_cosine(
        "hash map", "hash map hash map hash map hash map hash map "
        "hash map hash map hash map hash map hash map")
    # Sublinear: 10x repetition grows the score modestly, not 10x.
    assert repeated < base * 3.0


def test_stemmer_fixes_e_ending_verbs_and_doubles():
    """The legacy stemmer's e-ending/doubled-consonant bugs are fixed."""
    assert stem_improved("improved") == stem_improved("improve")
    assert stem_improved("cached") == stem_improved("cache")
    assert stem_improved("rolled") == stem_improved("roll")
    assert stem_improved("running") == stem_improved("run")
    assert stem_improved("stopped") == stem_improved("stop")
    assert stem_improved("frequencies") == stem_improved("frequency")


def test_method_field_distinguishes_gemini_from_tfidf():
    """similarity_method: 'gemini' when embeddings computed, 'tfidf'
    for any lexical path. Mirrors similarity_signal."""
    pair = _by_id("near-duplicate-01")
    scored = score_answer(
        {"id": "cal-01", "key_points": [pair["key_point"]]},
        pair["candidate"], embed=_stub_embed)
    assert scored["similarity_signal"] == "embeddings"
    assert scored["similarity_method"] == "gemini"
    scored = score_answer(
        {"id": "cal-01", "key_points": [pair["key_point"]]},
        pair["candidate"], embed=_none_embed)
    assert scored["similarity_signal"] == "tfidf"
    assert scored["similarity_method"] == "tfidf"


def test_get_rubric_embed_flag_on_no_key_returns_null_callable():
    """Flag on + no key -> callable returning None (not None itself), so
    callers take the flag-on path: improved TF-IDF with the 'tfidf'
    label, and no network attempt."""
    import studybuddy.rubric_embeddings as re_mod
    orig_enabled = re_mod.embeddings_enabled
    orig_get = re_mod.get_rubric_embed
    try:
        re_mod.embeddings_enabled = lambda: True
        # Force "no key" regardless of environment.
        import studybuddy.ai_gateway as gw
        orig_cfg = gw.gemini_configured
        gw.gemini_configured = lambda: False
        try:
            embed = re_mod.get_rubric_embed()
            assert embed is not None
            assert embed("some text") is None
            # End to end: the null callable drives the improved fallback.
            pair = _by_id("near-duplicate-01")
            mean, _sims, signal = _key_point_similarity_with_signal(
                [pair["key_point"]], pair["candidate"], embed=embed)
            assert signal == "tfidf"
            assert mean == _improved_for(pair)
        finally:
            gw.gemini_configured = orig_cfg
    finally:
        re_mod.embeddings_enabled = orig_enabled
        re_mod.get_rubric_embed = orig_get


def test_v2_improved_fallback_on_embed_failure():
    """eng_interview_v2: embed passed but failing -> improved TF-IDF
    (not the legacy lexical recall), labelled 'tfidf'."""
    from studybuddy.eng_interview_v2 import semantic_similarity
    pair = _by_id("near-duplicate-01")
    got = semantic_similarity(
        pair["key_point"], pair["candidate"], embed=_failing_embed)
    # Must be the improved value, deterministic and in range.
    assert 0.0 <= got <= 1.0
    # And it must differ from the legacy recall path on at least the
    # scrambled pair (n-gram order factor is new).
    scr = _by_id("scrambled-01")
    legacy_recall = semantic_similarity(
        scr["key_point"], scr["candidate"], embed=None)
    improved = semantic_similarity(
        scr["key_point"], scr["candidate"], embed=_failing_embed)
    assert improved != legacy_recall


def test_calibrated_cosine_baseline_shift():
    """calibrated_cosine: random baseline (~0.50) maps to 0, 1.0 stays 1."""
    from studybuddy.rubric_embeddings import calibrated_cosine
    # Vectors with cosine exactly at/below baseline -> 0
    va = [1.0, 0.0]
    vb = [1.0, 1.0]  # cosine = 1/sqrt(2) ~= 0.7071
    assert calibrated_cosine(va, vb) == round((0.7071 - 0.50) / 0.50, 4)
    # Orthogonal vectors (cosine 0) -> 0
    assert calibrated_cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    # Identical vectors -> 1.0
    assert calibrated_cosine([1.0, 2.0], [1.0, 2.0]) == 1.0
    # Empty vectors -> 0.0 (never raises)
    assert calibrated_cosine([], [1.0]) == 0.0


def test_calibrated_embedding_similarity_scramble_guard():
    """Severe word salad (high unigram, ~zero bigram recall) is scaled
    down; mild reorderings and paraphrases are unaffected."""
    from studybuddy.rubric_embeddings import calibrated_embedding_similarity
    va = [1.0] * 8
    vb = [0.95] * 8  # raw cosine ~1.0 -> calibrated 1.0
    kp = "Cache results to avoid repeated computation"
    severe = "computation repeated avoid to results Cache"
    got = calibrated_embedding_similarity(kp, severe, va, vb)
    assert got < 0.40, f"severe scramble should be penalized, got {got}"
    # Paraphrase (low unigram overlap) must NOT trigger the guard
    para = "Store outputs so you do not recompute them"
    got_para = calibrated_embedding_similarity(kp, para, va, vb)
    assert got_para == 1.0, f"paraphrase wrongly penalized: {got_para}"


def test_ngram_helpers():
    """unigram_recall / ngram_order_recall behave as documented."""
    from studybuddy.rubric_tfidf import ngram_order_recall, unigram_recall
    a = "quick brown fox jumps"
    assert unigram_recall(a, "quick brown fox") == 0.75
    assert ngram_order_recall(a, "quick brown fox runs") > 0.5
    assert ngram_order_recall(a, "fox brown quick jumps") == 0.0
    assert ngram_order_recall("hi", "hi there") == 1.0  # too short: 1.0
    assert unigram_recall("", "x") == 0.0
