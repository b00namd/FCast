"""Card attributes from FUTBIN and the play value on top of them."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis import cards
from fcast.analysis import playvalue as pv
from fcast.analysis import signals as sig
from fcast.analysis.service import analyze_player
from fcast.analysis.stats import price_stats
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.session import create_db_engine, create_session_factory
from fcast.promos.scoring import CardInfo, PromoInfo, PromoWeights, score_card
from fcast.radar import signals as rs
from fcast.sources.base import CardAttributes
from fcast.sources.futbin import parse_attributes, parse_player

FIXTURES = Path(__file__).parent / "fixtures" / "futbin"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_attributes_from_the_futbin_page() -> None:
    olise = parse_attributes(page("22947-olise-totw.html"))
    assert olise is not None
    assert olise.stats["Acceleration"] == 86
    assert olise.stats["Pace"] == 84
    assert len(olise.stats) == 34
    assert olise.playstyles_plus == ("Finesse Shot",)
    assert "Tiki Taka" in olise.playstyles
    assert (olise.skills, olise.weak_foot, olise.height_cm) == (5, 3, 184)
    assert (olise.foot, olise.body_type, olise.accelerate) == ("Left", "Avg & Lean", "Controlled")
    maradona = parse_attributes(page("21487-maradona.html"))
    assert maradona is not None
    assert maradona.accelerate == "Explosive"
    assert maradona.playstyles_plus == ("Technical",)
    assert parse_attributes("<html></html>") is None
    # Part of the player info, stored as JSON.
    info = parse_player(page("21516-muller.html"), 1)
    assert info.attributes is not None
    assert CardAttributes.from_json(info.attributes.to_json()) == info.attributes
    assert CardAttributes.from_json("not json") is None


def test_play_value_of_real_cards() -> None:
    values = {}
    for name in ("21487-maradona.html", "21516-muller.html", "22947-olise-totw.html"):
        info = parse_player(page(name), 1)
        value = pv.play_value(info.attributes, info.position)
        assert value is not None
        values[name] = value
    assert values["21487-maradona.html"].group == pv.PLAYMAKER
    assert values["21516-muller.html"].group == pv.STRIKER
    assert values["21487-maradona.html"].score > values["22947-olise-totw.html"].score
    assert "PlayStyle+: Technical" in values["21487-maradona.html"].reasons
    assert "AcceleRATE Explosive" in values["21487-maradona.html"].reasons


def attrs(**changes: object) -> CardAttributes:
    stats = {name: 80 for name in pv.WEIGHTS[pv.STRIKER]}
    base = CardAttributes(stats=stats, skills=3, weak_foot=3, height_cm=180)
    return CardAttributes(**{**base.__dict__, **changes})  # type: ignore[arg-type]


def test_play_value_scale_and_bonuses() -> None:
    plain = pv.play_value(attrs(), "ST")
    assert plain is not None
    assert plain.base == 80
    assert plain.score == 50.0  # (80 - 55) / 40 * 80
    meta_plus = pv.play_value(attrs(playstyles=("Rapid",), playstyles_plus=("Rapid",)), "ST")
    other_plus = pv.play_value(attrs(playstyles=("Aerial",), playstyles_plus=("Aerial",)), "ST")
    assert meta_plus is not None and other_plus is not None
    assert meta_plus.score == 55.0  # PlayStyle+ that matters for a striker
    assert other_plus.score == 52.0
    traits = pv.play_value(attrs(skills=5, weak_foot=5, accelerate="Explosive"), "ST")
    assert traits is not None and traits.score == 58.0
    # PlayStyle+ give at most 12 points; all bonuses together at most 20.
    many = ("Rapid", "Quick Step", "Finesse Shot", "Power Shot")
    stats = {n: 99 for n in pv.WEIGHTS[pv.STRIKER]}
    plus_only = pv.play_value(attrs(stats=stats, playstyles=many, playstyles_plus=many), "ST")
    assert plus_only is not None and plus_only.score == 92.0
    everything = attrs(
        stats=stats,
        playstyles=many,
        playstyles_plus=many,
        skills=5,
        weak_foot=5,
        accelerate="Explosive",
    )
    best = pv.play_value(everything, "ST")
    assert best is not None and best.score == 100.0


def test_position_matters() -> None:
    defender_stats = {name: 85 for name in pv.WEIGHTS[pv.DEFENDER]}
    tall = pv.play_value(CardAttributes(stats=defender_stats, height_cm=192), "CB")
    short = pv.play_value(CardAttributes(stats=defender_stats, height_cm=175), "CB")
    assert tall is not None and short is not None
    assert tall.score - short.score == 4
    # A striker's stats say little about a centre back.
    assert pv.play_value(attrs(), "CB") is None
    assert pv.play_value(attrs(), None) is None
    assert pv.play_value(None, "ST") is None


def test_meta_score_and_usage_agreement() -> None:
    assert pv.meta_score(70, None) == 70
    assert pv.meta_score(70, 1.0) == 76
    assert pv.percentiles({1: 10.0, 2: 30.0, 3: 20.0}) == {1: 0.0, 2: 1.0, 3: 0.5}
    agreeing = [(float(v), float(v * 1000)) for v in range(10)]
    assert pv.usage_agreement(agreeing) == pytest.approx(1.0)
    opposite = [(float(v), float(-v)) for v in range(10)]
    assert pv.usage_agreement(opposite) == pytest.approx(-1.0)
    assert pv.usage_agreement(agreeing[:5]) is None


# --- in the signals ------------------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CFG = sig.SignalConfig()


def test_play_value_raises_the_uev_score() -> None:
    points = [(NOW - timedelta(hours=h), 100_000 - 50 * h) for h in range(168)]
    stats = price_stats(points, NOW)
    supply = sig.Supply((100_000, 108_000), 300_000)
    plain = sig.overprice_score(stats, supply, CFG)
    strong = sig.overprice_score(stats, supply, CFG, play=90)
    weak = sig.overprice_score(stats, supply, CFG, play=20)
    assert plain is not None and strong is not None and weak is not None
    assert strong.score > plain.score > weak.score
    assert "starke Karte (Spielwert 90)" in strong.reasons


def test_promo_link_scores_strong_cards_higher() -> None:
    promo = PromoInfo(1, "Promo", NOW + timedelta(days=3), None, 0.9, (("club", "Arsenal"),))

    def card(play: float | None) -> CardInfo:
        return CardInfo(1, "X", None, "Arsenal", None, None, play=play)

    weights = PromoWeights()
    scores = [score_card(card(p), promo, None, NOW, weights) for p in (None, 90.0, 20.0)]
    assert all(s is not None for s in scores)
    neutral, strong, weak = (s.score for s in scores if s is not None)
    assert strong > neutral > weak


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def settings() -> Settings:
    return Settings(_env_file=None, platform="pc", sources="")


def test_weak_card_dip_gets_a_warning(factory: sessionmaker[Session]) -> None:
    weak = CardAttributes(stats={name: 60 for name in pv.WEIGHTS[pv.STRIKER]})
    with factory.begin() as session:
        player = repo.upsert_player(
            session,
            1,
            repo.PlayerDetails(name="Schwach", position="ST", attributes_raw=weak.to_json()),
        )
        for hours in range(160, 0, -1):
            at = NOW - timedelta(hours=hours)
            repo.add_snapshot(session, player, Platform.PC, 10_000, "futbin", at)
        repo.add_snapshot(session, player, Platform.PC, 8_000, "futbin", NOW)
        result = analyze_player(session, player, settings(), NOW)
    assert result.play is not None and result.play.score < 40
    dip = next(s for s in result.signals if s.rule is sig.Rule.BUY_DIP)
    assert any("spielerisch schwach" in r for r in dip.reasons)


def test_undervalued_signal() -> None:
    cfg = rs.RadarConfig()
    signal = rs.undervalued(80, 15_000, 50_000, cfg)
    assert signal is not None
    assert signal.kind is rs.RadarKind.UNDERVALUED
    assert "ähnlich starke Karten kosten ~50.000, diese 15.000 (-70,0 %)" in signal.reasons[0]
    assert rs.undervalued(80, 40_000, 50_000, cfg) is None  # only 20 % cheaper
    assert rs.undervalued(50, 5_000, 50_000, cfg) is None  # not a strong card
    assert rs.undervalued(80, None, 50_000, cfg) is None
    assert rs.potential([signal], meta=80) == round(signal.score + 5, 1)


def test_price_fit_finds_what_quality_usually_costs(factory: sessionmaker[Session]) -> None:
    # 20 strikers: price doubles every 10 play-value points; one strong card is cheap.
    with factory.begin() as session:
        for i in range(20):
            level = 70 + i  # stat level -> play value rises with i
            stats = {name: level for name in pv.WEIGHTS[pv.STRIKER]}
            player = repo.upsert_player(
                session,
                100 + i,
                repo.PlayerDetails(
                    name=f"P{i}",
                    position="ST",
                    rating=84 + i % 3,
                    card_type="Gold Rare",
                    attributes_raw=CardAttributes(stats=stats).to_json(),
                    games_used=1_000 * (i + 1),
                ),
            )
            value = pv.play_value(CardAttributes(stats=stats), "ST")
            assert value is not None
            price = round(5_000 * 2 ** ((value.score - 30) / 10) * 1.1 ** (i % 3))
            if i == 18:
                price = 3_000  # the bargain
            repo.add_snapshot(session, player, Platform.PC, price, "futbin", NOW)
        values = cards.card_values(session, settings(), NOW)
    fit = cards.price_fit(values)
    assert fit is not None and fit.cards == 20
    bargain = next(v for v in values.values() if v.price == 3_000)
    expected = cards.expected_price(fit, bargain)
    assert expected is not None and expected > 20_000
    assert rs.undervalued(bargain.meta, bargain.price, expected, rs.RadarConfig()) is not None
    check = cards.agreement(values)
    assert (check.cards, check.basis) == (20, "Spiele gesamt, Preis herausgerechnet (Gold)")
    assert check.correlation is not None


def test_usage_is_compared_with_the_price_taken_out() -> None:
    """Cheap cards are played more; quality shows in the usage on top of that."""
    values = {}
    for i in range(12):
        expensive = i % 2 == 0
        price = 80_000 if expensive else 5_000
        play = 50.0 + i + (30 if expensive else 0)  # good cards are usually expensive
        games = round(1_000_000 / price * (1 + (play - 50) / 20))  # cheap -> more, good -> more
        value = pv.PlayValue(play, pv.STRIKER, 80.0, ())
        values[i] = cards.CardValue(i, value, play, price, None, games, 85, True)
    raw = pv.usage_agreement([(v.play.score, float(v.games or 0)) for v in values.values()])
    adjusted = cards.agreement(values)
    assert raw is not None and adjusted.correlation is not None
    assert raw < 0  # without taking the price out, good (expensive) cards look less played
    assert adjusted.correlation > 0.4  # with the price taken out, quality shows
    stats = {i: {"Pace": 70 + 2 * i, "Strength": 90 - i % 2} for i in values}
    drivers = cards.stat_drivers(values, stats)
    assert drivers[0][0] == "Pace" and drivers[0][1] > 0.8


def test_special_and_holo_cards_are_not_compared(factory: sessionmaker[Session]) -> None:
    stats = {name: 85 for name in pv.WEIGHTS[pv.STRIKER]}
    raw = CardAttributes(stats=stats).to_json()
    with factory.begin() as session:
        for ea_id, card_type in ((1, "Team of the Week"), (2, "Team of the Week (Holo)")):
            player = repo.upsert_player(
                session,
                ea_id,
                repo.PlayerDetails(
                    name="X", position="ST", rating=88, card_type=card_type, attributes_raw=raw
                ),
            )
            repo.add_snapshot(session, player, Platform.PC, 500_000, "futbin", NOW)
        values = cards.card_values(session, settings(), NOW)
    assert len(values) == 1  # the holo version is left out
    (value,) = values.values()
    assert not value.base_card
    fit = cards.PriceFit(0.0, 0.1, 0.0, 20)
    assert cards.expected_price(fit, value) is None  # special cards are a market of their own
