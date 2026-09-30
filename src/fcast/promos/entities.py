"""Finds players, leagues, nations and clubs in leak texts.

Players are matched against FUTBIN's player slugs (full names, 2-4 words); leagues, nations
and clubs against curated alias lists plus the clubs FCast already knows. Everything is a
suggestion the user confirms.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from fcast.sources.futbin_locator import slugify

# canonical name (as FUTBIN/EA use it) -> aliases (slugified on use)
LEAGUES: dict[str, tuple[str, ...]] = {
    "Premier League": ("premier league", "epl"),
    "LALIGA EA SPORTS": ("laliga", "la liga", "liga f"),
    "Serie A Enilive": ("serie a",),
    "Bundesliga": ("bundesliga",),
    "Ligue 1 McDonald's": ("ligue 1",),
    "Eredivisie": ("eredivisie",),
    "Liga Portugal": ("liga portugal", "primeira liga"),
    "MLS": ("mls", "major league soccer"),
    "ROSHN Saudi League": ("saudi pro league", "roshn saudi", "saudi league"),
    "Trendyol Süper Lig": ("super lig", "süper lig"),
    "Barclays WSL": ("wsl", "women's super league"),
    "Icons": ("icon", "icons"),
    "Heroes": ("hero", "heroes"),
}

NATIONS: dict[str, tuple[str, ...]] = {
    name: tuple(aliases)
    for name, *aliases in [
        ("Argentina", "argentinian", "argentinien"), ("Brazil", "brazilian", "brasilien"),
        ("England", "english"), ("France", "french", "frankreich"),
        ("Germany", "german", "deutschland"), ("Spain", "spanish", "spanien"),
        ("Portugal", "portuguese"), ("Italy", "italian", "italien"),
        ("Netherlands", "dutch", "holland", "niederlande"), ("Belgium", "belgian", "belgien"),
        ("Croatia", "croatian", "kroatien"), ("Uruguay", "uruguayan"),
        ("Colombia", "colombian", "kolumbien"), ("Norway", "norwegian", "norwegen"),
        ("Denmark", "danish", "dänemark"), ("Sweden", "swedish", "schweden"),
        ("Poland", "polish", "polen"), ("Morocco", "moroccan", "marokko"),
        ("Nigeria", "nigerian"), ("Senegal", "senegalese"), ("Egypt", "egyptian", "ägypten"),
        ("Japan", "japanese"), ("Korea Republic", "south korea", "korean", "südkorea"),
        ("United States", "usa", "usmnt", "american"), ("Mexico", "mexican", "mexiko"),
        ("Canada", "canadian", "kanada"), ("Scotland", "scottish", "schottland"),
        ("Wales", "welsh"), ("Austria", "austrian", "österreich"),
        ("Switzerland", "swiss", "schweiz"), ("Turkey", "turkish", "türkei"),
        ("Ukraine", "ukrainian"), ("Serbia", "serbian", "serbien"),
        ("Ghana", "ghanaian"), ("Ivory Coast", "ivorian", "côte d'ivoire", "elfenbeinküste"),
        ("Algeria", "algerian", "algerien"), ("Cameroon", "cameroonian", "kamerun"),
        ("Ecuador", "ecuadorian"), ("Chile", "chilean"),
    ]
}  # fmt: skip

CLUBS: dict[str, tuple[str, ...]] = {
    "Real Madrid": ("real madrid",), "FC Barcelona": ("barcelona", "barca", "barça"),
    "Atlético de Madrid": ("atletico madrid", "atlético madrid", "atleti"),
    "Manchester City": ("man city", "manchester city"),
    "Manchester United": ("man united", "man utd", "manchester united"),
    "Liverpool": ("liverpool",), "Arsenal": ("arsenal",), "Chelsea": ("chelsea",),
    "Tottenham Hotspur": ("tottenham", "spurs"), "Newcastle United": ("newcastle",),
    "Aston Villa": ("aston villa",), "FC Bayern München": ("bayern", "fc bayern"),
    "Borussia Dortmund": ("dortmund", "bvb"), "Bayer 04 Leverkusen": ("leverkusen",),
    "RB Leipzig": ("leipzig",), "Paris Saint-Germain": ("psg", "paris saint-germain"),
    "Olympique de Marseille": ("marseille",), "Juventus": ("juventus", "juve"),
    "Inter": ("inter milan", "inter"), "AC Milan": ("ac milan", "milan"),
    "Napoli": ("napoli",), "AS Roma": ("roma", "as roma"), "Benfica": ("benfica",),
    "FC Porto": ("porto",), "Sporting CP": ("sporting",), "Ajax": ("ajax",),
    "PSV": ("psv",), "Al Nassr": ("al nassr",), "Al Hilal": ("al hilal",),
    "Inter Miami CF": ("inter miami",), "Galatasaray": ("galatasaray",),
}  # fmt: skip


@dataclass
class Detection:
    players: list[str] = field(default_factory=list)  # FUTBIN slugs
    leagues: list[str] = field(default_factory=list)
    nations: list[str] = field(default_factory=list)
    clubs: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.players or self.leagues or self.nations or self.clubs)


def _find_aliases(
    tokens: list[str], table: dict[str, tuple[str, ...]], include_canonical: bool = True
) -> list[str]:
    found = []
    joined = f" {' '.join(tokens)} "
    for canonical, aliases in table.items():
        names = [*aliases, canonical] if include_canonical else list(aliases)
        for alias in names:
            slug = slugify(alias).replace("-", " ")
            if slug and f" {slug} " in joined:
                found.append(canonical)
                break
    return found


MAX_SURNAME_MATCHES = 3  # a surname matching more players than this is too ambiguous

_APOSTROPHE = chr(0x2019)  # typographic apostrophe used in names like N'Golo
_WORD_RE = re.compile(r"[^\W_][\w'" + _APOSTROPHE + r".-]*", re.UNICODE)


def _words(text: str) -> list[tuple[str, bool]]:
    """Slug tokens with a flag whether the original word was capitalized."""
    words: list[tuple[str, bool]] = []
    for word in _WORD_RE.findall(text):
        capital = word[:1].isupper()
        words.extend((token, capital) for token in slugify(word).split("-") if token)
    return words


def detect(text: str, player_slugs: Iterable[str], known_clubs: Iterable[str] = ()) -> Detection:
    """Find entities in `text`. `player_slugs` are FUTBIN slugs like 'martin-odegaard'."""
    words = _words(text)
    tokens = [token for token, _ in words]
    slug_set = set(player_slugs)
    by_surname: dict[str, list[str]] = {}
    for slug in slug_set:
        by_surname.setdefault(slug.rsplit("-", 1)[-1], []).append(slug)

    players: list[str] = []
    used: set[int] = set()
    # Full names first, longest first ("vinicius-junior" beats "junior").
    for size in (4, 3, 2):
        for start in range(len(tokens) - size + 1):
            if any(i in used for i in range(start, start + size)):
                continue
            candidate = "-".join(tokens[start : start + size])
            if candidate in slug_set and candidate not in players:
                players.append(candidate)
                used.update(range(start, start + size))
    # Then capitalized single words: one-word names ("rodri") or distinctive surnames
    # ("Ødegaard" -> "martin-odegaard"); common surnames are too ambiguous.
    for index, (token, capital) in enumerate(words):
        if index in used or not capital or len(token) < 4:
            continue
        matches = sorted(by_surname.get(token, []))
        if token in slug_set:
            matches = [token]
        if 0 < len(matches) <= MAX_SURNAME_MATCHES:
            players.extend(m for m in matches if m not in players)
            used.add(index)

    clubs_table = dict(CLUBS)
    for club in known_clubs:
        clubs_table.setdefault(club, (club,))
    return Detection(
        players=players,
        leagues=_find_aliases(tokens, LEAGUES),
        nations=_find_aliases(tokens, NATIONS),
        clubs=_find_aliases(tokens, clubs_table),
    )


def display_name(slug: str) -> str:
    """'martin-odegaard' -> 'Martin Odegaard' (for suggestions)."""
    return " ".join(part.capitalize() for part in re.split(r"-", slug))
