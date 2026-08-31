from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field

from dst_server.models.base import FrozenModel

type Frequency = Literal["never", "rare", "default", "often", "always"]
type WorldgenFrequency = Literal[
    "never",
    "rare",
    "uncommon",
    "default",
    "often",
    "mostly",
    "always",
    "insane",
]
type OceanWorldgenFrequency = Literal[
    "ocean_never",
    "ocean_rare",
    "ocean_uncommon",
    "ocean_default",
    "ocean_often",
    "ocean_mostly",
    "ocean_always",
    "ocean_insane",
]
type YesNo = Literal["never", "default"]
type AutoDetect = Literal["never", "default", "always"]
type RegrowthSpeed = Literal[
    "never",
    "veryslow",
    "slow",
    "default",
    "fast",
    "veryfast",
]
type AtriumSpeed = Literal["veryslow", "slow", "default", "fast", "veryfast"]
type EnabledDisabled = Literal["none", "always"]
type EventSetting = Literal["default", "enabled"]
type SeasonLength = Literal[
    "noseason",
    "veryshortseason",
    "shortseason",
    "default",
    "longseason",
    "verylongseason",
    "random",
]
type DamageSetting = Literal["nonlethal", "default"]
type WorldSize = Literal["small", "medium", "default", "huge"]
type ResetTime = Literal["none", "slow", "default", "fast", "always"]
type Petrification = Literal["none", "few", "default", "many", "max"]
type SpecialEvent = Literal["none", "default"]
type Branching = Literal["never", "least", "default", "most", "random"]
type SpawnMode = Literal["fixed", "scatter"]
type SeasonStart = Literal[
    "default",
    "winter",
    "spring",
    "summer",
    "autumn|spring",
    "winter|summer",
    "autumn|winter|spring|summer",
]
type Day = Literal[
    "default",
    "longday",
    "longdusk",
    "longnight",
    "noday",
    "nodusk",
    "nonight",
    "onlyday",
    "onlydusk",
    "onlynight",
]
type DropEverything = Literal["default", "always"]
type StartingSwaps = Literal["classic", "default", "highly random"]
type DamageTaken = Literal["always", "none", "more"]
type ExtraStartingItems = Literal["0", "5", "default", "15", "20", "none"]
type ForestTaskSet = Literal["default", "classic"]
type CaveTaskSet = Literal["cave_default"]
type ForestStartLocation = Literal["default", "plus", "darkness"]
type CaveStartLocation = Literal["caves"]
type LayoutMode = Literal["LinkNodesByKeys", "RestrictNodesByKey"]
type DiseaseDelay = Literal["none", "random", "long", "default", "short"]
type UnitInterval = Annotated[int | float, Field(ge=0, le=1)]


def _validate_world_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "world override strings must contain valid UTF-8"
        raise ValueError(msg) from error
    return value


type WormholePrefab = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_validate_world_string),
]

type ForestPreset = Literal[
    "SURVIVAL_TOGETHER",
    "RELAXED",
    "ENDLESS",
    "WILDERNESS",
    "LIGHTS_OUT",
    "COMPLETE_DARKNESS",
    "TERRARIA",
    "SURVIVAL_TOGETHER_CLASSIC",
    "SURVIVAL_DEFAULT_PLUS",
]
type CavePreset = Literal["DST_CAVE", "DST_CAVE_PLUS", "TERRARIA_CAVE"]


class WorldOverrides(FrozenModel):
    model_config = ConfigDict(revalidate_instances="never")


