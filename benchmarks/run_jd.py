"""JD-match benchmark harness.

Scores every JD in benchmarks/jd_gold.json against its matched gold resume
(same role, strong) and its mismatched gold resume (different role, strong,
least skill overlap) using the production scorer
(studybuddy.jd_matcher.score_jd_match), and measures must-have skill recall
of the production skill extractor on the JD text itself.

Metrics (all honest, from the runs):
  - must_have_recall: mean fraction of human-defined must-have skills the
    production extractor finds in the JD text.
  - matched_score / mismatched_score: mean score_jd_match totals.
  - separation: matched_score - mismatched_score.
  - ranking_accuracy: fraction of JDs where matched > mismatched.

Usage:
    python benchmarks/run_jd.py [--quick] [--json]
Writes benchmarks/jd_results.json (full run) / prints the summary.

Exit code 0 when the benchmark completes; the numbers are reported, not
gated -- a low separation is a finding, not a crash.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.skill_extractor import extract_skills, get_skill_names  # noqa: E402
from parser.section_extractor import extract_sections  # noqa: E402
from parser.text_cleaning import extract_email, extract_phone  # noqa: E402
from studybuddy.jd_matcher import score_jd_match  # noqa: E402

HERE = Path(__file__).resolve().parent
JD_GOLD = HERE / "jd_gold.json"
GOLD_DIR = HERE / "resume_gold"
RESULTS = HERE / "jd_results.json"


def load_resumes():
    resumes = {}
    for path in sorted(GOLD_DIR.glob("syn_*.json")):
        item = json.loads(path.read_text())
        resumes[item["id"]] = item
    return resumes


def _resume_inputs(item):
    """Production-side inputs for score_jd_match from a gold resume."""
    text = item["text"]
    skills = get_skill_names(extract_skills(text))
    sections = extract_sections(text)
    contact = {"email": extract_email(text), "phone": extract_phone(text)}
    # experience_years lives at the gold-item level, not inside labels.
    return text, skills, item.get("experience_years", 0), sections, contact


def run(quick=False):
    jds = json.loads(JD_GOLD.read_text())
    if quick:
        jds = jds[:10]
    resumes = load_resumes()
    per_jd = []
    for jd in jds:
        jd_text = jd["text"]
        # 1. Must-have skill recall of the production extractor on the JD.
        jd_skills = {s.lower() for s in get_skill_names(extract_skills(jd_text))}
        must = jd["must_have_skills"]
        found = [s for s in must if s.lower() in jd_skills]
        recall = len(found) / len(must) if must else 1.0
        # 2. Matched vs mismatched resume scores (production scorer).
        matched = resumes[jd["matched_resume_id"]]
        mismatched = resumes[jd["mismatched_resume_id"]]
        m_text, m_sk, m_exp, m_sec, m_con = _resume_inputs(matched)
        x_text, x_sk, x_exp, x_sec, x_con = _resume_inputs(mismatched)
        m_rep = score_jd_match(m_text, m_sk, m_exp, m_sec, m_con, jd_text)
        x_rep = score_jd_match(x_text, x_sk, x_exp, x_sec, x_con, jd_text)
        per_jd.append({
            "id": jd["id"],
            "role": jd["role"],
            "level": jd["level"],
            "title": jd["title"],
            "must_have_recall": round(recall, 3),
            "must_have_found": found,
            "must_have_missing": [s for s in must if s.lower() not in jd_skills],
            "matched_resume_id": jd["matched_resume_id"],
            "matched_score": m_rep["score"],
            "mismatched_resume_id": jd["mismatched_resume_id"],
            "mismatched_score": x_rep["score"],
            "separation": round(m_rep["score"] - x_rep["score"], 1),
            "matched_breakdown": m_rep["breakdown"],
            "mismatched_breakdown": x_rep["breakdown"],
        })
    summary = {
        "n_jds": len(per_jd),
        "must_have_recall_mean": round(statistics.mean(
            p["must_have_recall"] for p in per_jd), 3),
        "must_have_recall_min": min(p["must_have_recall"] for p in per_jd),
        "matched_score_mean": round(statistics.mean(
            p["matched_score"] for p in per_jd), 1),
        "mismatched_score_mean": round(statistics.mean(
            p["mismatched_score"] for p in per_jd), 1),
        "separation_mean": round(statistics.mean(
            p["separation"] for p in per_jd), 1),
        "ranking_accuracy": round(sum(
            1 for p in per_jd if p["matched_score"] > p["mismatched_score"])
            / len(per_jd), 3),
        "per_jd": per_jd,
    }
    return summary


def main():
    ap = argparse.ArgumentParser(description="JD-match benchmark")
    ap.add_argument("--quick", action="store_true",
                    help="run on the first 10 JDs only")
    ap.add_argument("--json", action="store_true",
                    help="print the full JSON summary")
    args = ap.parse_args()
    summary = run(quick=args.quick)
    if not args.quick:
        RESULTS.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nWrote {RESULTS}")
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        s = summary
        print(f"n_jds: {s['n_jds']}")
        print(f"must_have_recall_mean: {s['must_have_recall_mean']} "
              f"(min {s['must_have_recall_min']})")
        print(f"matched_score_mean: {s['matched_score_mean']}")
        print(f"mismatched_score_mean: {s['mismatched_score_mean']}")
        print(f"separation_mean: {s['separation_mean']}")
        print(f"ranking_accuracy: {s['ranking_accuracy']}")
        worst = sorted(s["per_jd"], key=lambda p: p["separation"])[:5]
        print("lowest-separation JDs:")
        for p in worst:
            print(f"  {p['id']} {p['title']}: matched {p['matched_score']} vs "
                  f"mismatched {p['mismatched_score']} "
                  f"(sep {p['separation']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
