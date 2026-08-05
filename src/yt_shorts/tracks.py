"""One vocabulary pack per racing venue, selected by an event.

A glossary term biases the decoder (see glossary.hotwords) and that bias is
NOT free: faster-whisper truncates the hotword prompt at 224 tokens, so a
global list of every circuit's corner names would be silently cut to a
fraction of itself. Scoping the vocabulary to the circuit an event actually
runs at is what makes the whole set affordable - and it is also what stops a
track-specific correction from firing on the wrong track, which is why
`carousel -> Karussell` is safe here and was not safe as an always-on default
(Road America, Sears Point and Watkins Glen each have a Carousel).

Pure logic, stdlib only, no file access - the same constraints glossary.py
carries, and for the same reason: this is data plus lookups, and its only
dependency is the layer format it hands its data to.

A pack is REFERENCED by an event, never copied into it, so correcting a name
here corrects every event at that venue with no migration.

One pack per VENUE, not per layout: GT7's 121 layouts collapse to 41
locations, and corner names belong to the place. Monza and Monza No Chicane
share a pack. The Nürburgring is the one deliberate split - its GP circuit and
the Nordschleife have entirely different corners, and the combined set is 249
tokens, over the limit before an operator adds a single name of their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .glossary import GlossaryLayer, parse_layer


@dataclass(frozen=True)
class TrackPack:
    """One venue's shipped vocabulary.

    `terms` bias the decoder before it errs; `replacements` correct what it
    already got wrong. A pack's replacements start EMPTY unless a mis-hearing
    has actually been OBSERVED in a transcript - inventing plausible ones for
    a venue nobody has transcribed yet would ship exactly the unmeasured rule
    this module exists to scope.

    Both collections are READ-ONLY, and that is load-bearing rather than
    tidiness: PACKS is a module-level singleton shared by every channel and
    every event for the life of the process, so a caller that mutated a
    pack's replacements would poison the shipped data for everyone until the
    studio restarts. `frozen=True` alone does NOT give that - it blocks
    rebinding the field, not mutating the dict the field points at - which is
    why replacements is a MappingProxyType and terms a tuple. This is the same
    hazard glossary.DEFAULT_LAYER's own comment names."""
    track_id: str
    name: str
    terms: tuple[str, ...]
    replacements: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def _pack(track_id: str, name: str, terms: list[str],
          replacements: dict[str, str] | None = None) -> TrackPack:
    return TrackPack(track_id=track_id, name=name, terms=tuple(terms),
                     replacements=MappingProxyType(dict(replacements or {})))