class _CommonWorldOverrides(WorldOverrides):
    autumn: SeasonLength = "default"
    basicresource_regrowth: EnabledDisabled = "none"
    beefaloheat: Frequency = "default"
    brightmarecreatures: Frequency = "default"
    crow_carnival: EventSetting = "default"
    darkness: DamageSetting = "default"
    day: Day = "default"
    dropeverythingondespawn: DropEverything = "default"
    extrastartingitems: ExtraStartingItems = "default"
    ghostenabled: EnabledDisabled = "always"
    ghostsanitydrain: EnabledDisabled = "always"
    hallowed_nights: EventSetting = "default"
    healthpenalty: EnabledDisabled = "always"
    hunger: DamageSetting = "default"
    krampus: Frequency = "default"
    lessdamagetaken: DamageTaken = "none"
    portalresurection: EnabledDisabled = "none"
    resettime: ResetTime = "default"
    season_start: SeasonStart = "default"
    seasonalstartingitems: YesNo = "default"
    shadowcreatures: Frequency = "default"
    spawnmode: SpawnMode = "fixed"
    spawnprotection: AutoDetect = "default"
    specialevent: SpecialEvent = "default"
    spring: SeasonLength = "default"
    summer: SeasonLength = "default"
    temperaturedamage: DamageSetting = "default"
    winter: SeasonLength = "default"
    winters_feast: EventSetting = "default"
    year_of_the_beefalo: EventSetting = "default"
    year_of_the_bunnyman: EventSetting = "default"
    year_of_the_carrat: EventSetting = "default"
    year_of_the_catcoon: EventSetting = "default"
    year_of_the_dragonfly: EventSetting = "default"
    year_of_the_gobbler: EventSetting = "default"
    year_of_the_knight: EventSetting = "default"
    year_of_the_pig: EventSetting = "default"
    year_of_the_snake: EventSetting = "default"
    year_of_the_varg: EventSetting = "default"


class _SharedWorldOverrides(_CommonWorldOverrides):
    bats_setting: Frequency = "default"
    berrybush: WorldgenFrequency = "default"
    boons: WorldgenFrequency = "default"
    branching: Branching = "default"
    bunnymen_setting: Frequency = "default"
    chess: WorldgenFrequency = "default"
    evergreen_regrowth: RegrowthSpeed = "default"
    flint: WorldgenFrequency = "default"
    fruitfly: Frequency = "default"
    grass: WorldgenFrequency = "default"
    grassgekkos: Frequency = "default"
    liefs: Frequency = "default"
    loop: AutoDetect = "default"
    marshbush: WorldgenFrequency = "default"
    merms: Frequency = "default"
    moles_setting: Frequency = "default"
    moon_spider: Frequency = "default"
    mushroom: WorldgenFrequency = "default"
    mutated_birds: YesNo = "default"
    mutated_merm: YesNo = "default"
    mutated_spiderqueen: YesNo = "default"
    pigs_setting: Frequency = "default"
    prefabswaps_start: StartingSwaps = "default"
    reeds: WorldgenFrequency = "default"
    reeds_regrowth: RegrowthSpeed = "default"
    regrowth: RegrowthSpeed = "default"
    rock: WorldgenFrequency = "default"
    sapling: WorldgenFrequency = "default"
    spider_warriors: YesNo = "default"
    spiderqueen: Frequency = "default"
    spiders: WorldgenFrequency = "default"
    spiders_setting: Frequency = "default"
    tentacles: WorldgenFrequency = "default"
    touchstone: WorldgenFrequency = "default"
    trees: WorldgenFrequency = "default"
    twiggytrees_regrowth: RegrowthSpeed = "default"
    weather: Frequency = "default"
    world_size: WorldSize = "default"


class _EventOverrides(_CommonWorldOverrides):
    boons: WorldgenFrequency = "never"
    keep_disconnected_tiles: bool
    layout_mode: LayoutMode = "RestrictNodesByKey"
    no_joining_islands: bool = True
    no_wormholes_to_disconnected_tiles: bool = True
    poi: WorldgenFrequency = "never"
    protected: WorldgenFrequency = "never"
    roads: YesNo = "never"
    traps: WorldgenFrequency = "never"
    touchstone: WorldgenFrequency = "never"
    world_size: WorldSize = "small"


class QuagmireOverrides(_EventOverrides):
    branching: Branching = "random"
    disease_delay: DiseaseDelay = "none"
    keep_disconnected_tiles: bool = False
    loop_percent: UnitInterval = 0
    petrification: Petrification = "none"
    prefabswaps_start: StartingSwaps = "classic"
    start_location: Literal["quagmire_startlocation"] = "quagmire_startlocation"
    task_set: Literal["quagmire_taskset"] = "quagmire_taskset"
    wildfires: Frequency = "never"


class LavaArenaOverrides(_EventOverrides):
    keep_disconnected_tiles: bool = True
    start_location: Literal["lavaarena"] = "lavaarena"
    task_set: Literal["lavaarena_taskset"] = "lavaarena_taskset"


