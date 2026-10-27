"""DATA-04: Question bank validation tests."""
import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from validate_questions import (
    norm_stem,
    validate_aptitude,
    validate_interview,
    validate_skill_quiz,
)


class TestNormStem:
    def test_normalizes(self):
        assert norm_stem("What is 50% of 360?") == norm_stem("what is 50 of 360")
        assert norm_stem("  Hello   World! ") == "hello world"


class TestAptitude:
    def test_valid(self):
        q = [{"id": "apt-1", "category": "quant", "difficulty": "Easy",
              "statement": "What is 2+2?", "options": ["3", "4"], "answer": 1}]
        issues, _ = validate_aptitude(q)
        assert issues == []

    def test_duplicate(self):
        q = [
            {"id": "a1", "category": "q", "difficulty": "Easy",
             "statement": "What is the capital of France?",
             "options": ["London", "Paris"], "answer": 1},
            {"id": "a2", "category": "q", "difficulty": "Easy",
             "statement": "What is the capital of France!  ",
             "options": ["London", "Paris"], "answer": 1},
        ]
        issues, _ = validate_aptitude(q)
        assert any("duplicate" in i for i in issues)

    def test_missing_field(self):
        q = [{"id": "a1", "category": "q", "difficulty": "Easy",
              "statement": "Q?", "options": ["a"]}]  # no answer
        issues, _ = validate_aptitude(q)
        assert any("missing" in i and "answer" in i for i in issues)

    def test_answer_out_of_range(self):
        q = [{"id": "a1", "category": "q", "difficulty": "Easy",
              "statement": "Q?", "options": ["a", "b"], "answer": 5}]
        issues, _ = validate_aptitude(q)
        assert any("out of range" in i for i in issues)


class TestInterview:
    def test_duplicate(self):
        q = [
            {"question": "Tell me about yourself.", "category": "hr",
             "difficulty": "Easy"},
            {"question": "Tell me about yourself!", "category": "hr",
             "difficulty": "Easy"},
        ]
        issues, _ = validate_interview(q)
        assert any("duplicate" in i for i in issues)

    def test_company_claim_flagged(self):
        q = [{"question": "This was asked at Google last year. Solve it.",
              "category": "dsa", "difficulty": "Hard"}]
        issues, _ = validate_interview(q)
        assert any("company claim" in i for i in issues)


class TestSkillQuiz:
    def test_valid(self):
        q = [{"id": "sq-1", "skill": "Python", "difficulty": "Easy",
              "question": "What is list?", "options": ["a", "b"], "answer": 0}]
        issues, _ = validate_skill_quiz(q)
        assert issues == []


class TestRealBanks:
    def test_no_issues_in_shipped_banks(self):
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        for fname, validator in [
                ("aptitude_questions.json", validate_aptitude),
                ("interview_questions.json", validate_interview),
                ("skill_quizzes.json", validate_skill_quiz)]:
            path = os.path.join(data_dir, fname)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            issues, _ = validator(data)
            assert issues == [], f"{fname}: {issues[:5]}"

    def test_counts(self):
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        apt = json.load(open(os.path.join(data_dir, "aptitude_questions.json")))
        iv = json.load(open(os.path.join(data_dir, "interview_questions.json")))
        sq = json.load(open(os.path.join(data_dir, "skill_quizzes.json")))
        assert len(apt) == 815  # bank grew past the old 332 pin; 815 unique ids verified
        assert len(iv) == 1455  # bank grew past the old 354 pin; 1455 unique questions verified
        assert len(sq) == 660  # bank grew past the old 320 pin; 660 verified
