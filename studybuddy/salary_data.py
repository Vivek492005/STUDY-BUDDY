"""India salary explorer (Wave 1 feature #8).

Serves a curated, built-in dataset of indicative salary ranges
(data/salary_ranges.json): role x city-tier x experience-band -> min/median/max
in LPA. The data is curated for student guidance (October 2026) and is NOT
live-scraped; callers must surface the disclaimer and last-updated note.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "salary_ranges.json")


class SalaryDataError(Exception):
    """Raised when the salary dataset is missing or malformed."""


@lru_cache(maxsize=1)
def load_dataset() -> dict[str, Any]:
    """Load and validate the salary dataset (cached for the process lifetime)."""
    if not os.path.exists(DATA_PATH):
        raise SalaryDataError("Salary dataset not found.")
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise SalaryDataError("Salary dataset is unreadable.") from exc
    for key in ("meta", "roles", "tiers", "experience_bands", "entries"):
        if key not in data:
            raise SalaryDataError(f"Salary dataset missing key: {key}")
    for e in data["entries"]:
        for k in ("role", "tier", "experience_band", "min_lpa", "median_lpa", "max_lpa"):
            if k not in e:
                raise SalaryDataError("Salary dataset entry is malformed.")
        if not (0 < e["min_lpa"] <= e["median_lpa"] <= e["max_lpa"]):
            raise SalaryDataError("Salary dataset has an inconsistent range.")
    # #11: optional city-level entries carry DATA-01 provenance fields.
    for e in data.get("city_entries", []):
        for k in ("id", "role", "city", "experience_band",
                  "min_lpa", "median_lpa", "max_lpa",
                  "source_url", "retrieved_at", "license",
                  "last_verified", "confidence"):
            if k not in e:
                raise SalaryDataError("Salary city entry is malformed.")
        if not (0 < e["min_lpa"] <= e["median_lpa"] <= e["max_lpa"]):
            raise SalaryDataError("Salary city entry has an inconsistent range.")
    return data


def get_options() -> dict[str, Any]:
    """Return filter options: roles, tiers, experience bands + meta."""
    data = load_dataset()
    return {
        "roles": list(data["roles"]),
        "tiers": {k: {"label": v["label"], "cities": list(v["cities"])}
                  for k, v in data["tiers"].items()},
        "experience_bands": list(data["experience_bands"]),
        "meta": dict(data["meta"]),
    }


def query(
    role: str | None = None,
    tier: str | None = None,
    band: str | None = None,
) -> list[dict[str, Any]]:
    """Return matching salary entries. All filters optional; invalid values -> []."""
    data = load_dataset()
    valid_roles = set(data["roles"])
    valid_tiers = set(data["tiers"])
    valid_bands = set(data["experience_bands"])
    if role is not None and role not in valid_roles:
        return []
    if tier is not None and tier not in valid_tiers:
        return []
    if band is not None and band not in valid_bands:
        return []
    out = []
    for e in data["entries"]:
        if role is not None and e["role"] != role:
            continue
        if tier is not None and e["tier"] != tier:
            continue
        if band is not None and e["experience_band"] != band:
            continue
        out.append({
            "role": e["role"],
            "tier": e["tier"],
            "tier_label": data["tiers"][e["tier"]]["label"],
            "cities": list(data["tiers"][e["tier"]]["cities"]),
            "experience_band": e["experience_band"],
            "min_lpa": e["min_lpa"],
            "median_lpa": e["median_lpa"],
            "max_lpa": e["max_lpa"],
        })
    return out


def lookup(role: str, tier: str, band: str) -> dict[str, Any] | None:
    """Return the single entry for an exact role/tier/band, or None."""
    rows = query(role=role, tier=tier, band=band)
    return rows[0] if rows else None


def city_query(
    role: str | None = None,
    city: str | None = None,
    band: str | None = None,
) -> list[dict[str, Any]]:
    """Return city-level entries (#11), provenance fields included.

    The tier table gives one range per city group; city_entries refine
    the metros students ask about most. All filters optional.
    """
    data = load_dataset()
    out = []
    for e in data.get("city_entries", []):
        if role is not None and e["role"] != role:
            continue
        if city is not None and e["city"] != city:
            continue
        if band is not None and e["experience_band"] != band:
            continue
        out.append(dict(e))
    return out


def lookup_city(role: str, city: str, band: str) -> dict[str, Any] | None:
    """Return the single city entry for role/city/band, or None."""
    rows = city_query(role=role, city=city, band=band)
    return rows[0] if rows else None


# ---- Live Data Engine: freshness ----
SALARY_STALE_AFTER_DAYS = 90  # curated indicative ranges; review quarterly

def get_freshness() -> dict[str, Any]:
    """Freshness badge info for the salary explorer."""
    from studybuddy.live_data import freshness_badge
    try:
        meta = load_dataset().get("meta", {})
    except SalaryDataError:
        meta = {}
    return freshness_badge(meta, SALARY_STALE_AFTER_DAYS)


# ================= Feature 8 Ultimate Enhancements =================
# 8.1 Offer evaluator | 8.2 Negotiation coach | 8.3 Band comparison

import re as _re3


def _clean3(text: str | None) -> str:
    return _re3.sub(r"<[^>]*>", "", text or "")


def evaluate_offer(
    role: str,
    tier: str,
    band: str,
    offer_lpa: float,
) -> dict[str, Any]:
    """Paste an offer, get a verdict: below/at/above market + negotiation script.

    Returns {verdict, percentile_hint, market: {min, median, max}, script, gap_lpa}.
    offer_lpa in LPA (float). Never raises on hostile input.
    """
    role = _clean3(role)
    tier = _clean3(tier)
    band = _clean3(band)
    try:
        offer = float(offer_lpa)
    except (TypeError, ValueError):
        raise ValueError("Offer must be a number in LPA.")
    if not (0 < offer < 1000):
        raise ValueError("Offer looks unrealistic (0-1000 LPA expected).")

    entry = lookup(role, tier, band)
    if not entry:
        raise ValueError("No market data for that role/tier/band combination.")

    lo, med, hi = entry["min_lpa"], entry["median_lpa"], entry["max_lpa"]
    if offer < lo:
        verdict = "Below market"
        detail = f"₹{offer:.1f} LPA is below the market floor of ₹{lo:.1f} LPA."
    elif offer < med:
        verdict = "Lower half of market"
        detail = f"₹{offer:.1f} LPA sits between floor ₹{lo:.1f} and median ₹{med:.1f} LPA."
    elif offer <= hi:
        verdict = "At or above market"
        detail = f"₹{offer:.1f} LPA is at/above the median of ₹{med:.1f} LPA."
    else:
        verdict = "Above market"
        detail = f"₹{offer:.1f} LPA beats the market ceiling of ₹{hi:.1f} LPA — strong offer."

    gap = round(med - offer, 1)
    if verdict in ("Below market", "Lower half of market") and gap > 0:
        script = (
            f"Thank you for the offer. Based on market data for {role} "
            f"({tier}, {band}), the median is around ₹{med:.1f} LPA. "
            f"Given my skills in [your top 2 skills], would you consider ₹{med:.1f} LPA? "
            f"I am excited about the team and happy to discuss.")
    elif verdict == "Above market":
        script = ("This is a strong offer — accept graciously, and confirm the "
                  "breakup (fixed vs variable), joining bonus, and review cycle in writing.")
    else:
        script = ("Solid offer. If you have competing offers, you can still ask: "
                  "'Is there flexibility on the joining bonus or first review?'")
    return {"verdict": verdict, "detail": detail,
            "market": {"min_lpa": lo, "median_lpa": med, "max_lpa": hi},
            "offer_lpa": round(offer, 1), "gap_to_median_lpa": gap,
            "negotiation_script": script}


def negotiation_coach(situation: str) -> dict[str, Any]:
    """Rule-based negotiation playbook for common situations.

    situation: one of 'lowball', 'at_median', 'competing_offers',
    'fresher_no_leverage', 'intern_to_ppo'. Returns {do, say, dont}.
    """
    situation = _clean3(situation or "").strip().lower().replace(" ", "_")
    playbooks = {
        "lowball": {
            "do": ["Thank them warmly first", "Cite market data, not feelings",
                   "Give a specific counter (median, not max)", "Ask what is flexible besides base"],
            "say": ("I am excited about the role. Market data suggests ₹X LPA is typical "
                    "for this profile — would ₹Y work for you?"),
            "dont": ["Never accept on the spot", "Don't threaten to walk away",
                     "Don't mention personal expenses as justification"],
        },
        "at_median": {
            "do": ["Confirm the full breakup in writing", "Negotiate joining bonus / ESOPs",
                   "Ask about the first appraisal cycle"],
            "say": ("The base looks fair. Could we discuss a joining bonus or an "
                    "early performance review at 6 months?"),
            "dont": ["Don't push base 20% higher without leverage", "Don't delay deciding too long"],
        },
        "competing_offers": {
            "do": ["Use the better offer as data, not a threat", "Give a deadline",
                   "Let them know they are your first choice (if true)"],
            "say": ("I have another offer at ₹X. I prefer your team — can you match ₹X "
                    "or close the gap? I can decide by Friday."),
            "dont": ["Don't lie about fake offers — it backfires", "Don't share the offer letter"],
        },
        "fresher_no_leverage": {
            "do": ["Negotiate learning velocity, not salary", "Ask for mentorship + review timeline",
                   "Get the role title right (titles compound)"],
            "say": ("I understand fresher bands are fixed. Could we agree on a 6-month "
                    "performance review with a raise linked to goals?"),
            "dont": ["Don't reject your first offer over 0.5 LPA", "Don't compare with FAANG outliers"],
        },
        "intern_to_ppo": {
            "do": ["Remind them of your shipped work", "Ask for the PPO band, not a number",
                   "Negotiate start date flexibility"],
            "say": ("During my internship I shipped [X, Y]. What does the PPO band look like "
                    "for converting interns at my level?"),
            "dont": ["Don't assume PPO equals market — verify", "Don't wait till the last week"],
        },
    }
    if situation not in playbooks:
        raise ValueError(
            "Unknown situation. Try: lowball, at_median, competing_offers, "
            "fresher_no_leverage, intern_to_ppo.")
    pb = playbooks[situation]
    return {"situation": situation, "do": pb["do"], "say": pb["say"],
            "dont": pb["dont"]}


def band_comparison(role: str, tier: str) -> dict[str, Any]:
    """Fresher vs experienced bands side-by-side: the growth trajectory.

    Returns {bands: [{band, min, median, max}], growth_note}.
    """
    role = _clean3(role)
    tier = _clean3(tier)
    data = load_dataset()
    if role not in data["roles"] or tier not in data["tiers"]:
        raise ValueError("Unknown role or tier.")
    rows = query(role=role, tier=tier)
    if not rows:
        raise ValueError("No market data for that role/tier.")
    bands = [{"band": r["experience_band"], "min_lpa": r["min_lpa"],
              "median_lpa": r["median_lpa"], "max_lpa": r["max_lpa"]}
             for r in rows]
    medians = [b["median_lpa"] for b in bands]
    growth = round(max(medians) - min(medians), 1) if len(medians) > 1 else 0.0
    return {"role": role, "tier": tier, "bands": bands,
            "growth_note": (
                f"Median grows ~₹{growth:.1f} LPA from {bands[0]['band']} to "
                f"{bands[-1]['band']} — skills compound faster than tenure."
                if growth else "Single band available for this role/tier.")}