class ForestOverrides(_SharedWorldOverrides):
    alternatehunt: Frequency = "default"
    angrybees: WorldgenFrequency = "default"
    antliontribute: Frequency = "default"
    balatro: YesNo = "default"
    bananabush_portalrate: Frequency = "default"
    bearger: Frequency = "default"
    beefalo: WorldgenFrequency = "default"
    beequeen: Frequency = "default"
    bees: WorldgenFrequency = "default"
    bees_setting: Frequency = "default"
    birds: Frequency = "default"
    butterfly: Frequency = "default"
    buzzard: WorldgenFrequency = "default"
    cactus: WorldgenFrequency = "default"
    cactus_regrowth: RegrowthSpeed = "default"
    carrot: WorldgenFrequency = "default"
    carrots_regrowth: RegrowthSpeed = "default"
    catcoon: WorldgenFrequency = "default"
    catcoons: Frequency = "default"
    cookiecutters: Frequency = "default"
    crabking: Frequency = "default"
    daywalker2: Frequency = "default"
    deciduousmonster: Frequency = "default"
    deciduoustree_regrowth: RegrowthSpeed = "default"
    deerclops: Frequency = "default"
    dragonfly: Frequency = "default"
    eyeofterror: Frequency = "default"
    fishschools: Frequency = "default"
    flowers: WorldgenFrequency = "default"
    flowers_regrowth: RegrowthSpeed = "default"
    frograin: Frequency = "default"
    frogs: Frequency = "default"
    gnarwail: Frequency = "default"
    goosemoose: Frequency = "default"
    has_ocean: bool = True
    hound_mounds: Frequency = "default"
    houndmound: WorldgenFrequency = "default"
    hounds: Frequency = "default"
    hunt: Frequency = "default"
    junkyard: YesNo = "default"
    keep_disconnected_tiles: bool = True
    klaus: Frequency = "default"
    layout_mode: LayoutMode = "LinkNodesByKeys"
    lightcrab_portalrate: Frequency = "default"
    lightning: Frequency = "default"
    lightninggoat: WorldgenFrequency = "default"
    lunarhail_frequency: Frequency = "default"
    lureplants: Frequency = "default"
    malbatross: Frequency = "default"
    merm: WorldgenFrequency = "default"
    meteorshowers: Frequency = "default"
    meteorspawner: WorldgenFrequency = "default"
    moles: WorldgenFrequency = "default"
    monkeytail_portalrate: Frequency = "default"
    moon_berrybush: WorldgenFrequency = "default"
    moon_bullkelp: WorldgenFrequency = "default"
    moon_carrot: WorldgenFrequency = "default"
    moon_fissure: WorldgenFrequency = "default"
    moon_fruitdragon: WorldgenFrequency = "default"
    moon_hotspring: WorldgenFrequency = "default"
    moon_rock: WorldgenFrequency = "default"
    moon_sapling: WorldgenFrequency = "default"
    moon_spiders: WorldgenFrequency = "default"
    moon_starfish: WorldgenFrequency = "default"
    moon_tree: WorldgenFrequency = "default"
    moon_tree_regrowth: RegrowthSpeed = "default"
    mosquitos: Frequency = "default"
    mutated_bearger: YesNo = "default"
    mutated_bird_gestalt: YesNo = "default"
    mutated_buzzard_gestalt: YesNo = "default"
    mutated_deerclops: YesNo = "default"
    mutated_hounds: YesNo = "default"
    mutated_warg: YesNo = "default"
    no_joining_islands: bool = True
    no_wormholes_to_disconnected_tiles: bool = True
    ocean_bullkelp: WorldgenFrequency = "default"
    ocean_otterdens: WorldgenFrequency = "default"
    ocean_seastack: OceanWorldgenFrequency = "ocean_default"
    ocean_shoal: WorldgenFrequency = "default"
    ocean_waterplant: OceanWorldgenFrequency = "ocean_default"
    ocean_wobsterden: WorldgenFrequency = "default"
    otters_setting: Frequency = "default"
    palmcone_seed_portalrate: Frequency = "default"
    palmconetree: WorldgenFrequency = "default"
    palmconetree_regrowth: RegrowthSpeed = "default"
    penguins: Frequency = "default"
    penguins_moon: YesNo = "default"
    perd: Frequency = "default"
    petrification: Petrification = "default"
    pigs: WorldgenFrequency = "default"
    pirateraids: Frequency = "default"
    ponds: WorldgenFrequency = "default"
    portal_spawnrate: Frequency = "default"
    powder_monkey_portalrate: Frequency = "default"
    rabbits: WorldgenFrequency = "default"
    rabbits_setting: Frequency = "default"
    rifts_enabled: AutoDetect = "default"
    rifts_frequency: Frequency = "default"
    roads: YesNo = "default"
    rock_ice: WorldgenFrequency = "default"
    saltstack_regrowth: RegrowthSpeed = "default"
    sharkboi: Frequency = "default"
    sharks: Frequency = "default"
    squid: Frequency = "default"
    stageplays: YesNo = "default"
    start_location: ForestStartLocation = "default"
    summerhounds: YesNo = "default"
    tallbirds: WorldgenFrequency = "default"
    task_set: ForestTaskSet = "default"
    terrariumchest: YesNo = "default"
    tumbleweed: WorldgenFrequency = "default"
    walrus: WorldgenFrequency = "default"
    walrus_setting: Frequency = "default"
    wanderingtrader_enabled: EnabledDisabled = "always"
    wasps: Frequency = "default"
    wildfires: Frequency = "default"
    winterhounds: YesNo = "default"
    wobsters: Frequency = "default"
    wormhole_prefab: WormholePrefab = "wormhole"


