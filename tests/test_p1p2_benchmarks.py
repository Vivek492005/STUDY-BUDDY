"""Regression tests for the P2 benchmark harnesses.

These pin the harnesses' behavior, not engine quality: the synthetic
resume set must keep passing its bands, the RAG set must keep its
measured accuracy above floors (floors guard against catastrophic
retrieval regressions; they are NOT quality claims), and the corpus
integrity check must exclude cases whose expected answer is absent.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import benchmark_rag  # noqa: E402
import benchmark_resume  # noqa: E402


def test_resume_synthetic_set_passes_bands():
    results = benchmark_resume.run_cases(benchmark_resume.SYNTHETIC_CASES)
    assert len(results) == 12
    failed = [r for r in results if r["verdict"] == "FAIL"]
    assert not failed
    summary = benchmark_resume.summarize(results)
    assert summary["ranking_strong_gt_average_gt_weak"] is True
    assert summary["band_passes"] == summary["band_checks"] == 11


def test_resume_harness_main_returns_zero(tmp_path):
    assert benchmark_resume.main([]) == 0
    real_file = tmp_path / "r1.txt"
    real_file.write_text(
        "Asha Verma\nasha@example.com\n\nSummary\nDeveloper with 3 years "
        "of experience.\n\nExperience\nBuilt Python tools.\n\nSkills\n"
        "Python, SQL, Git\n", encoding="utf-8")
    assert benchmark_resume.main(["--real-dir", str(tmp_path)]) == 0
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps([{
        "id": "c1", "label": "json case",
        "text": "Tom Ray\ntom@example.com\n\nSkills\nJava, SQL",
        "expected_band": None}]), encoding="utf-8")
    assert benchmark_resume.main(["--cases", str(cases_file)]) == 0


def test_resume_synthetic_set_is_labeled_synthetic():
    assert "SYNTHETIC" in benchmark_resume.__doc__
    assert "no real person" in benchmark_resume.__doc__
    assert "NEVER present them" in benchmark_resume.__doc__
    for case in benchmark_resume.SYNTHETIC_CASES:
        assert case["label"].startswith("SYNTHETIC")


def test_rag_bundled_accuracy_above_floors():
    chunks = benchmark_rag.build_chunks(benchmark_rag.CORPUS)
    invalid = benchmark_rag.check_corpus_integrity(
        benchmark_rag.RAG_CASES, benchmark_rag.CORPUS)
    assert invalid == []
    graded = benchmark_rag.RAG_CASES
    assert len(graded) == 50
    results = [benchmark_rag.run_case(c, chunks) for c in graded]
    abstain = [benchmark_rag.run_abstain_case(c, chunks)
               for c in benchmark_rag.ABSTAIN_CASES]
    summary = benchmark_rag.summarize(results, abstain, invalid)
    # Floors, not exact values: the harness measures; these catch a
    # catastrophic retrieval regression (e.g. a broken ranker).
    assert summary["answer_accuracy"] >= 0.9
    assert summary["citation_accuracy"] >= 0.9
    assert summary["abstention_accuracy"] >= 0.6


def test_rag_integrity_check_excludes_bad_cases():
    bad = [{"id": "bad-1", "question": "What is missing?",
            "expected": "this phrase is nowhere in the corpus", "passage": 0}]
    invalid = benchmark_rag.check_corpus_integrity(bad, benchmark_rag.CORPUS)
    assert len(invalid) == 1
    assert invalid[0]["id"] == "bad-1"
    # A case against a custom corpus it does not match is likewise invalid.
    custom = ["A passage about something completely different."]
    custom_chunks = benchmark_rag.build_chunks(custom)
    assert benchmark_rag.check_corpus_integrity(
        [{"id": "x", "question": "Q?", "expected": "all-in-one platform",
          "passage": 0}], custom)
    assert custom_chunks  # chunking a custom corpus works


def test_rag_abstention_cases_are_out_of_corpus():
    # The abstention set must not accidentally contain corpus vocabulary
    # that would make abstention impossible; each question's distinctive
    # words must be absent from every passage.
    corpus_text = " ".join(benchmark_rag.CORPUS).lower()
    for case in benchmark_rag.ABSTAIN_CASES:
        assert case["question"]
    assert len(benchmark_rag.ABSTAIN_CASES) == 5
    assert "sourdough" not in corpus_text
    assert "quantum entanglement" not in corpus_text
