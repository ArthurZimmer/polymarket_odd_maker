"""Team-name normalization + title parsing.

`normalize_team()` produces a comparable representation:
  - NFKD-folded ASCII (drops accents)
  - lowercased
  - punctuation stripped
  - trailing/leading common qualifiers stripped (FC, CF, SC, SE, AC, EC, Club…)
  - whitespace collapsed
  - alias lookup applied last (so PT-BR variants land on canonical form)

`extract_teams_from_title()` parses Polymarket event titles like
  "Catanzaro vs. Monza"  ─►  ("catanzaro", "monza")
  "Real Madrid - Barcelona" ─►  ("real madrid", "barcelona")
Returns None when the title doesn't look like a head-to-head match (e.g.
"Will X win the league?", championship outright markets).
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

_ALIASES_PATH = Path(__file__).parent / "aliases" / "teams_ptbr.json"

# Tokens stripped from the start/end of a normalized name. Order matters:
# longer ones first so we don't half-strip "football club" by matching "fc".
_NOISE_TOKENS: tuple[str, ...] = (
    "football club",
    "futebol clube",
    "esporte clube",
    "clube de regatas",
    "associacao atletica",
    "sport club",
    "cf",
    "fc",
    "sc",
    "ec",
    "ac",
    "ud",
    "cd",
    "sd",
    "rcd",
    "club",
    "clube",
    "team",
    "tim",
    "if",
    "afc",
    "bk",
)

# Title patterns: ordered most-specific first. Each must capture (home, away).
_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?P<home>.+?)\s+vs\.?\s+(?P<away>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^(?P<home>.+?)\s+v\.?\s+(?P<away>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^(?P<home>.+?)\s+@\s+(?P<away>.+?)\s*$"),  # US-style "away @ home"
    re.compile(r"^(?P<home>.+?)\s+x\s+(?P<away>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^(?P<home>.+?)\s+[-–—]\s+(?P<away>.+?)\s*$"),
)

# Reject when the title looks like a prop / outright / multi-leg question, or
# a Polymarket sub-market variant (halftime, exact score, etc.) that we don't
# want to treat as the head-to-head canonical event.
_TITLE_REJECT_HINTS: tuple[str, ...] = (
    "?",
    "will ",
    "who will",
    "winner of",
    "champion",
    "to win",
    "outright",
    " - more markets",
    " - halftime",
    " - exact score",
    " - halftime/fulltime",
    " - both teams to score",
    " - total goals",
    " - most goals",
    " - most sixes",
    " - toss match",
    " - first goal",
    " - clean sheet",
    " - winning margin",
    " - asian handicap",
    " - over",
    " - under",
    " - draw no bet",
    " - last place",
    " - top goalscorer",
    " - top scorer",
    " - most assists",
    " - player props",
    " - relegated",
    " - which clubs",
)


def _ascii_fold(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)
    )


def _strip_noise(tokens: list[str]) -> list[str]:
    """Drop standalone noise tokens (e.g. 'fc') and noise prefixes/suffixes."""
    # Remove single-token noise anywhere in the list. Multi-word noise is
    # handled before tokenization (we replace it with spaces).
    return [t for t in tokens if t not in _NOISE_TOKENS]


_ALIAS_CACHE: tuple[float, dict[str, str]] | None = None


def _load_aliases() -> dict[str, str]:
    """Reload the alias JSON when its mtime changes.

    The matcher runs as a long-lived task; the editor flow needs new aliases
    to take effect without a uvicorn restart. uvicorn's --reload only watches
    .py files, so we mtime-check the JSON here on every call.
    """
    global _ALIAS_CACHE
    try:
        mtime = os.path.getmtime(_ALIASES_PATH)
    except FileNotFoundError:
        return {}
    if _ALIAS_CACHE is not None and _ALIAS_CACHE[0] == mtime:
        return _ALIAS_CACHE[1]
    try:
        with _ALIASES_PATH.open(encoding="utf-8") as fp:
            data = json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return _ALIAS_CACHE[1] if _ALIAS_CACHE else {}
    aliases = {
        k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)
    }
    _ALIAS_CACHE = (mtime, aliases)
    return aliases


def normalize_team(name: str) -> str:
    """Return a comparable, deterministic form of `name`. Empty string on no input."""
    if not name:
        return ""
    s = _ascii_fold(name).lower()
    # Strip multi-word noise first so we don't lose the team name's core.
    for noise in _NOISE_TOKENS:
        if " " in noise:
            s = s.replace(noise, " ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)  # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    tokens = _strip_noise(s.split())
    normalized = " ".join(tokens)
    aliases = _load_aliases()
    return aliases.get(normalized, normalized)


def _preprocess_title(raw: str) -> str:
    """Trim sport-prefix headers and trailing competition descriptors.

    Polymarket titles often look like:
        "LoL: Team A vs Team B (BO3) - LPL Group Ascend"
        "T20 Series X vs Y: Real Team A vs Real Team B"
    The actual head-to-head is right of the last ":" (when present), and we
    drop trailing parenthesized format hints + " - <league descriptor>".
    """
    s = raw.strip()
    if ":" in s:
        # Prefer the rightmost colon-split half — that's the actual match.
        s = s.rsplit(":", 1)[1].strip()
    # Drop trailing " - <descriptor>" once.
    if " - " in s:
        head, _, tail = s.rpartition(" - ")
        # Only drop the tail if the head still contains a vs/x/-/@ separator.
        if any(sep in head.lower() for sep in (" vs", " v ", " x ", " @ ")):
            s = head
    # Drop trailing parens like "(BO3)", "(GAME 1)" etc.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return s


def extract_teams_from_title(title: str) -> tuple[str, str] | None:
    """Return (home_normalized, away_normalized) or None when not parseable.

    `home`/`away` are taken at face value from the title — Polymarket titles
    don't actually encode which side is home, so callers should treat the
    order as arbitrary and check both orderings when matching.
    """
    if not title:
        return None
    raw = title.strip()
    low = raw.lower()
    if any(hint in low for hint in _TITLE_REJECT_HINTS):
        return None
    raw = _preprocess_title(raw)
    for pat in _TITLE_PATTERNS:
        m = pat.match(raw)
        if not m:
            continue
        home = normalize_team(m.group("home"))
        away = normalize_team(m.group("away"))
        if home and away and home != away:
            return home, away
    return None
