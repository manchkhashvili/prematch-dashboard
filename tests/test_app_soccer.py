"""
Phase 2 / C2.5.a regression tests for soccer multi-sport app.py behavior.

Covers:
  - _build_match_row: soccer 3-way ML populates cb_draw / pin_draw /
    pin_draw_fair / edge_draw_pct; basketball rows keep them None.
  - /api/matches: combines basketball + soccer rows, sorted by start_time
    across sports.
  - /api/opportunities: combines both sports' opps; each row carries a
    `sport` field; 3-way soccer +EV opps emitted; no ARB for 3-way ML.
  - /api/status: returns per-sport structure with both sports nested.
  - /api/unmatched: aggregates across sports with sport tag per row.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import app as app_mod  # noqa: E402
from src.matcher import MatchedEvent  # noqa: E402
from src.models import Odds  # noqa: E402

# NOTE: tests call endpoint async functions directly via asyncio.run rather
# than going through FastAPI's TestClient. TestClient triggers the lifespan
# handler which spawns 4 background poll tasks (Pinnacle + CB for each sport)
# — those try to hit live APIs and either hang on Playwright or block on the
# 15-s httpx timeout. Direct calls test the endpoint logic without that
# side effect.


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=4)


def _odds(source: str, sport: str, market_type: str, selections: dict, *,
          period: str = "FT", line: float | None = None,
          home: str = "Hawks", away: str = "Lakers",
          event_id: str = "evt-1",
          submarket: str | None = None,
          team_side: str | None = None,
          start_time: datetime = FUTURE) -> Odds:
    return Odds(
        source=source,  # type: ignore[arg-type]
        sport=sport,
        home=home,
        away=away,
        market_type=market_type,  # type: ignore[arg-type]
        period=period,  # type: ignore[arg-type]
        selections=selections,
        fetched_at=NOW,
        line=line,
        start_time=start_time,
        league="TestLeague",
        raw_event_id=event_id,
        submarket=submarket,  # type: ignore[arg-type]
        team_side=team_side,  # type: ignore[arg-type]
    )


# ── _build_match_row 3-way ML (soccer) ────────────────────────────────────────
class TestBuildMatchRow3Way:
    def test_soccer_3way_populates_draw_fields(self):
        cb_ml = _odds("crystalbet", "soccer", "moneyline",
                      {"home": 2.30, "draw": 3.20, "away": 3.00},
                      home="PSG", away="Arsenal", event_id="ev-soccer-1")
        pin_ml = _odds("pinnacle", "soccer", "moneyline",
                       {"home": 2.20, "draw": 3.10, "away": 2.95},
                       home="PSG", away="Arsenal", event_id="pin-1")
        match = MatchedEvent(
            cb=[cb_ml], pin=[pin_ml],
            home="PSG", away="Arsenal", score=100.0,
        )
        row = app_mod._build_match_row("PSG", "Arsenal", [cb_ml], match)

        assert row["sport"] == "soccer"
        # CB columns include draw
        assert row["cb_home"] == 2.30
        assert row["cb_draw"] == 3.20
        assert row["cb_away"] == 3.00
        # Pin vigged prices include draw
        assert row["pin_home"] == 2.20
        assert row["pin_draw"] == 3.10
        assert row["pin_away"] == 2.95
        # Fair decimals devigged via devig_3way → 3 fields populated, sum
        # of inverse-fair-decimals ≈ 1.0 (since fair_probs sum to 1).
        assert row["pin_home_fair"] is not None
        assert row["pin_draw_fair"] is not None
        assert row["pin_away_fair"] is not None
        prob_sum = (
            1.0 / row["pin_home_fair"]
            + 1.0 / row["pin_draw_fair"]
            + 1.0 / row["pin_away_fair"]
        )
        assert prob_sum == pytest.approx(1.0, abs=1e-9)
        # Edges populated (signed; positive when CB > fair)
        assert row["edge_home_pct"] is not None
        assert row["edge_draw_pct"] is not None
        assert row["edge_away_pct"] is not None

    def test_basketball_2way_leaves_draw_fields_null(self):
        cb_ml = _odds("crystalbet", "basketball", "moneyline",
                      {"home": 1.91, "away": 1.91})
        pin_ml = _odds("pinnacle", "basketball", "moneyline",
                       {"home": 1.91, "away": 1.91})
        match = MatchedEvent(
            cb=[cb_ml], pin=[pin_ml],
            home="Hawks", away="Lakers", score=100.0,
        )
        row = app_mod._build_match_row("Hawks", "Lakers", [cb_ml], match)
        # 2-way path: draw fields stay None
        assert row["cb_draw"] is None
        assert row["pin_draw"] is None
        assert row["pin_draw_fair"] is None
        assert row["edge_draw_pct"] is None
        # 2-way fields populated as before (regression)
        assert row["pin_home_fair"] is not None
        assert row["pin_away_fair"] is not None

    def test_soccer_3way_unmatched_leaves_pin_draw_null(self):
        cb_ml = _odds("crystalbet", "soccer", "moneyline",
                      {"home": 2.30, "draw": 3.20, "away": 3.00},
                      home="PSG", away="Arsenal")
        # No match → all Pin cols + edges stay None even though CB has draw.
        row = app_mod._build_match_row("PSG", "Arsenal", [cb_ml], match=None)
        assert row["cb_draw"] == 3.20  # CB side still populates
        assert row["pin_draw"] is None
        assert row["pin_draw_fair"] is None
        assert row["edge_draw_pct"] is None
        assert row["has_pin"] is False


# ── /api/matches across sports ────────────────────────────────────────────────
@pytest.fixture
def reset_state():
    """Restore _state to empty before/after each test so tests don't leak."""
    snapshot = {}
    for sport in app_mod.SPORT_NAMES:
        snapshot[sport] = {
            "cb": dict(app_mod._state[sport]["cb"]),
            "pin": dict(app_mod._state[sport]["pin"]),
        }
        app_mod._state[sport] = app_mod._empty_sport_state()
    yield
    for sport in app_mod.SPORT_NAMES:
        app_mod._state[sport] = snapshot[sport]


