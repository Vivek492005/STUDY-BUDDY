"""
Mock exams from the study library — built on the flashcard generators,
not a second question engine.

An exam is a mix of the deck's own card kinds, ordered auto-gradeable
first: MCQ, then cloze, then short-answer (QA) prompts. MCQ and cloze
are graded by the server; QA prompts are returned with their model
answer and page for self-check and are never auto-scored.

HONESTY NOTE: this is a self-test, not a proctored exam. The generate
response carries the answer key (like flashcards, which show answers),
and the grade endpoint trusts the exam the client sends back. Grade
honestly. A document too thin to yield qualified key terms produces
fewer questions — or none, in which case the route says so plainly
instead of inventing questions.
"""

import re
from types import SimpleNamespace

from studybuddy.flashcards import generate_deck

MAX_QUESTIONS = 30
DEFAULT_QUESTIONS = 10


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _order_questions(cards):
    """Auto-gradeable first (mcq, cloze), then short-answer (qa)."""
    rank = {"mcq": 0, "cloze": 1, "qa": 2}
    return sorted(cards, key=lambda c: (rank.get(c.get("kind"), 3),
                                        c.get("id") or ""))


def _exam_payload(title, cards):
    questions = []
    for card in cards:
        q = {"id": card.get("id"), "kind": card.get("kind"),
             "question": card.get("question"),
             "options": list(card.get("options") or []),
             "page": card.get("page"),
             "doc_title": card.get("doc_title")}
        if card.get("kind") == "mcq":
            q["correct_index"] = card.get("correct_index")
        q["answer"] = card.get("answer")
        questions.append(q)
    return {"title": title, "total": len(questions), "questions": questions,
            "note": ("Self-test — the key travels with the exam, like "
                     "flashcards. Grade honestly.")}


def build_exam_from_doc(doc, n=DEFAULT_QUESTIONS):
    """Mock exam from one StudyDoc (duck-typed: .data['chunks'], .title)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_QUESTIONS
    n = max(1, min(n, MAX_QUESTIONS))
    deck = generate_deck(doc, max_per_kind=MAX_QUESTIONS)
    cards = _order_questions(deck.get("cards") or [])[:n]
    title = getattr(doc, "title", "") or "Document"
    return _exam_payload("Mock exam: %s" % title, cards)


def build_exam_from_docs(docs, n=DEFAULT_QUESTIONS):
    """Mock exam across several docs, round-robin so no one doc dominates.

    Each card keeps its own doc's title, so the re-read list can point
    back to the exact document and page.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_QUESTIONS
    n = max(1, min(n, MAX_QUESTIONS))
    per_doc = []
    for doc in (docs or []):
        deck = generate_deck(doc, max_per_kind=MAX_QUESTIONS)
        per_doc.append(_order_questions(deck.get("cards") or []))
    cards = []
    idx = 0
    while len(cards) < n:
        progressed = False
        for deck_cards in per_doc:
            if len(cards) >= n:
                break
            if idx < len(deck_cards):
                cards.append(deck_cards[idx])
                progressed = True
        if not progressed:
            break
        idx += 1
    return _exam_payload("Mock exam: your library", cards)


def build_exam_from_chunks(chunks, title, n=DEFAULT_QUESTIONS):
    """Convenience wrapper for callers holding raw chunks, not a doc."""
    doc = SimpleNamespace(data={"chunks": list(chunks or [])}, title=title)
    return build_exam_from_doc(doc, n)


def grade_exam(questions, answers):
    """Score an exam. ``answers`` maps question id -> MCQ index (int) or
    cloze text (str). QA prompts are never auto-scored.

    Returns {auto_total, auto_correct, score_pct, results, reread} where
    reread lists the (doc, page) pairs behind wrong auto-graded answers
    plus every QA prompt's source, so the student knows what to re-read.
    """
    if not isinstance(questions, list):
        questions = []
    if not isinstance(answers, dict):
        answers = {}
    results = []
    auto_total = 0
    auto_correct = 0
    reread_map = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        kind = q.get("kind")
        given = answers.get(qid)
        entry = {"id": qid, "kind": kind, "page": q.get("page"),
                 "doc_title": q.get("doc_title"),
                 "your_answer": given, "expected": q.get("answer"),
                 "correct": None}
        if kind == "mcq":
            auto_total += 1
            try:
                correct = int(given) == int(q.get("correct_index"))
            except (TypeError, ValueError):
                correct = False
            entry["correct"] = correct
            if correct:
                auto_correct += 1
            else:
                reread_map[(q.get("doc_title"), q.get("page"))] = True
        elif kind == "cloze":
            auto_total += 1
            correct = _norm(given) == _norm(q.get("answer")) and bool(_norm(given))
            entry["correct"] = correct
            if correct:
                auto_correct += 1
            else:
                reread_map[(q.get("doc_title"), q.get("page"))] = True
        else:  # qa: self-check only, source page joins the re-read list
            reread_map[(q.get("doc_title"), q.get("page"))] = True
        results.append(entry)
    reread = [{"doc_title": doc, "page": page}
              for (doc, page) in sorted(reread_map, key=lambda t: (str(t[0]), t[1] or 0))]
    score_pct = round(100.0 * auto_correct / auto_total, 1) if auto_total else None
    return {"auto_total": auto_total, "auto_correct": auto_correct,
            "score_pct": score_pct, "results": results, "reread": reread}