# Every pack below. Real circuits carry their published corner names; GT's own
# designs have no officially named corners, so theirs carry the venue and
# section names a commentator actually says instead ("Yamagiwa", "Seaside",
# "Nordwand") - which Whisper mangles just as reliably as a corner name would
# be mangled, and which come up far more often.
_ALL: list[TrackPack] = [
    # ---- Real circuits ----
    _pack("nurburgring-nordschleife", "Nürburgring Nordschleife", [
        "Nordschleife", "Hatzenbach", "Hocheichen", "Quiddelbacher Höhe",
        "Flugplatz", "Schwedenkreuz", "Aremberg", "Fuchsröhre",
        "Adenauer Forst", "Metzgesfeld", "Kallenhard", "Wehrseifen",
        "Ex-Mühle", "Bergwerk", "Kesselchen", "Klostertal", "Steilstrecke",
        "Karussell", "Hohe Acht", "Wippermann", "Eschbach", "Brünnchen",
        "Pflanzgarten", "Schwalbenschwanz", "Galgenkopf", "Döttinger Höhe",
        "Antoniusbuche", "Tiergarten", "Hohenrain", "Kleines Karussell",
        "Stefan-Bellof-S", "Mutkurve",
    ], {
        # Every key OBSERVED in the real V9nVNEQNdR4 transcript. `carousel` is
        # the reason this belongs to a venue rather than to an always-on
        # default: Road America, Sears Point and Watkins Glen each have a
        # Carousel of their own, and this rule must not reach them.
        "schwab schwanz": "Schwalbenschwanz",
        "shriver schwanz": "Schwalbenschwanz",
        "kleine carousel": "Kleines Karussell",
        "kleinica or sell": "Kleines Karussell",
        "carousel": "Karussell",
        "galgen cop": "Galgenkopf",
        "galbenkopf": "Galgenkopf",
        "geigenkop": "Galgenkopf",
        "kessichen": "Kesselchen",
        "boyacht": "Hohe Acht",
    }),
    _pack("nurburgring-gp", "Nürburgring Grand-Prix-Strecke", [
        "Mercedes-Arena", "Yokohama-S", "Ford-Kurve", "Goodyear-Kehre",
        "Michael-Schumacher-S", "Kumho-Kurve", "Warsteiner-Kurve", "RTL-Kurve",
        "Advan-Bogen", "Veedol-Schikane", "Coca-Cola-Kurve", "Valvoline-Kurve",
    ]),
    _pack("le-mans", "24 Heures du Mans Racing Circuit", [
        "Dunlop Curve", "Forest Esses", "Tertre Rouge", "Daytona Chicane",
        "Michelin Chicane", "Mulsanne Straight", "Hunaudières", "Mulsanne Corner",
        "Indianapolis", "Arnage", "Porsche Curves", "Maison Blanche",
        "Karting Esses", "Ford Chicanes", "Motul Turn",
    ]),
    _pack("monza", "Autodromo Nazionale Monza", [
        "Variante del Rettifilo", "Prima Variante", "Curva Grande",
        "Variante della Roggia", "Lesmo", "Variante Ascari", "Parabolica",
        "Curva Alboreto",
    ]),
    _pack("interlagos", "Autódromo de Interlagos", [
        "Senna S", "Curva do Sol", "Descida do Lago", "Ferradura", "Laranjinha",
        "Pinheirinho", "Bico de Pato", "Mergulho", "Junção", "Subida dos Boxes",
        "Arquibancadas",
    ]),
    _pack("autopolis", "Autopolis International Racing Course", [
        "Autopolis", "Nakayama Seimitsu Corner", "Astemo Corner", "Final Corner",
    ]),
    _pack("brands-hatch", "Brands Hatch", [
        "Paddock Hill Bend", "Druids", "Graham Hill Bend", "Surtees",
        "Hawthorn Bend", "Westfield Bend", "Sheene Curve", "Stirling's Bend",
        "Clark Curve", "Clearways", "McLaren",
    ]),
    _pack("gilles-villeneuve", "Circuit Gilles-Villeneuve", [
        "Senna S", "L'Épingle", "Casino Straight", "Wall of Champions",
    ]),
    _pack("barcelona-catalunya", "Circuit de Barcelona-Catalunya", [
        "Elf", "Renault", "Repsol", "Seat", "Würth", "Campsa", "La Caixa",
        "Banc Sabadell", "Europcar", "New Holland",
    ]),
    _pack("spa-francorchamps", "Circuit de Spa-Francorchamps", [
        "La Source", "Eau Rouge", "Raidillon", "Kemmel Straight", "Les Combes",
        "Malmedy", "Rivage", "Bruxelles", "Pouhon", "Fagnes", "Campus",
        "Stavelot", "Blanchimont", "Bus Stop",
    ]),
    _pack("daytona", "Daytona International Speedway", [
        "International Horseshoe", "Le Mans Chicane", "Tri-Oval", "Superstretch",
    ]),
    _pack("fuji", "Fuji International Speedway", [
        "TGR Corner", "Coca-Cola Corner", "100R", "Advan Corner", "Hairpin",
        "300R", "Dunlop Corner", "GR Supra Corner", "Panasonic Corner",
    ]),
    _pack("goodwood", "Goodwood Motor Circuit", [
        "Goodwood", "Madgwick", "Fordwater", "St Mary's", "Lavant", "Woodcote",
    ]),
    _pack("road-atlanta", "Michelin Raceway Road Atlanta", [
        "Road Atlanta", "The Esses",
    ]),
    _pack("mount-panorama", "Mount Panorama Motor Racing Circuit", [
        "Mount Panorama", "Hell Corner", "Griffins Bend", "The Cutting",
        "Quarry Corner", "Reid Park", "Sulman Park", "McPhillamy Park",
        "Brock's Skyline", "The Dipper", "Forrest's Elbow", "Conrod Straight",
        "The Chase", "Murray's Corner",
    ]),
    _pack("red-bull-ring", "Red Bull Ring", [
        "Red Bull Ring", "Niki Lauda Kurve", "Remus Kurve", "Rauch Kurve",
        "Graz Kurve", "Jochen Rindt Kurve",
    ]),
    _pack("suzuka", "Suzuka Circuit", [
        "Suzuka", "First Curve", "S Curves", "Degner", "Dunlop Curve",
        "Hairpin", "Spoon Curve", "130R", "Casio Triangle",
    ]),
    _pack("tsukuba", "Tsukuba Circuit", [
        "Tsukuba", "First Hairpin", "Dunlop Corner", "80R", "MC Corner",
        "Second Hairpin", "Final Corner",
    ]),
    _pack("watkins-glen", "Watkins Glen International", [
        "Watkins Glen", "The Esses", "The Chute", "Toe of the Boot",
        "Heel of the Boot", "Inner Loop", "The Boot",
    ]),
    _pack("laguna-seca", "WeatherTech Raceway Laguna Seca", [
        "Laguna Seca", "Andretti Hairpin", "The Corkscrew", "Rainey Curve",
    ]),
    _pack("willow-springs", "Willow Springs International Raceway", [
        "Willow Springs", "Big Willow", "Streets of Willow", "Horse Thief Mile",
        "Castrol Corner", "Rabbit's Ear", "The Omega", "Monroe Ridge",
        "Repass Pass", "The Sweeper",
    ]),
    _pack("yas-marina", "Yas Marina Circuit", [
        "Yas Marina", "North Hairpin", "Marsa Corner",
    ]),
    # ---- Polyphony's own designs: venue and section names ----
    _pack("alsace", "Alsace", ["Alsace", "Alsace Village"]),
    _pack("lago-maggiore", "Autodrome Lago Maggiore",
          ["Lago Maggiore", "Autodrome Lago Maggiore"]),
    _pack("blue-moon-bay", "Blue Moon Bay Speedway",
          ["Blue Moon Bay", "Blue Moon Bay Speedway"]),
    _pack("bb-raceway", "BB Raceway", ["BB Raceway"]),
    _pack("sainte-croix", "Circuit de Sainte-Croix",
          ["Sainte-Croix", "Circuit de Sainte-Croix"]),
    _pack("colorado-springs", "Colorado Springs", ["Colorado Springs"]),
    _pack("deep-forest", "Deep Forest Raceway",
          ["Deep Forest", "Deep Forest Raceway"]),
    _pack("dragon-trail", "Dragon Trail",
          ["Dragon Trail", "Dragon Trail Seaside", "Dragon Trail Gardens"]),
    _pack("eiger-nordwand", "Eiger Nordwand", ["Eiger Nordwand", "Eiger"]),
    _pack("fishermans-ranch", "Fishermans Ranch", ["Fishermans Ranch"]),
    _pack("grand-valley", "Grand Valley",
          ["Grand Valley", "Grand Valley Highway 1", "Grand Valley South"]),
    _pack("high-speed-ring", "High Speed Ring", ["High Speed Ring"]),
    _pack("kyoto-driving-park", "Kyoto Driving Park",
          ["Kyoto Driving Park", "Yamagiwa", "Miyabi"]),
    _pack("lake-louise", "Lake Louise", ["Lake Louise"]),
    _pack("northern-isle", "Northern Isle Speedway", ["Northern Isle Speedway"]),
    _pack("sardegna", "Sardegna",
          ["Sardegna", "Sardegna Windmills", "Sardegna Road Track"]),
    _pack("special-stage-route-x", "Special Stage Route X",
          ["Special Stage Route X", "Route X"]),
    _pack("tokyo-expressway", "Tokyo Expressway", ["Tokyo Expressway"]),
    _pack("trial-mountain", "Trial Mountain Circuit",
          ["Trial Mountain", "Trial Mountain Circuit"]),
]

