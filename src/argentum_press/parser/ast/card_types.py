# pyright: basic
"""Card-type, subtype, and supertype dataclasses with their backing enums.

Renamed from ``types.py`` so as not to shadow the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Card-type enum + dataclass
# ---------------------------------------------------------------------------


class CardTypeEnum(Enum):
    ARTIFACT = "artifact"
    CONSPIRACY = "conspiracy"
    CREATURE = "creature"
    ENCHANTMENT = "enchantment"
    INSTANT = "instant"
    LAND = "land"
    PHENOMENON = "phenomenon"
    PLANE = "plane"
    PLANESWALKER = "planeswalker"
    SCHEME = "scheme"
    SORCERY = "sorcery"
    TRIBAL = "tribal"
    VANGUARD = "vanguard"


_PERMANENT_TYPES = {
    CardTypeEnum.ARTIFACT,
    CardTypeEnum.CREATURE,
    CardTypeEnum.ENCHANTMENT,
    CardTypeEnum.LAND,
    CardTypeEnum.PLANESWALKER,
}
_SUPPLEMENTARY_TYPES = {
    CardTypeEnum.CONSPIRACY,
    CardTypeEnum.PHENOMENON,
    CardTypeEnum.PLANE,
    CardTypeEnum.SCHEME,
    CardTypeEnum.VANGUARD,
}


@dataclass(frozen=True, slots=True)
class CardType:
    """A Magic card type such as Creature or Sorcery.

    ``value`` is a :class:`CardTypeEnum` ordinarily, or a free-form string
    for a custom (user-defined) type.
    """

    value: CardTypeEnum | str

    @property
    def is_custom(self) -> bool:
        return isinstance(self.value, str)

    @property
    def is_permanent_type(self) -> bool:
        return self.value in _PERMANENT_TYPES

    @property
    def is_supplementary_type(self) -> bool:
        return self.value in _SUPPLEMENTARY_TYPES

    @property
    def is_instant_or_sorcery(self) -> bool:
        return self.value in {CardTypeEnum.INSTANT, CardTypeEnum.SORCERY}


# ---------------------------------------------------------------------------
# Subtype enums + dataclass
# ---------------------------------------------------------------------------


class SpellSubtypeEnum(Enum):
    ARCANE = "arcane"
    TRAP = "trap"


class LandSubtypeEnum(Enum):
    DESERT = "desert"
    FOREST = "forest"
    GATE = "gate"
    ISLAND = "island"
    LAIR = "lair"
    LOCUS = "locus"
    MINE = "mine"
    MOUNTAIN = "mountain"
    PLAINS = "plains"
    POWER_PLANT = "power-plant"
    SWAMP = "swamp"
    TOWER = "tower"
    URZAS = "urza's"


class ArtifactSubtypeEnum(Enum):
    CLUE = "clue"
    CONTRAPTION = "contraption"
    EQUIPMENT = "equipment"
    FORTIFICATION = "fortification"
    TREASURE = "treasure"
    VEHICLE = "vehicle"


class EnchantmentSubtypeEnum(Enum):
    AURA = "aura"
    CARTOUCHE = "cartouche"
    CURSE = "curse"
    SAGA = "saga"
    SHRINE = "shrine"


class PlaneswalkerSubtypeEnum(Enum):
    AJANI = "ajani"
    AMINATOU = "aminatou"
    ANGRATH = "angrath"
    ARLINN = "arlinn"
    ASHIOK = "ashiok"
    BOLAS = "bolas"
    CHANDRA = "chandra"
    DACK = "dack"
    DARETTI = "daretti"
    DOMRI = "domri"
    DOVIN = "dovin"
    ELSPETH = "elspeth"
    ESTRID = "estrid"
    FREYALISE = "freyalise"
    GARRUK = "garruk"
    GIDEON = "gideon"
    HUATLI = "huatli"
    JACE = "jace"
    JAYA = "jaya"
    KARN = "karn"
    KAYA = "kaya"
    KIORA = "kiora"
    KOTH = "koth"
    LILIANA = "liliana"
    NAHIRI = "nahiri"
    NARSET = "narset"
    NISSA = "nissa"
    NIXILIS = "nixilis"
    RAL = "ral"
    ROWAN = "rowan"
    SAHEELI = "saheeli"
    SAMUT = "samut"
    SARKHAN = "sarkhan"
    SORIN = "sorin"
    TAMIYO = "tamiyo"
    TEFERI = "teferi"
    TEZZERET = "tezzeret"
    TIBALT = "tibalt"
    UGIN = "ugin"
    VENSER = "venser"
    VIVIEN = "vivien"
    VRASKA = "vraska"
    WILL = "will"
    WINDGRACE = "windgrace"
    XENAGOS = "xenagos"
    YANGGU = "yanggu"
    YANLING = "yanling"


class CreatureSubtypeEnum(Enum):
    # Creatures + tribals share their subtype list. The full canonical list
    # below is deliberately preserved (Reed enumerated all of them).
    ADVISOR = "advisor"
    AETHERBORN = "aetherborn"
    ALLY = "ally"
    ANGEL = "angel"
    ANTELOPE = "antelope"
    APE = "ape"
    ARCHER = "archer"
    ARCHON = "archon"
    ARTIFICER = "artificer"
    ASSASSIN = "assassin"
    ASSEMBLY_WORKER = "assembly-worker"
    ATOG = "atog"
    AUROCHS = "aurochs"
    AVATAR = "avatar"
    AZRA = "azra"
    BADGER = "badger"
    BARBARIAN = "barbarian"
    BASILISK = "basilisk"
    BAT = "bat"
    BEAR = "bear"
    BEAST = "beast"
    BEEBLE = "beeble"
    BERSERKER = "berserker"
    BIRD = "bird"
    BLINKMOTH = "blinkmoth"
    BOAR = "boar"
    BRINGER = "bringer"
    BRUSHWAGG = "brushwagg"
    CAMARID = "camarid"
    CAMEL = "camel"
    CARIBOU = "caribou"
    CARRIER = "carrier"
    CAT = "cat"
    CENTAUR = "centaur"
    CEPHALID = "cephalid"
    CHIMERA = "chimera"
    CITIZEN = "citizen"
    CLERIC = "cleric"
    COCKATRICE = "cockatrice"
    CONSTRUCT = "construct"
    COWARD = "coward"
    CRAB = "crab"
    CROCODILE = "crocodile"
    CYCLOPS = "cyclops"
    DAUTHI = "dauthi"
    DEMON = "demon"
    DESERTER = "deserter"
    DEVIL = "devil"
    DINOSAUR = "dinosaur"
    DJINN = "djinn"
    DRAGON = "dragon"
    DRAKE = "drake"
    DREADNOUGHT = "dreadnought"
    DRONE = "drone"
    DRUID = "druid"
    DRYAD = "dryad"
    DWARF = "dwarf"
    EFREET = "efreet"
    EGG = "egg"
    ELDER = "elder"
    ELDRAZI = "eldrazi"
    ELEMENTAL = "elemental"
    ELEPHANT = "elephant"
    ELF = "elf"
    ELK = "elk"
    EYE = "eye"
    FAERIE = "faerie"
    FERRET = "ferret"
    FISH = "fish"
    FLAGBEARER = "flagbearer"
    FOX = "fox"
    FROG = "frog"
    FUNGUS = "fungus"
    GARGOYLE = "gargoyle"
    GERM = "germ"
    GIANT = "giant"
    GNOME = "gnome"
    GOAT = "goat"
    GOBLIN = "goblin"
    GOD = "god"
    GOLEM = "golem"
    GORGON = "gorgon"
    GRAVEBORN = "graveborn"
    GREMLIN = "gremlin"
    GRIFFIN = "griffin"
    HAG = "hag"
    HARPY = "harpy"
    HELLION = "hellion"
    HIPPO = "hippo"
    HIPPOGRIFF = "hippogriff"
    HOMARID = "homarid"
    HOMUNCULUS = "homunculus"
    HORROR = "horror"
    HORSE = "horse"
    HOUND = "hound"
    HUMAN = "human"
    HYDRA = "hydra"
    HYENA = "hyena"
    ILLUSION = "illusion"
    IMP = "imp"
    INCARNATION = "incarnation"
    INSECT = "insect"
    JACKAL = "jackal"
    JELLYFISH = "jellyfish"
    JUGGERNAUT = "juggernaut"
    KAVU = "kavu"
    KIRIN = "kirin"
    KITHKIN = "kithkin"
    KNIGHT = "knight"
    KOBOLD = "kobold"
    KOR = "kor"
    KRAKEN = "kraken"
    LAMIA = "lamia"
    LAMMASU = "lammasu"
    LEECH = "leech"
    LEVIATHAN = "leviathan"
    LHURGOYF = "lhurgoyf"
    LICID = "licid"
    LIZARD = "lizard"
    MANTICORE = "manticore"
    MASTICORE = "masticore"
    MERCENARY = "mercenary"
    MERFOLK = "merfolk"
    METATHRAN = "metathran"
    MINION = "minion"
    MINOTAUR = "minotaur"
    MOLE = "mole"
    MONGER = "monger"
    MONGOOSE = "mongoose"
    MONK = "monk"
    MONKEY = "monkey"
    MOONFOLK = "moonfolk"
    MUTANT = "mutant"
    MYR = "myr"
    MYSTIC = "mystic"
    NAGA = "naga"
    NAUTILUS = "nautilus"
    NEPHILIM = "nephilim"
    NIGHTMARE = "nightmare"
    NIGHTSTALKER = "nightstalker"
    NINJA = "ninja"
    NOGGLE = "noggle"
    NOMAD = "nomad"
    NYMPH = "nymph"
    OCTOPUS = "octopus"
    OGRE = "ogre"
    OOZE = "ooze"
    ORB = "orb"
    ORC = "orc"
    ORGG = "orgg"
    OUPHE = "ouphe"
    OX = "ox"
    OYSTER = "oyster"
    PANGOLIN = "pangolin"
    PEGASUS = "pegasus"
    PENTAVITE = "pentavite"
    PEST = "pest"
    PHELDDAGRIF = "phelddagrif"
    PHOENIX = "phoenix"
    PILOT = "pilot"
    PINCHER = "pincher"
    PIRATE = "pirate"
    PLANT = "plant"
    PRAETOR = "praetor"
    PRISM = "prism"
    PROCESSOR = "processor"
    RABBIT = "rabbit"
    RAT = "rat"
    REBEL = "rebel"
    REFLECTION = "reflection"
    RHINO = "rhino"
    RIGGER = "rigger"
    ROGUE = "rogue"
    SABLE = "sable"
    SALAMANDER = "salamander"
    SAMURAI = "samurai"
    SAND = "sand"
    SAPROLING = "saproling"
    SATYR = "satyr"
    SCARECROW = "scarecrow"
    SCION = "scion"
    SCORPION = "scorpion"
    SCOUT = "scout"
    SERF = "serf"
    SERPENT = "serpent"
    SERVO = "servo"
    SHADE = "shade"
    SHAMAN = "shaman"
    SHAPESHIFTER = "shapeshifter"
    SHEEP = "sheep"
    SIREN = "siren"
    SKELETON = "skeleton"
    SLITH = "slith"
    SLIVER = "sliver"
    SLUG = "slug"
    SNAKE = "snake"
    SOLDIER = "soldier"
    SOLTARI = "soltari"
    SPAWN = "spawn"
    SPECTER = "specter"
    SPELLSHAPER = "spellshaper"
    SPHINX = "sphinx"
    SPIDER = "spider"
    SPIKE = "spike"
    SPIRIT = "spirit"
    SPLINTER = "splinter"
    SPONGE = "sponge"
    SQUID = "squid"
    SQUIRREL = "squirrel"
    STARFISH = "starfish"
    SURRAKAR = "surrakar"
    SURVIVOR = "survivor"
    TETRAVITE = "tetravite"
    THALAKOS = "thalakos"
    THOPTER = "thopter"
    THRULL = "thrull"
    TREEFOLK = "treefolk"
    TRILOBITE = "trilobite"
    TRISKELAVITE = "triskelavite"
    TROLL = "troll"
    TURTLE = "turtle"
    UNICORN = "unicorn"
    VAMPIRE = "vampire"
    VEDALKEN = "vedalken"
    VIASHINO = "viashino"
    VOLVER = "volver"
    WALL = "wall"
    WARRIOR = "warrior"
    WEIRD = "weird"
    WEREWOLF = "werewolf"
    WHALE = "whale"
    WIZARD = "wizard"
    WOLF = "wolf"
    WOLVERINE = "wolverine"
    WOMBAT = "wombat"
    WORM = "worm"
    WRAITH = "wraith"
    WURM = "wurm"
    YETI = "yeti"
    ZOMBIE = "zombie"
    ZUBERA = "zubera"


class PlanarSubtypeEnum(Enum):
    ALARA = "alara"
    ARKHOS = "arkhos"
    AZGOL = "azgol"
    BELENON = "belenon"
    BOLASS_MEDITATION_REALM = "bolas's meditation realm"
    DOMINARIA = "dominaria"
    EQUILOR = "equilor"
    ERGAMON = "ergamon"
    FABACIN = "fabacin"
    INNISTRAD = "innistrad"
    IQUATANA = "iquatana"
    IR = "ir"
    KALDHEIM = "kaldheim"
    KAMIGAWA = "kamigawa"
    KARSUS = "karsus"
    KEPHALAI = "kephalai"
    KINSHALA = "kinshala"
    KOLBAHAN = "kolbahan"
    KYNETH = "kyneth"
    LORWYN = "lorwyn"
    LUVION = "luvion"
    MERCADIA = "mercadia"
    MIRRODIN = "mirrodin"
    MOAG = "moag"
    MONGSENG = "mongseng"
    MURAGANDA = "muraganda"
    NEW_PHYREXIA = "new phyrexia"
    PHYREXIA = "phyrexia"
    PYRULEA = "pyrulea"
    RABIAH = "rabiah"
    RATH = "rath"
    RAVNICA = "ravnica"
    REGATHA = "regatha"
    SEGOVIA = "segovia"
    SERRAS_REALM = "serra's realm"
    SHADOWMOOR = "shadowmoor"
    SHANDALAR = "shandalar"
    ULGROTHA = "ulgrotha"
    VALLA = "valla"
    VRYN = "vryn"
    WILDFIRE = "wildfire"
    XEREX = "xerex"
    ZENDIKAR = "zendikar"


SubtypeEnumValue = (
    SpellSubtypeEnum
    | LandSubtypeEnum
    | ArtifactSubtypeEnum
    | EnchantmentSubtypeEnum
    | PlaneswalkerSubtypeEnum
    | CreatureSubtypeEnum
    | PlanarSubtypeEnum
)


@dataclass(frozen=True, slots=True)
class Subtype:
    """A subtype such as Elephant or Equipment.

    ``value`` is one of the subtype enum classes, or a free-form string for
    a custom subtype.
    """

    value: SubtypeEnumValue | str

    @property
    def is_custom(self) -> bool:
        return isinstance(self.value, str)

    @property
    def is_spell_subtype(self) -> bool:
        return isinstance(self.value, SpellSubtypeEnum)

    @property
    def is_artifact_subtype(self) -> bool:
        return isinstance(self.value, ArtifactSubtypeEnum)

    @property
    def is_land_subtype(self) -> bool:
        return isinstance(self.value, LandSubtypeEnum)

    @property
    def is_enchantment_subtype(self) -> bool:
        return isinstance(self.value, EnchantmentSubtypeEnum)

    @property
    def is_planeswalker_subtype(self) -> bool:
        return isinstance(self.value, PlaneswalkerSubtypeEnum)

    @property
    def is_creature_subtype(self) -> bool:
        return isinstance(self.value, CreatureSubtypeEnum)

    @property
    def is_planar_subtype(self) -> bool:
        return isinstance(self.value, PlanarSubtypeEnum)


# ---------------------------------------------------------------------------
# Supertype enum + dataclass
# ---------------------------------------------------------------------------


class SupertypeEnum(Enum):
    BASIC = "basic"
    LEGENDARY = "legendary"
    ONGOING = "ongoing"
    SNOW = "snow"
    WORLD = "world"


@dataclass(frozen=True, slots=True)
class Supertype:
    """A supertype such as Legendary or Snow."""

    value: SupertypeEnum | str

    @property
    def is_custom(self) -> bool:
        return isinstance(self.value, str)