def _seed_basketball():
    """Single basketball event with 2-way ML, Pin matched."""
    cb = _odds("crystalbet", "basketball", "moneyline",
               {"home": 2.10, "away": 1.80},
               home="Hawks", away="Lakers",
               event_id="bb-1",
               start_time=FUTURE)
    pin = _odds("pinnacle", "basketball", "moneyline",
                {"home": 1.91, "away": 1.91},
                home="Hawks", away="Lakers",
                event_id="bb-pin",
                start_time=FUTURE)
    app_mod._state["basketball"]["cb"]["odds"] = [cb]
    app_mod._state["basketball"]["cb"]["count"] = 1
    app_mod._state["basketball"]["cb"]["fetched_at"] = NOW
    app_mod._state["basketball"]["pin"]["odds"] = [pin]
    app_mod._state["basketball"]["pin"]["count"] = 1
    app_mod._state["basketball"]["pin"]["fetched_at"] = NOW


def _seed_soccer():
    """Single soccer event with 3-way ML, Pin matched. Starts LATER than basketball."""
    later = FUTURE + timedelta(hours=2)
    cb = _odds("crystalbet", "soccer", "moneyline",
               {"home": 2.60, "draw": 3.20, "away": 3.00},
               home="PSG", away="Arsenal",
               event_id="sc-1", start_time=later)
    pin = _odds("pinnacle", "soccer", "moneyline",
                {"home": 2.20, "draw": 3.10, "away": 2.95},
                home="PSG", away="Arsenal",
                event_id="sc-pin", start_time=later)
    app_mod._state["soccer"]["cb"]["odds"] = [cb]
    app_mod._state["soccer"]["cb"]["count"] = 1
    app_mod._state["soccer"]["cb"]["fetched_at"] = NOW
    app_mod._state["soccer"]["pin"]["odds"] = [pin]
    app_mod._state["soccer"]["pin"]["count"] = 1
    app_mod._state["soccer"]["pin"]["fetched_at"] = NOW


