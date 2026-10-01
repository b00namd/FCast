"""Card attributes from FUTBIN and the play value on top of them."""

from pathlib import Path

import pytest

from fcast.analysis import playvalue as pv
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