class CaveOverrides(_SharedWorldOverrides):
    acidrain_enabled: EnabledDisabled = "always"
    atriumgate: AtriumSpeed = "default"
    banana: WorldgenFrequency = "default"
    bats: WorldgenFrequency = "default"
    bunnymen: WorldgenFrequency = "default"
    cave_ponds: WorldgenFrequency = "default"
    cave_spiders: WorldgenFrequency = "default"
    cavelight: RegrowthSpeed = "default"
    chest_mimics: Frequency = "default"
    daywalker: Frequency = "default"
    dustmoths: Frequency = "default"
    earthquakes: Frequency = "default"
    fern: WorldgenFrequency = "default"
    fissure: WorldgenFrequency = "default"
    flower_cave: WorldgenFrequency = "default"
    flower_cave_regrowth: RegrowthSpeed = "default"
    has_ocean: bool | None = None
    itemmimics: Frequency = "default"
    keep_disconnected_tiles: bool | None = None
    layout_mode: LayoutMode = "RestrictNodesByKey"
    lichen: WorldgenFrequency = "default"
    lightflier_flower_regrowth: RegrowthSpeed = "default"
    lightfliers: Frequency = "default"
    molebats: Frequency = "default"
    monkey: WorldgenFrequency = "default"
    monkey_setting: Frequency = "default"
    mushgnome: Frequency = "default"
    mushtree: WorldgenFrequency = "default"
    mushtree_moon_regrowth: RegrowthSpeed = "default"
    mushtree_regrowth: RegrowthSpeed = "default"
    nightmarecreatures: Frequency = "default"
    no_joining_islands: bool | None = None
    no_wormholes_to_disconnected_tiles: bool | None = None
    rifts_enabled_cave: AutoDetect = "default"
    rifts_frequency_cave: Frequency = "default"
    rocky: WorldgenFrequency = "default"
    rocky_setting: Frequency = "default"
    slurper: WorldgenFrequency = "default"
    slurtles: WorldgenFrequency = "default"
    slurtles_setting: Frequency = "default"
    snurtles: Frequency = "default"
    spider_dropper: Frequency = "default"
    spider_hider: Frequency = "default"
    spider_spitter: Frequency = "default"
    start_location: CaveStartLocation = "caves"
    task_set: CaveTaskSet = "cave_default"
    toadstool: Frequency = "default"
    tree_rock: WorldgenFrequency = "default"
    tree_rock_regrowth: RegrowthSpeed = "default"
    wormhole_prefab: WormholePrefab = "tentacle_pillar"
    wormattacks: Frequency = "default"
    wormattacks_boss: Frequency = "default"
    wormlights: WorldgenFrequency = "default"
    worms: WorldgenFrequency = "default"