PACKS: dict[str, TrackPack] = {pack.track_id: pack for pack in _ALL}

if len(PACKS) != len(_ALL):  # pragma: no cover - a data typo, caught at import
    raise ValueError("duplicate track id in the registry")


def get(track_id: str) -> TrackPack | None:
    """The pack for `track_id`, or None. Exact match only: the id comes from
    the studio's own selector or a hand-edited glossary.json, and quietly
    accepting a different case would hide a typo the caller should report."""
    return PACKS.get(track_id)


def as_layer(pack: TrackPack) -> GlossaryLayer:
    """A pack as a glossary layer, validated by glossary.parse_layer - the
    same function that validates a hand-written glossary.json, so a pack this
    module ships can never be one profile.load would refuse."""
    return parse_layer({"terms": list(pack.terms),
                        "replacements": dict(pack.replacements)})


def listing() -> list[dict]:
    """Every pack as `{"id", "name"}`, sorted by name - what the studio's
    track selector renders. Deliberately not the terms: the selector needs a
    name, and shipping ~40 packs' worth of vocabulary to the browser to fill a
    dropdown would be waste."""
    return sorted(({"id": pack.track_id, "name": pack.name} for pack in _ALL),
                  key=lambda row: row["name"])


# Every shipped pack is parsed at import, not lazily on the first as_layer
# call. The duplicate-id check above catches one class of data typo; this
# catches the rest - a control character, an over-long name, a term that
# collides with another after normalisation - and it catches them when the
# module is first imported rather than at an operator's next detection run.
# glossary.py builds DEFAULT_LAYER at import for exactly this reason, and
# without this loop the claim that shipped data cannot be data profile.load
# would refuse is only true for whichever packs someone happened to select.
for _shipped in _ALL:
    as_layer(_shipped)
del _shipped
