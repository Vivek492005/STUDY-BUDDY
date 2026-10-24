"""Career Health Review (Wave 6 #30): a monthly, explainable review of direction,
proof, opportunity activity, preparation and wellbeing.

Design rules (from the spec):
- No opaque employability score; five transparent dimensions, each with a
  score (0-100) or None when there is not enough data, a trend vs last month,
  a confidence (0-100) driven by data volume, and human-readable evidence.
- Honest empty states: a locked dimension says exactly what would unlock it.
- The aggregators read EXISTING tables only — this feature creates no new
  activity, it only summarizes what is already there.
- Wellbeing respects momentum opt-out (opted out -> score None with an
  affirming note) and treats shields/rest as healthy, never as penalties.

Module layout (mirrors the codebase convention: feature modules stay free of
top-level Flask imports so there are no circular imports with unified_app):

- Pure rules & month math: DIMENSIONS, MONTH_KEY_RE, valid_month_key,
  current_month_key, prev_month_key, score clamping. Never raises.
- DB-backed aggregation: _agg_direction/_agg_proof/_agg_opportunity/
  _agg_preparation/_agg_wellbeing read unified_app's models lazily (inside
  the function). generate_review() builds the five dimensions, diffs against
  last month for trends, picks next_action, writes a narrative, and persists
  the HealthReview row idempotently. Individual aggregator failures degrade
  to a locked dimension with an honest note — never a raised exception,
  except ValueError for a malformed month_key (caller validates first).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

log = logging.getLogger(__name__)

#: "YYYY-MM", months 01-12 only.
MONTH_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: The five transparent dimensions: title + plain-language description.
DIMENSIONS = [
    {"id": "direction",
     "title": "Direction",
     "description": "Do you know where you're headed? Career assessment plus tracked goals."},
    {"id": "proof",
     "title": "Proof",
     "description": "Can you show what you can do? Resume strength, portfolio, certifications."},
    {"id": "opportunity",
     "title": "Opportunity activity",
     "description": "Are you in the market? Applications tracked and opportunities saved."},
    {"id": "preparation",
     "title": "Preparation",
     "description": "Are you interview-ready? Quizzes, DSA, mocks and aptitude practice."},
    {"id": "wellbeing",
     "title": "Wellbeing",
     "description": "Is your pace sustainable? Steady consistency, forgiveness, and rest."},
]
DIMENSION_IDS = [d["id"] for d in DIMENSIONS]


def valid_month_key(month_key: str) -> bool:
    """True iff month_key is a strict 'YYYY-MM' string."""
    return isinstance(month_key, str) and bool(MONTH_KEY_RE.match(month_key))


def current_month_key(now: datetime | None = None) -> str:
    """'YYYY-MM' for now (UTC)."""
    now = now or datetime.now(UTC)
    return "%04d-%02d" % (now.year, now.month)


def prev_month_key(month_key: str) -> str:
    """The month before a valid month_key ('2026-01' -> '2025-12')."""
    if not valid_month_key(month_key):
        raise ValueError("month_key must be 'YYYY-MM', got %r" % (month_key,))
    year, month = int(month_key[:4]), int(month_key[5:7])
    if month == 1:
        return "%04d-12" % (year - 1)
    return "%04d-%02d" % (year, month - 1)


def _clamp(score: float | int | None) -> int | None:
    return max(0, min(100, int(round(score)))) if score is not None else None


def _models() -> ModuleType:
    """Lazy import of unified_app models (avoids circular import)."""
    import unified_app
    return unified_app


def _locked(
    unlock_hint: str,
    confidence: int = 0,
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"score": None, "trend": None, "confidence": confidence,
            "evidence": [unlock_hint], "signals": signals or {}}


def _dim(
    score: float | int | None,
    confidence: float | int,
    evidence: list[str],
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"score": _clamp(score), "trend": None,
            "confidence": _clamp(confidence), "evidence": evidence,
            "signals": signals or {}}


# ---------------------------------------------------------------------------
# Dimension aggregators — each reads existing tables only.
# ---------------------------------------------------------------------------

def _agg_direction(M: ModuleType, user_id: int) -> dict[str, Any]:
    """CareerAssessment exists? Goal rows count?"""
    assessment = (M.CareerAssessment.query
                  .filter_by(user_id=user_id)
                  .order_by(M.CareerAssessment.created_at.desc()).first())
    goals = M.Goal.query.filter_by(user_id=user_id).all()
    active = [g for g in goals if (g.status or "active") == "active"]
    done = [g for g in goals if g.status == "done"]
    if assessment is None and not goals:
        return _locked(
            "Not enough data yet — take the career assessment to unlock this dimension.",
            signals={"has_assessment": False, "active_goals": 0, "done_goals": 0})

    score, evidence, signals = 0, [], {}
    has_assessment = assessment is not None
    signals["has_assessment"] = has_assessment
    signals["active_goals"] = len(active)
    signals["done_goals"] = len(done)
    if has_assessment:
        score += 45
        top = None
        clusters = assessment.top_clusters or []
        if isinstance(clusters, list) and clusters and isinstance(clusters[0], dict):
            top = clusters[0].get("name")
        evidence.append("career assessment completed"
                        + (" (top match: %s)" % top if top else ""))
    score += min(len(active), 3) * 10
    score += min(len(done), 2) * 7
    if active:
        evidence.append("%d goal%s tracked" % (len(active), "" if len(active) == 1 else "s"))
    if done:
        evidence.append("%d goal%s completed" % (len(done), "" if len(done) == 1 else "s"))
    confidence = min(40 + (20 if has_assessment else 0) + min(len(goals), 4) * 10, 95)
    return _dim(score, confidence, evidence, signals)


def _agg_proof(M: ModuleType, user_id: int) -> dict[str, Any]:
    """ResumeAnalysis count + max resume-readiness score, Portfolio exists?, CertPlan count."""
    analyses = M.ResumeAnalysis.query.filter_by(user_id=user_id).all()
    ats_scores = [a.ats_score for a in analyses
                  if isinstance(a.ats_score, (int, float))]
    max_ats = max(ats_scores) if ats_scores else None
    portfolio = M.Portfolio.query.filter_by(user_id=user_id).first()
    cert_count = M.CertPlan.query.filter_by(user_id=user_id).count()
    if not analyses and portfolio is None and not cert_count:
        return _locked(
            "Not enough data yet — upload your resume for an ATS check to unlock this dimension.",
            signals={"analyses": 0, "max_ats": None, "has_portfolio": False,
                     "cert_plans": 0})

    score, evidence = 0, []
    if analyses:
        score += 25
    if max_ats is not None:
        if max_ats >= 80:
            score += 30
        elif max_ats >= 65:
            score += 22
        elif max_ats >= 50:
            score += 14
        else:
            score += 6
    if portfolio is not None:
        score += 20
    score += min(cert_count, 2) * 10
    if analyses:
        evidence.append("%d resume %s (best ATS %s)" % (
            len(analyses), "analysis" if len(analyses) == 1 else "analyses",
            int(round(max_ats)) if max_ats is not None else "n/a"))
    if portfolio is not None:
        evidence.append("portfolio page published")
    if cert_count:
        evidence.append("%d certification %s planned" % (
            cert_count, "plan" if cert_count == 1 else "plans"))
    confidence = min(35 + min(len(analyses), 4) * 10 + min(cert_count, 3) * 8
                     + (10 if portfolio is not None else 0), 95)
    signals = {"analyses": len(analyses), "max_ats": max_ats,
               "has_portfolio": portfolio is not None, "cert_plans": cert_count}
    return _dim(score, confidence, evidence, signals)


def _agg_opportunity(M: ModuleType, user_id: int) -> dict[str, Any]:
    """JobApplication count by stage, OpportunityBookmark count."""
    apps = M.JobApplication.query.filter_by(user_id=user_id).all()
    bookmarks = M.OpportunityBookmark.query.filter_by(user_id=user_id).count()
    if not apps and not bookmarks:
        return _locked(
            "Not enough data yet — log your first application to unlock this dimension.",
            signals={"applications": 0, "active": 0, "interviews": 0,
                     "offers": 0, "bookmarks": 0})

    beyond_wishlist = [a for a in apps if (a.stage or "wishlist") != "wishlist"]
    interviews = [a for a in apps if (a.stage or "") in ("interview", "onsite", "final")]
    offers = [a for a in apps if (a.stage or "") in ("offer", "accepted")]
    score = min(len(apps), 10) * 5
    if beyond_wishlist:
        score += 10
    if interviews:
        score += 10
    if offers:
        score += 10
    score += min(bookmarks, 5) * 2
    evidence = ["%d application%s tracked" % (
        len(apps), "" if len(apps) == 1 else "s")]
    if beyond_wishlist:
        evidence.append("%d beyond the wishlist" % len(beyond_wishlist))
    if interviews:
        evidence.append("%d in interview stages" % len(interviews))
    if offers:
        evidence.append("%d offer%s" % (len(offers), "" if len(offers) == 1 else "s"))
    if bookmarks:
        evidence.append("%d opportunit%s bookmarked" % (
            bookmarks, "y" if bookmarks == 1 else "ies"))
    confidence = min(30 + min(len(apps), 8) * 7 + min(bookmarks, 4) * 5, 95)
    signals = {"applications": len(apps), "active": len(beyond_wishlist),
               "interviews": len(interviews), "offers": len(offers),
               "bookmarks": bookmarks}
    return _dim(score, confidence, evidence, signals)


def _quiz_avg(quizzes: list[Any]) -> float | None:
    pcts = []
    for q in quizzes:
        summary = q.summary_json or {}
        pct = summary.get("pct")
        if isinstance(pct, (int, float)):
            pcts.append(float(pct))
    return (sum(pcts) / len(pcts)) if pcts else None


def _agg_preparation(M: ModuleType, user_id: int) -> dict[str, Any]:
    """QuizAttempt count + avg score, DSA solved count, InterviewSession count,
    AptitudeAttempt count, PaperQuizAttempt count."""
    quizzes = M.QuizAttempt.query.filter_by(user_id=user_id).all()
    quiz_avg = _quiz_avg(quizzes)
    dsa_solved = M.DSAProgress.query.filter_by(user_id=user_id, status="solved").count()
    mocks = [s for s in M.InterviewSession.query.filter_by(user_id=user_id).all()
             if s.total_score is not None]
    apt_submitted = [a for a in M.AptitudeAttempt.query.filter_by(user_id=user_id).all()
                     if a.submitted_at is not None]
    apt_avg = None
    apt_pcts = [float((a.summary_json or {}).get("score_pct"))
                for a in apt_submitted
                if isinstance((a.summary_json or {}).get("score_pct"), (int, float))]
    if apt_pcts:
        apt_avg = sum(apt_pcts) / len(apt_pcts)
    paper = M.PaperQuizAttempt.query.filter_by(user_id=user_id).all()

    if (not quizzes and not dsa_solved and not mocks and not apt_submitted
            and not paper):
        return _locked(
            "Not enough data yet — complete a quiz to unlock this dimension.",
            signals={"quizzes": 0, "quiz_avg": None, "dsa_solved": 0,
                     "mocks": 0, "aptitude": 0, "paper_quizzes": 0})

    score, evidence = 0, []
    if quizzes:
        quality = (quiz_avg / 100.0) if quiz_avg is not None else 0.5
        score += min(len(quizzes), 10) * 3 * quality
        evidence.append("based on %d quiz%s%s" % (
            len(quizzes), "" if len(quizzes) == 1 else "zes",
            (", avg %d%%" % int(round(quiz_avg))) if quiz_avg is not None else ""))
    if dsa_solved:
        score += min(dsa_solved, 20) * 1.5
        evidence.append("%d DSA problem%s solved" % (
            dsa_solved, "" if dsa_solved == 1 else "s"))
    if mocks:
        score += min(len(mocks), 5) * 4
        evidence.append("%d mock interview%s completed" % (
            len(mocks), "" if len(mocks) == 1 else "s"))
    if apt_submitted:
        score += min(len(apt_submitted), 5) * 2
        evidence.append("%d timed aptitude test%s%s" % (
            len(apt_submitted), "" if len(apt_submitted) == 1 else "s",
            (", avg %d%%" % int(round(apt_avg))) if apt_avg is not None else ""))
    if paper:
        score += min(len(paper), 5) * 2
        evidence.append("%d paper quiz%s" % (
            len(paper), "" if len(paper) == 1 else "zes"))
    total_events = len(quizzes) + dsa_solved + len(mocks) + len(apt_submitted) + len(paper)
    confidence = min(25 + total_events * 4, 95)
    signals = {"quizzes": len(quizzes), "quiz_avg": quiz_avg,
               "dsa_solved": dsa_solved, "mocks": len(mocks),
               "aptitude": len(apt_submitted), "paper_quizzes": len(paper)}
    return _dim(score, confidence, evidence, signals)


def _agg_wellbeing(
    M: ModuleType,
    user_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """StreakState current/longest + shields used (forgiveness respected —
    score consistency, not grind). MomentumSettings opt-out respected: opted
    out -> score None with an affirming note. Rest days noted positively."""
    now = now or datetime.now(UTC)
    settings = M.MomentumSettings.query.filter_by(user_id=user_id).first()
    if settings is not None and settings.momentum_opt_out:
        return {"score": None, "trend": None, "confidence": 0,
                "evidence": ["You opted out of momentum tracking — that's healthy."],
                "signals": {"opted_out": True}}

    st = M.StreakState.query.filter_by(user_id=user_id).first()
    current = int(st.current or 0) if st else 0
    longest = int(st.longest or 0) if st else 0
    shields_used = int(st.shields_used_month or 0) if st else 0

    # Active days in the last 14 days, derived from the XP ledger (evidence of
    # effort, not screen time).
    cutoff = (now - timedelta(days=14)).replace(tzinfo=None)
    active_days = 0
    try:
        rows = (M.XPLedger.query
                .filter(M.XPLedger.user_id == user_id,
                        M.XPLedger.created_at >= cutoff).all())
        active_days = len({r.created_at.date() for r in rows if r.created_at})
    except Exception:
        log.exception("health_review: wellbeing activity lookup failed")

    if st is None and not active_days:
        return _locked(
            "Not enough data yet — momentum activity will unlock this dimension.",
            signals={"opted_out": False, "streak": 0, "longest": 0,
                     "shields_used": 0, "active_days_14": 0})

    # Consistency, not grind: streak contribution caps at 30 days, longest at
    # 60. Shields cost nothing — they are forgiveness by design.
    score = min(current, 30) / 30.0 * 55
    score += min(longest, 60) / 60.0 * 25
    score += min(active_days, 14) / 14.0 * 20
    evidence = []
    if current:
        evidence.append("%d-day current streak%s" % (
            current, (" (longest: %d)" % longest) if longest > current else ""))
    elif longest:
        evidence.append("longest streak %d days — a fresh start is fine" % longest)
    if shields_used:
        evidence.append("%d rest %s forgiven by shields — that's by design, not a failure" % (
            shields_used, "day" if shields_used == 1 else "days"))
    evidence.append("%d of the last 14 days active — steady beats grinding" % active_days)
    confidence = min(30 + longest * 3 + active_days * 3, 90)
    signals = {"opted_out": False, "streak": current, "longest": longest,
               "shields_used": shields_used, "active_days_14": active_days}
    return _dim(score, confidence, evidence, signals)


_AGGREGATORS = {
    "direction": _agg_direction,
    "proof": _agg_proof,
    "opportunity": _agg_opportunity,
    "preparation": _agg_preparation,
    "wellbeing": _agg_wellbeing,
}


# ---------------------------------------------------------------------------
# next_action: the single smallest high-leverage action, from the weakest
# unlocked dimension.
# ---------------------------------------------------------------------------

def _pick_next_action(dims: dict[str, dict[str, Any]]) -> str:
    unlocked = [(did, d) for did, d in dims.items()
                if d.get("score") is not None]
    if not unlocked:
        return ("Do one small career thing today — a quiz, a saved opportunity, "
                "or one goal written down.")
    weakest_id, weakest = min(unlocked, key=lambda kv: kv[1]["score"])
    sig = weakest.get("signals") or {}

    if weakest_id == "direction":
        if not sig.get("has_assessment"):
            return "Take the career assessment — five minutes to pin down your direction."
        if not sig.get("active_goals"):
            return "Write down one concrete career goal for this month."
        return "Review your goals — close out one small win this week."

    if weakest_id == "proof":
        if not sig.get("analyses"):
            return "Upload your resume for a free ATS check."
        if not sig.get("has_portfolio"):
            return "Publish your one-page portfolio — proof you can link to."
        return "Add one certification plan to strengthen your proof."

    if weakest_id == "opportunity":
        if not sig.get("applications"):
            return "Log your first application in the tracker."
        if not sig.get("active"):
            return "Move one wishlist item into a real application."
        return "Follow up on your oldest pending application."

    if weakest_id == "preparation":
        if not sig.get("quizzes") and not sig.get("dsa_solved"):
            return "Solve one easy DSA problem to start the engine."
        if (sig.get("quiz_avg") is not None and sig["quiz_avg"] < 60):
            return "Retake a quiz on your weakest topic — reps beat rereading."
        if not sig.get("mocks"):
            return "Book one mock interview — real reps, low stakes."
        return "Complete one timed aptitude test this week."

    # wellbeing
    if sig.get("streak", 0) < 3:
        return "Do one small career action today — consistency beats intensity."
    return "Take a deliberate rest day — recovery is part of the plan."


# ---------------------------------------------------------------------------
# Narrative: 2-3 plain sentences, no jargon, no fake precision.
# ---------------------------------------------------------------------------

_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _month_label(month_key: str) -> str:
    try:
        return "%s %s" % (_MONTH_NAMES[int(month_key[5:7])], month_key[:4])
    except (ValueError, IndexError):
        return month_key


def _build_narrative(dims: dict[str, dict[str, Any]], month_key: str) -> str:
    meta = {d["id"]: d for d in DIMENSIONS}
    unlocked = [(did, dims[did]) for did in DIMENSION_IDS
                if dims[did].get("score") is not None]
    label = _month_label(month_key)
    if not unlocked:
        return ("Not much signal yet for %s — and that's fine. "
                "This review gets useful once you've done a few career "
                "activities. Pick the one small action below and check back "
                "next month." % label)
    by_score = sorted(unlocked, key=lambda kv: kv[1]["score"])
    weakest_id, weakest = by_score[0]
    strongest_id, strongest = by_score[-1]
    s1 = ("For %s, %d of 5 areas have enough data. Strongest right now: %s "
          "(%d). Needs the most attention: %s (%d)."
          % (label, len(unlocked), meta[strongest_id]["title"], strongest["score"],
             meta[weakest_id]["title"], weakest["score"]))
    movers = [(did, d["trend"]) for did, d in unlocked
              if d.get("trend") not in (None, 0)]
    if movers:
        up = [meta[did]["title"] for did, t in movers if t > 0]
        down = [meta[did]["title"] for did, t in movers if t < 0]
        bits = []
        if up:
            bits.append("%s trending up" % ", ".join(up))
        if down:
            bits.append("%s slipped a little" % ", ".join(down))
        s2 = "Since last month: %s." % "; ".join(bits)
    else:
        s2 = "No big moves since last month — steady is good."
    locked = [meta[did]["title"] for did in DIMENSION_IDS
              if dims[did].get("score") is None and did != "wellbeing"]
    if locked:
        s3 = ("Still quiet: %s. That's a gap, not a grade — the action below "
              "is the smallest step that would fill it." % ", ".join(locked))
    else:
        s3 = "Keep the weakest area in view; small reps compound."
    return " ".join([s1, s2, s3])


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def generate_review(
    user_id: int,
    month_key: str,
    now: datetime | None = None,
) -> tuple[Any, bool]:
    """Build (or rebuild) the review for (user_id, month_key). Idempotent:
    regenerating the same month updates the existing row in place.

    Returns (HealthReview row, created: bool). Raises ValueError for a
    malformed month_key. Never raises for aggregator failures — a broken
    aggregator degrades to a locked dimension with an honest note.
    """
    if not valid_month_key(month_key):
        raise ValueError("month_key must be 'YYYY-MM', got %r" % (month_key,))
    M = _models()
    user_id = int(user_id)

    dims = {}
    for dim in DIMENSIONS:
        try:
            d = _AGGREGATORS[dim["id"]](M, user_id, now) \
                if dim["id"] == "wellbeing" \
                else _AGGREGATORS[dim["id"]](M, user_id)
        except TypeError:
            # Aggregator signature drift — call the 2-arg form.
            d = _AGGREGATORS[dim["id"]](M, user_id)
        except Exception:
            log.exception("health_review: aggregator %s failed", dim["id"])
            d = {"score": None, "trend": None, "confidence": 0,
                 "evidence": ["We couldn't compute this dimension this month."],
                 "signals": {}}
        dims[dim["id"]] = d

    prev = (M.HealthReview.query
            .filter_by(user_id=user_id, month_key=prev_month_key(month_key)).first())
    prev_dims = (prev.dimensions_json or {}) if prev is not None else {}
    for did in DIMENSION_IDS:
        prev_score = (prev_dims.get(did) or {}).get("score")
        cur = dims[did]["score"]
        if cur is not None and isinstance(prev_score, (int, float)):
            dims[did]["trend"] = int(cur) - int(prev_score)
        else:
            dims[did]["trend"] = None

    narrative = _build_narrative(dims, month_key)
    next_action = _pick_next_action(dims)

    row = (M.HealthReview.query
           .filter_by(user_id=user_id, month_key=month_key).first())
    created = row is None
    if created:
        row = M.HealthReview(user_id=user_id, month_key=month_key)
        M.db.session.add(row)
    row.dimensions_json = dims
    row.narrative = narrative
    row.next_action = next_action
    M.db.session.commit()
    return row, created


# ================= Feature 31 Ultimate Enhancements =================
# 31.1 Monthly auto-report | 31.2 Peer benchmarks | 31.3 Action nudges

import re as _re21


def _clean21(text: str | None) -> str:
    return _re21.sub(r"<[^>]*>", "", text or "")


def monthly_report(review: dict[str, Any]) -> dict[str, Any]:
    """Turn a health review into a readable monthly digest.

    review: dict from generate_review() with {dimensions, overall?}.
    Returns {headline, highlights, concerns, markdown}.
    """
    if not isinstance(review, dict):
        raise ValueError("review must be a dict.")
    dims = review.get("dimensions") or {}
    if not isinstance(dims, dict) or not dims:
        raise ValueError("Review has no dimensions to report on.")
    scored = []
    for name, d in dims.items():
        if not isinstance(d, dict):
            continue
        try:
            score = float(d.get("score") or 0)
        except (TypeError, ValueError):
            continue
        scored.append((name, score, _clean21(d.get("evidence") or "")[:150]))
    if not scored:
        raise ValueError("No scored dimensions found.")
    scored.sort(key=lambda x: -x[1])
    overall = round(sum(s for _, s, _ in scored) / len(scored), 1)
    highlights = [{"dimension": n, "score": s, "evidence": e}
                  for n, s, e in scored[:2]]
    concerns = [{"dimension": n, "score": s, "evidence": e}
                for n, s, e in scored[-2:] if s < 60]
    month = _clean21(review.get("month") or review.get("month_key") or "")[:20]
    lines = [f"# Career Health — {month or 'This Month'}", "",
             f"**Overall: {overall}/100**", "",
             "## Highlights"]
    for h in highlights:
        lines.append(f"- **{h['dimension']}** ({h['score']}): {h['evidence'] or 'strong'}")
    if concerns:
        lines.append("")
        lines.append("## Needs attention")
        for c in concerns:
            lines.append(f"- **{c['dimension']}** ({c['score']}): {c['evidence'] or 'weak'}")
    headline = ("Career health is strong — keep the rhythm." if overall >= 70 else
                "Career health is steady — push one weak area." if overall >= 50 else
                "Career health needs work — focus this month.")
    return {"headline": headline, "overall": overall,
            "highlights": highlights, "concerns": concerns,
            "markdown": "\n".join(lines)}


def peer_benchmarks(
    my_scores: dict[str, float],
    peer_scores: list[dict[str, float]],
) -> dict[str, Any]:
    """Where do you stand vs peers? Returns percentiles per dimension.

    my_scores: {dimension: score}. peer_scores: list of {dimension: score}.
    """
    if not isinstance(my_scores, dict) or not my_scores:
        raise ValueError("my_scores must be a non-empty dict.")
    if not isinstance(peer_scores, (list, tuple)) or not peer_scores:
        raise ValueError("peer_scores must be a non-empty list.")
    out = []
    for dim, mine in my_scores.items():
        try:
            mine_f = float(mine)
        except (TypeError, ValueError):
            continue
        peers = []
        for p in peer_scores:
            if isinstance(p, dict) and dim in p:
                try:
                    peers.append(float(p[dim]))
                except (TypeError, ValueError):
                    continue
        if not peers:
            continue
        below = sum(1 for v in peers if v < mine_f)
        pct = round(below / len(peers) * 100, 1)
        out.append({"dimension": _clean21(dim)[:40], "my_score": mine_f,
                    "peer_median": round(sorted(peers)[len(peers) // 2], 1),
                    "peer_count": len(peers), "percentile": pct,
                    "standing": ("Top 10%" if pct >= 90 else
                                 f"Top {100 - int(pct)}%" if pct >= 50 else
                                 f"Bottom {int(pct)}% — room to grow")})
    out.sort(key=lambda x: x["percentile"])
    return {"benchmarks": out,
            "summary": (f"Weakest vs peers: {out[0]['dimension']} "
                        f"({out[0]['standing']})." if out else "No comparable data.")}


# Health-dip triggers -> specific actions.
_NUDGE_RULES = [
    ("applications", 3, "Applications stalled",
     "Apply to 3 roles this week — momentum beats perfection."),
    ("interview_prep", 40, "Interview prep dropped",
     "Do one mock interview this week — even a 20-min peer mock."),
    ("learning", 40, "Learning stalled",
     "Finish one roadmap module — 2 hours, that's it."),
    ("networking", 30, "Networking quiet",
     "Send 2 cold outreaches this week with the outreach generator."),
]


def action_nudges(
    current_scores: dict[str, Any],
    previous_scores: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Health dips trigger specific suggested actions.

    Returns {nudges: [{dimension, message, action}], urgent_count}.
    A nudge fires when a score is low OR dropped vs last month.
    """
    if not isinstance(current_scores, dict):
        raise ValueError("current_scores must be a dict.")
    previous_scores = previous_scores or {}
    nudges = []
    for dim, threshold, label, action in _NUDGE_RULES:
        try:
            cur = float(current_scores.get(dim)) if current_scores.get(dim) is not None else None
        except (TypeError, ValueError):
            continue
        if cur is None:
            continue
        try:
            prev = float(previous_scores.get(dim)) if previous_scores.get(dim) is not None else None
        except (TypeError, ValueError):
            prev = None
        dropped = prev is not None and cur < prev - 10
        if cur < threshold or dropped:
            why = (f"dropped {prev - cur:.0f} pts" if dropped else f"at {cur:.0f}")
            nudges.append({"dimension": dim, "label": label,
                           "message": f"{label} ({why}). {action}",
                           "action": action, "urgent": dropped})
    nudges.sort(key=lambda x: (not x["urgent"], x["dimension"]))
    return {"nudges": nudges, "count": len(nudges),
            "urgent_count": sum(1 for n in nudges if n["urgent"])}