class TestApiMatchesMultiSport:
    def test_combines_basketball_and_soccer_rows(self, reset_state):
        _seed_basketball()
        _seed_soccer()
        rows = asyncio.run(app_mod.api_matches())
        assert len(rows) == 2
        sports = {row["sport"] for row in rows}
        assert sports == {"basketball", "soccer"}

    def test_rows_sorted_by_start_time_across_sports(self, reset_state):
        # basketball starts at FUTURE; soccer starts FUTURE+2h. Sort asc.
        _seed_basketball()
        _seed_soccer()
        rows = asyncio.run(app_mod.api_matches())
        assert rows[0]["sport"] == "basketball"
        assert rows[1]["sport"] == "soccer"

    def test_soccer_row_includes_draw_columns(self, reset_state):
        _seed_soccer()
        rows = asyncio.run(app_mod.api_matches())
        soccer_row = next(r for r in rows if r["sport"] == "soccer")
        assert soccer_row["cb_draw"] == 3.20
        assert soccer_row["pin_draw"] == 3.10
        assert soccer_row["pin_draw_fair"] is not None
        assert soccer_row["edge_draw_pct"] is not None

    def test_basketball_row_draw_columns_null(self, reset_state):
        _seed_basketball()
        rows = asyncio.run(app_mod.api_matches())
        bb_row = next(r for r in rows if r["sport"] == "basketball")
        assert bb_row["cb_draw"] is None
        assert bb_row["pin_draw"] is None
        assert bb_row["edge_draw_pct"] is None

    def test_no_data_returns_empty(self, reset_state):
        assert asyncio.run(app_mod.api_matches()) == []


# ── /api/opportunities ────────────────────────────────────────────────────────
class TestApiOpportunities:
    def test_opps_carry_sport_tag(self, reset_state):
        _seed_basketball()
        _seed_soccer()
        opps = asyncio.run(app_mod.api_opportunities(min_edge=0.0, kind=None))
        # At least the +EV home side from soccer (CB 2.60 vs fair ~2.456 → +5.87%)
        # and the +EV home side from basketball (CB 2.10 vs fair 2.00 → +5%).
        sports = {o["sport"] for o in opps}
        assert "basketball" in sports
        assert "soccer" in sports

    def test_opps_sorted_by_edge_desc_across_sports(self, reset_state):
        _seed_basketball()
        _seed_soccer()
        opps = asyncio.run(app_mod.api_opportunities(min_edge=0.0, kind=None))
        # Top opp has highest edge across both sports
        edges = [o["edge_pct"] for o in opps]
        assert edges == sorted(edges, reverse=True)

    def test_no_arb_for_3way_moneyline(self, reset_state):
        _seed_soccer()
        opps = asyncio.run(app_mod.api_opportunities(min_edge=0.0, kind=None))
        soccer_arbs = [o for o in opps if o["sport"] == "soccer" and o["kind"] == "ARB"]
        assert soccer_arbs == []


# ── /api/status ───────────────────────────────────────────────────────────────
class TestApiStatus:
    def test_per_sport_structure(self, reset_state):
        _seed_basketball()
        _seed_soccer()
        body = asyncio.run(app_mod.api_status())
        assert "sports" in body
        assert "basketball" in body["sports"]
        assert "soccer" in body["sports"]
        bb = body["sports"]["basketball"]
        assert bb["cb"]["count"] == 1
        assert bb["pin"]["count"] == 1
        sc = body["sports"]["soccer"]
        assert sc["cb"]["count"] == 1
        assert sc["pin"]["count"] == 1
        # Expect basketball + soccer in sport_names (any other sports may also be wired in).
        assert "basketball" in body["config"]["sport_names"]
        assert "soccer" in body["config"]["sport_names"]


# ── /api/unmatched ────────────────────────────────────────────────────────────
class TestApiUnmatched:
    def test_unmatched_rows_carry_sport_tag(self, reset_state):
        # CB has events but Pin doesn't match — unmatched list populates.
        cb_bb = _odds("crystalbet", "basketball", "moneyline",
                      {"home": 1.91, "away": 1.91},
                      home="Hawks", away="Lakers")
        pin_bb = _odds("pinnacle", "basketball", "moneyline",
                       {"home": 1.91, "away": 1.91},
                       home="OtherTeam", away="DifferentTeam")
        app_mod._state["basketball"]["cb"]["odds"] = [cb_bb]
        app_mod._state["basketball"]["pin"]["odds"] = [pin_bb]

        cb_sc = _odds("crystalbet", "soccer", "moneyline",
                      {"home": 2.20, "draw": 3.10, "away": 2.95},
                      home="PSG", away="Arsenal")
        pin_sc = _odds("pinnacle", "soccer", "moneyline",
                       {"home": 2.20, "draw": 3.10, "away": 2.95},
                       home="UnknownClub", away="AnotherClub")
        app_mod._state["soccer"]["cb"]["odds"] = [cb_sc]
        app_mod._state["soccer"]["pin"]["odds"] = [pin_sc]

        rows = asyncio.run(app_mod.api_unmatched())
        # Both events should be unmatched (different names on Pin side).
        sports_in_rows = {row["sport"] for row in rows}
        assert "basketball" in sports_in_rows
        assert "soccer" in sports_in_rows
