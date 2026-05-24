"""EV math: devig, consensus fair-prob, EV calculation.

Conventions
-----------
- Polymarket prices: 0..1 representing P(outcome) directly.
- Bookmaker odds: decimal (e.g. 2.10) → implied prob = 1 / odd.
- Bookmakers carry vig: implied probs sum to >1; devig normalizes them back.
- Fair prob is the devigged Pinnacle prob for now (sharp book, weight 1.0).
  In Etapa 8 the consensus turns into a weighted average across multiple
  books (Pinnacle 0.40, bet365 0.20, Betano 0.15, Superbet 0.15, Estrela 0.10).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz

from backend.matcher.normalize import normalize_team

logger = logging.getLogger(__name__)

# Source weight used to build the consensus fair price. Pinnacle is the
# anchor; the rest gets populated when their scrapers land.
SOURCE_WEIGHTS: dict[str, float] = {
    "pinnacle": 0.40,
    "bet365": 0.20,
    "betano": 0.15,
    "superbet": 0.15,
    "estrelabet": 0.10,
}

# Probability bounds outside which we refuse to trade — extreme outcomes
# have low absolute EV and high error from devigging.
MIN_FAIR_PROB = 0.05
MAX_FAIR_PROB = 0.95

# Outcome-mapping fuzzy threshold: PM outcome string must look this close
# to the matched external_event's home/away/draw name to be classified.
OUTCOME_MAP_THRESHOLD = 70.0


@dataclass(slots=True)
class DevigResult:
    """Devigged probabilities indexed by side ('home'|'away'|'draw')."""
    probs: dict[str, float]
    overround: float  # sum of raw probs (>1 → vig present)
    n_outcomes: int


def devig_simple(raw_probs: dict[str, float]) -> DevigResult | None:
    """Normalize raw implied probabilities so they sum to 1.

    "Simple" because we just divide each by the sum (proportional method).
    Shin's method is more theoretically sound for sharp books but the gap is
    small for Pinnacle and the proportional method has the advantage of
    composing trivially with the weighted-consensus we'll do later.
    """
    if not raw_probs:
        return None
    total = sum(raw_probs.values())
    if total <= 0 or any(p <= 0 for p in raw_probs.values()):
        return None
    if total < 0.9 or total > 1.5:
        # Sanity guard — odds way off this range probably mean a bad scrape
        # or a partial market (e.g. only home odd available, no draw/away).
        return None
    devigged = {side: p / total for side, p in raw_probs.items()}
    return DevigResult(probs=devigged, overround=total, n_outcomes=len(raw_probs))


def implied_prob_from_decimal(decimal_odd: float) -> float | None:
    if decimal_odd is None or decimal_odd <= 1.0:
        return None
    return 1.0 / decimal_odd


def map_pm_outcome_to_side(
    pm_outcome: str, ext_home: str, ext_away: str
) -> str | None:
    """Decide whether a PM outcome string ('Catanzaro', 'Draw', 'Yes', ...)
    means home/away/draw of the matched external_event.

    Returns None when we can't tell — caller logs PASS_NO_MAP.
    """
    if not pm_outcome:
        return None
    low = pm_outcome.strip().lower()
    if low in {"draw", "tie", "the draw", "x"}:
        return "draw"
    if low in {"yes", "no"}:
        # Binary markets that aren't team-named (e.g. "Will X win?") — those
        # were rejected by the matcher anyway; reject here too.
        return None
    n_pm = normalize_team(pm_outcome)
    if not n_pm:
        return None
    n_home = normalize_team(ext_home)
    n_away = normalize_team(ext_away)
    s_home = fuzz.partial_token_set_ratio(n_pm, n_home) if n_home else 0.0
    s_away = fuzz.partial_token_set_ratio(n_pm, n_away) if n_away else 0.0
    if s_home >= OUTCOME_MAP_THRESHOLD and s_home > s_away:
        return "home"
    if s_away >= OUTCOME_MAP_THRESHOLD and s_away > s_home:
        return "away"
    return None


def consensus_fair_prob(probs_by_source: dict[str, float]) -> float | None:
    """Weighted average of devigged probs across available sources.

    Renormalize weights against only the sources that actually contributed
    a probability — so during Etapa 7 (Pinnacle only) it just returns the
    Pinnacle prob, and the formula naturally extends once we add bet365,
    Betano, etc. in Etapa 8.
    """
    if not probs_by_source:
        return None
    weighted = 0.0
    weight_sum = 0.0
    for source, p in probs_by_source.items():
        w = SOURCE_WEIGHTS.get(source, 0.0)
        if w <= 0 or p is None:
            continue
        weighted += w * p
        weight_sum += w
    if weight_sum == 0:
        return None
    return weighted / weight_sum


def compute_ev_buy(fair_prob: float, poly_ask: float) -> float:
    """Expected value of buying at `poly_ask` when fair prob is `fair_prob`.

    Polymarket pays $1 per share on win, 0 on loss. Buying at `ask` costs
    `ask` per share.
        EV per $1 staked = (fair_prob - ask) / ask
    """
    if poly_ask is None or poly_ask <= 0:
        return float("-inf")
    return (fair_prob - poly_ask) / poly_ask
