"""Chem style groups for the colored chips in the dashboard (own design, no EA artwork)."""

# Group per chem style name as used by EA FC / FUTBIN. Unknown names fall back to "other".
CHEM_GROUPS: dict[str, str] = {
    # attacking
    "Sniper": "attack",
    "Finisher": "attack",
    "Deadeye": "attack",
    "Marksman": "attack",
    "Hawk": "attack",
    # pace
    "Hunter": "pace",
    "Catalyst": "pace",
    "Shadow": "pace",
    # midfield / passing
    "Artist": "midfield",
    "Architect": "midfield",
    "Powerhouse": "midfield",
    "Maestro": "midfield",
    "Engine": "midfield",
    # defending
    "Sentinel": "defense",
    "Guardian": "defense",
    "Gladiator": "defense",
    "Backbone": "defense",
    "Anchor": "defense",
    # goalkeeper
    "GK Basic": "keeper",
    "Wall": "keeper",
    "Shield": "keeper",
    "Cat": "keeper",
    "Glove": "keeper",
    # neutral
    "Basic": "basic",
}

GROUP_LABELS = {
    "attack": "Abschluss",
    "pace": "Tempo",
    "midfield": "Mittelfeld/Passspiel",
    "defense": "Defensive",
    "keeper": "Torwart",
    "basic": "Allround",
    "other": "Chemstyle",
}


def chem_group(name: str) -> str:
    return CHEM_GROUPS.get(name.strip(), "other")
