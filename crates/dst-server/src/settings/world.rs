// Native world customization choices and defaults.

choices! { ClusterLanguage {
    En => "en",
    Fr => "fr",
    Es => "es",
    Mex => "mex",
    Tr => "tr",
    De => "de",
    It => "it",
    Pt => "pt",
    Pl => "pl",
    Ru => "ru",
    Ko => "ko",
    Zh => "zh",
    Zht => "zht",
    Zhr => "zhr",
} }

choices! { Frequency {
    Never => "never",
    Rare => "rare",
    Default => "default",
    Often => "often",
    Always => "always",
} }

choices! { WorldgenFrequency {
    Never => "never",
    Rare => "rare",
    Uncommon => "uncommon",
    Default => "default",
    Often => "often",
    Mostly => "mostly",
    Always => "always",
    Insane => "insane",
} }

choices! { SeasonLength {
    Noseason => "noseason",
    Veryshortseason => "veryshortseason",
    Shortseason => "shortseason",
    Default => "default",
    Longseason => "longseason",
    Verylongseason => "verylongseason",
    Random => "random",
} }

choices! { YesNo {
    Never => "never",
    Default => "default",
} }

choices! { EnabledDisabled {
    None => "none",
    Always => "always",
} }

choices! { Branching {
    Never => "never",
    Least => "least",
    Default => "default",
    Most => "most",
    Random => "random",
} }

choices! { RegrowthSpeed {
    Never => "never",
    Veryslow => "veryslow",
    Slow => "slow",
    Default => "default",
    Fast => "fast",
    Veryfast => "veryfast",
} }

choices! { EventSetting {
    Default => "default",
    Enabled => "enabled",
} }

choices! { DamageSetting {
    Nonlethal => "nonlethal",
    Default => "default",
} }

choices! { Day {
    Default => "default",
    Longday => "longday",
    Longdusk => "longdusk",
    Longnight => "longnight",
    Noday => "noday",
    Nodusk => "nodusk",
    Nonight => "nonight",
    Onlyday => "onlyday",
    Onlydusk => "onlydusk",
    Onlynight => "onlynight",
} }

choices! { DropEverything {
    Default => "default",
    Always => "always",
} }

choices! { ExtraStartingItems {
    Count0 => "0",
    Count5 => "5",
    Default => "default",
    Count15 => "15",
    Count20 => "20",
    None => "none",
} }

choices! { LayoutMode {
    LinkNodesByKeys => "LinkNodesByKeys",
    RestrictNodesByKey => "RestrictNodesByKey",
} }

choices! { DamageTaken {
    Always => "always",
    None => "none",
    More => "more",
} }

choices! { AutoDetect {
    Never => "never",
    Default => "default",
    Always => "always",
} }

choices! { OceanWorldgenFrequency {
    OceanNever => "ocean_never",
    OceanRare => "ocean_rare",
    OceanUncommon => "ocean_uncommon",
    OceanDefault => "ocean_default",
    OceanOften => "ocean_often",
    OceanMostly => "ocean_mostly",
    OceanAlways => "ocean_always",
    OceanInsane => "ocean_insane",
} }

choices! { Petrification {
    None => "none",
    Few => "few",
    Default => "default",
    Many => "many",
    Max => "max",
} }

choices! { StartingSwaps {
    Classic => "classic",
    Default => "default",
    HighlyRandom => "highly random",
} }

choices! { ResetTime {
    None => "none",
    Slow => "slow",
    Default => "default",
    Fast => "fast",
    Always => "always",
} }

choices! { SeasonStart {
    Default => "default",
    Winter => "winter",
    Spring => "spring",
    Summer => "summer",
    AutumnSpring => "autumn|spring",
    WinterSummer => "winter|summer",
    AutumnWinterSpringSummer => "autumn|winter|spring|summer",
} }

choices! { SpawnMode {
    Fixed => "fixed",
    Scatter => "scatter",
} }

choices! { SpecialEvent {
    None => "none",
    Default => "default",
} }

choices! { ForestStartLocation {
    Default => "default",
    Plus => "plus",
    Darkness => "darkness",
} }

choices! { ForestTaskSet {
    Default => "default",
    Classic => "classic",
} }

choices! { WorldSize {
    Small => "small",
    Medium => "medium",
    Default => "default",
    Huge => "huge",
} }

choices! { LoadedForestIslands {
    Always => "always",
    Never => "never",
    Default => "default",
    Sometimes => "sometimes",
    Often => "often",
} }

choices! { AtriumSpeed {
    Veryslow => "veryslow",
    Slow => "slow",
    Default => "default",
    Fast => "fast",
    Veryfast => "veryfast",
} }

choices! { CaveStartLocation {
    Caves => "caves",
} }

choices! { CaveTaskSet {
    CaveDefault => "cave_default",
} }

choices! { LoadedCaveRoads {
    Never => "never",
} }

choices! { DiseaseDelay {
    None => "none",
    Random => "random",
    Long => "long",
    Default => "default",
    Short => "short",
} }

choices! { QuagmireOverridesStartLocation {
    QuagmireStartlocation => "quagmire_startlocation",
} }

choices! { QuagmireOverridesTaskSet {
    QuagmireTaskset => "quagmire_taskset",
} }

choices! { LavaArenaOverridesStartLocation {
    Lavaarena => "lavaarena",
} }

choices! { LavaArenaOverridesTaskSet {
    LavaarenaTaskset => "lavaarena_taskset",
} }

sparse_model! {
    #[derive(Default)]
    WorldOverrides {
        required {}
        optional {
        }
    }
}

world_model! { ForestOverrides, LoadedForestOverrides {
    #[garde(skip)]
    alternatehunt: Frequency = Frequency::Default,
    #[garde(skip)]
    angrybees: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    antliontribute: Frequency = Frequency::Default,
    #[garde(skip)]
    autumn: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    balatro: YesNo = YesNo::Default,
    #[garde(skip)]
    bananabush_portalrate: Frequency = Frequency::Default,
    #[garde(skip)]
    basicresource_regrowth: EnabledDisabled = EnabledDisabled::None,
    #[garde(skip)]
    bats_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    bearger: Frequency = Frequency::Default,
    #[garde(skip)]
    beefalo: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    beefaloheat: Frequency = Frequency::Default,
    #[garde(skip)]
    beequeen: Frequency = Frequency::Default,
    #[garde(skip)]
    bees: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    bees_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    berrybush: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    birds: Frequency = Frequency::Default,
    #[garde(skip)]
    boons: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    branching: Branching = Branching::Default,
    #[garde(skip)]
    brightmarecreatures: Frequency = Frequency::Default,
    #[garde(skip)]
    bunnymen_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    butterfly: Frequency = Frequency::Default,
    #[garde(skip)]
    buzzard: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    cactus: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    cactus_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    carrot: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    carrots_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    catcoon: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    catcoons: Frequency = Frequency::Default,
    #[garde(skip)]
    chess: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    cookiecutters: Frequency = Frequency::Default,
    #[garde(skip)]
    crabking: Frequency = Frequency::Default,
    #[garde(skip)]
    crow_carnival: EventSetting = EventSetting::Default,
    #[garde(skip)]
    darkness: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    day: Day = Day::Default,
    #[garde(skip)]
    daywalker2: Frequency = Frequency::Default,
    #[garde(skip)]
    deciduousmonster: Frequency = Frequency::Default,
    #[garde(skip)]
    deciduoustree_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    deerclops: Frequency = Frequency::Default,
    #[garde(skip)]
    dragonfly: Frequency = Frequency::Default,
    #[garde(skip)]
    dropeverythingondespawn: DropEverything = DropEverything::Default,
    #[garde(skip)]
    evergreen_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    extrastartingitems: ExtraStartingItems = ExtraStartingItems::Default,
    #[garde(skip)]
    eyeofterror: Frequency = Frequency::Default,
    #[garde(skip)]
    fishschools: Frequency = Frequency::Default,
    #[garde(skip)]
    flint: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    flowers: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    flowers_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    frograin: Frequency = Frequency::Default,
    #[garde(skip)]
    frogs: Frequency = Frequency::Default,
    #[garde(skip)]
    fruitfly: Frequency = Frequency::Default,
    #[garde(skip)]
    ghostenabled: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    ghostsanitydrain: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    gnarwail: Frequency = Frequency::Default,
    #[garde(skip)]
    goosemoose: Frequency = Frequency::Default,
    #[garde(skip)]
    grass: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    grassgekkos: Frequency = Frequency::Default,
    #[garde(skip)]
    hallowed_nights: EventSetting = EventSetting::Default,
    #[garde(skip)]
    has_ocean: bool = true,
    #[garde(skip)]
    healthpenalty: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    hound_mounds: Frequency = Frequency::Default,
    #[garde(skip)]
    houndmound: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    hounds: Frequency = Frequency::Default,
    #[garde(skip)]
    hunger: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    hunt: Frequency = Frequency::Default,
    #[garde(skip)]
    junkyard: YesNo = YesNo::Default,
    #[garde(skip)]
    keep_disconnected_tiles: bool = true,
    #[garde(skip)]
    klaus: Frequency = Frequency::Default,
    #[garde(skip)]
    krampus: Frequency = Frequency::Default,
    #[garde(skip)]
    layout_mode: LayoutMode = LayoutMode::LinkNodesByKeys,
    #[garde(skip)]
    lessdamagetaken: DamageTaken = DamageTaken::None,
    #[garde(skip)]
    liefs: Frequency = Frequency::Default,
    #[garde(skip)]
    lightcrab_portalrate: Frequency = Frequency::Default,
    #[garde(skip)]
    lightning: Frequency = Frequency::Default,
    #[garde(skip)]
    lightninggoat: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    r#loop: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    lunarhail_frequency: Frequency = Frequency::Default,
    #[garde(skip)]
    lureplants: Frequency = Frequency::Default,
    #[garde(skip)]
    malbatross: Frequency = Frequency::Default,
    #[garde(skip)]
    marshbush: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    merm: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    merms: Frequency = Frequency::Default,
    #[garde(skip)]
    meteorshowers: Frequency = Frequency::Default,
    #[garde(skip)]
    meteorspawner: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moles: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moles_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    monkeytail_portalrate: Frequency = Frequency::Default,
    #[garde(skip)]
    moon_berrybush: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_bullkelp: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_carrot: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_fissure: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_fruitdragon: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_hotspring: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_rock: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_sapling: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_spider: Frequency = Frequency::Default,
    #[garde(skip)]
    moon_spiders: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_starfish: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_tree: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    moon_tree_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    mosquitos: Frequency = Frequency::Default,
    #[garde(skip)]
    mushroom: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    mutated_bearger: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_bird_gestalt: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_birds: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_buzzard_gestalt: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_deerclops: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_hounds: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_merm: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_spiderqueen: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_warg: YesNo = YesNo::Default,
    #[garde(skip)]
    no_joining_islands: bool = true,
    #[garde(skip)]
    no_wormholes_to_disconnected_tiles: bool = true,
    #[garde(skip)]
    ocean_bullkelp: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    ocean_otterdens: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    ocean_seastack: OceanWorldgenFrequency = OceanWorldgenFrequency::OceanDefault,
    #[garde(skip)]
    ocean_shoal: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    ocean_waterplant: OceanWorldgenFrequency = OceanWorldgenFrequency::OceanDefault,
    #[garde(skip)]
    ocean_wobsterden: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    otters_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    palmcone_seed_portalrate: Frequency = Frequency::Default,
    #[garde(skip)]
    palmconetree: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    palmconetree_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    penguins: Frequency = Frequency::Default,
    #[garde(skip)]
    penguins_moon: YesNo = YesNo::Default,
    #[garde(skip)]
    perd: Frequency = Frequency::Default,
    #[garde(skip)]
    petrification: Petrification = Petrification::Default,
    #[garde(skip)]
    pigs: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    pigs_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    pirateraids: Frequency = Frequency::Default,
    #[garde(skip)]
    ponds: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    portal_spawnrate: Frequency = Frequency::Default,
    #[garde(skip)]
    portalresurection: EnabledDisabled = EnabledDisabled::None,
    #[garde(skip)]
    powder_monkey_portalrate: Frequency = Frequency::Default,
    #[garde(skip)]
    prefabswaps_start: StartingSwaps = StartingSwaps::Default,
    #[garde(skip)]
    rabbits: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    rabbits_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    reeds: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    reeds_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    resettime: ResetTime = ResetTime::Default,
    #[garde(skip)]
    rifts_enabled: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    rifts_frequency: Frequency = Frequency::Default,
    #[garde(skip)]
    roads: YesNo = YesNo::Default,
    #[garde(skip)]
    rock: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    rock_ice: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    saltstack_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    sapling: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    season_start: SeasonStart = SeasonStart::Default,
    #[garde(skip)]
    seasonalstartingitems: YesNo = YesNo::Default,
    #[garde(skip)]
    shadowcreatures: Frequency = Frequency::Default,
    #[garde(skip)]
    sharkboi: Frequency = Frequency::Default,
    #[garde(skip)]
    sharks: Frequency = Frequency::Default,
    #[garde(skip)]
    spawnmode: SpawnMode = SpawnMode::Fixed,
    #[garde(skip)]
    spawnprotection: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    specialevent: SpecialEvent = SpecialEvent::Default,
    #[garde(skip)]
    spider_warriors: YesNo = YesNo::Default,
    #[garde(skip)]
    spiderqueen: Frequency = Frequency::Default,
    #[garde(skip)]
    spiders: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    spiders_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    spring: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    squid: Frequency = Frequency::Default,
    #[garde(skip)]
    stageplays: YesNo = YesNo::Default,
    #[garde(skip)]
    start_location: ForestStartLocation = ForestStartLocation::Default,
    #[garde(skip)]
    summer: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    summerhounds: YesNo = YesNo::Default,
    #[garde(skip)]
    tallbirds: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    task_set: ForestTaskSet = ForestTaskSet::Default,
    #[garde(skip)]
    temperaturedamage: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    tentacles: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    terrariumchest: YesNo = YesNo::Default,
    #[garde(skip)]
    touchstone: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    trees: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    tumbleweed: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    twiggytrees_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    walrus: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    walrus_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    wanderingtrader_enabled: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    wasps: Frequency = Frequency::Default,
    #[garde(skip)]
    weather: Frequency = Frequency::Default,
    #[garde(skip)]
    wildfires: Frequency = Frequency::Default,
    #[garde(skip)]
    winter: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    winterhounds: YesNo = YesNo::Default,
    #[garde(skip)]
    winters_feast: EventSetting = EventSetting::Default,
    #[garde(skip)]
    wobsters: Frequency = Frequency::Default,
    #[garde(skip)]
    world_size: WorldSize = WorldSize::Default,
    #[garde(length(chars, min = 1))]
    wormhole_prefab: String = "wormhole".into(),
    #[garde(skip)]
    year_of_the_beefalo: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_bunnyman: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_carrat: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_catcoon: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_dragonfly: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_gobbler: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_knight: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_pig: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_snake: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_varg: EventSetting = EventSetting::Default,
} loaded {
    #[garde(skip)]
    islands: Option<LoadedForestIslands> = None,
} }

world_model! { CaveOverrides, LoadedCaveOverrides {
    #[garde(skip)]
    acidrain_enabled: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    atriumgate: AtriumSpeed = AtriumSpeed::Default,
    #[garde(skip)]
    autumn: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    banana: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    basicresource_regrowth: EnabledDisabled = EnabledDisabled::None,
    #[garde(skip)]
    bat_boss: Frequency = Frequency::Default,
    #[garde(skip)]
    bats: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    bats_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    beefaloheat: Frequency = Frequency::Default,
    #[garde(skip)]
    berrybush: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    boons: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    branching: Branching = Branching::Default,
    #[garde(skip)]
    brightmarecreatures: Frequency = Frequency::Default,
    #[garde(skip)]
    bunnymen: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    bunnymen_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    cave_ponds: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    cave_spiders: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    cavelight: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    chess: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    chest_mimics: Frequency = Frequency::Default,
    #[garde(skip)]
    crow_carnival: EventSetting = EventSetting::Default,
    #[garde(skip)]
    darkness: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    day: Day = Day::Default,
    #[garde(skip)]
    daywalker: Frequency = Frequency::Default,
    #[garde(skip)]
    dropeverythingondespawn: DropEverything = DropEverything::Default,
    #[garde(skip)]
    dustmoths: Frequency = Frequency::Default,
    #[garde(skip)]
    earthquakes: Frequency = Frequency::Default,
    #[garde(skip)]
    evergreen_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    extrastartingitems: ExtraStartingItems = ExtraStartingItems::Default,
    #[garde(skip)]
    fern: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    fissure: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    flint: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    flower_cave: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    flower_cave_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    fruitfly: Frequency = Frequency::Default,
    #[garde(skip)]
    ghostenabled: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    ghostsanitydrain: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    grass: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    grassgekkos: Frequency = Frequency::Default,
    #[garde(skip)]
    hallowed_nights: EventSetting = EventSetting::Default,
    #[garde(skip)]
    has_ocean: Option<bool> = None,
    #[garde(skip)]
    healthpenalty: EnabledDisabled = EnabledDisabled::Always,
    #[garde(skip)]
    hunger: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    itemmimics: Frequency = Frequency::Default,
    #[garde(skip)]
    keep_disconnected_tiles: Option<bool> = None,
    #[garde(skip)]
    krampus: Frequency = Frequency::Default,
    #[garde(skip)]
    layout_mode: LayoutMode = LayoutMode::RestrictNodesByKey,
    #[garde(skip)]
    lessdamagetaken: DamageTaken = DamageTaken::None,
    #[garde(skip)]
    lichen: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    liefs: Frequency = Frequency::Default,
    #[garde(skip)]
    lightflier_flower_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    lightfliers: Frequency = Frequency::Default,
    #[garde(skip)]
    r#loop: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    marshbush: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    merms: Frequency = Frequency::Default,
    #[garde(skip)]
    molebats: Frequency = Frequency::Default,
    #[garde(skip)]
    moles_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    monkey: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    monkey_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    moon_spider: Frequency = Frequency::Default,
    #[garde(skip)]
    mushgnome: Frequency = Frequency::Default,
    #[garde(skip)]
    mushroom: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    mushtree: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    mushtree_moon_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    mushtree_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    mutated_birds: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_merm: YesNo = YesNo::Default,
    #[garde(skip)]
    mutated_spiderqueen: YesNo = YesNo::Default,
    #[garde(skip)]
    nightmarecreatures: Frequency = Frequency::Default,
    #[garde(skip)]
    no_joining_islands: Option<bool> = None,
    #[garde(skip)]
    no_wormholes_to_disconnected_tiles: Option<bool> = None,
    #[garde(skip)]
    pigs_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    portalresurection: EnabledDisabled = EnabledDisabled::None,
    #[garde(skip)]
    prefabswaps_start: StartingSwaps = StartingSwaps::Default,
    #[garde(skip)]
    reeds: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    reeds_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    resettime: ResetTime = ResetTime::Default,
    #[garde(skip)]
    rifts_enabled_cave: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    rifts_frequency_cave: Frequency = Frequency::Default,
    #[garde(skip)]
    rock: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    rocky: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    rocky_boss: Frequency = Frequency::Default,
    #[garde(skip)]
    rocky_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    sapling: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    season_start: SeasonStart = SeasonStart::Default,
    #[garde(skip)]
    seasonalstartingitems: YesNo = YesNo::Default,
    #[garde(skip)]
    shadowcreatures: Frequency = Frequency::Default,
    #[garde(skip)]
    slurper: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    slurtles: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    slurtles_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    snurtles: Frequency = Frequency::Default,
    #[garde(skip)]
    spawnmode: SpawnMode = SpawnMode::Fixed,
    #[garde(skip)]
    spawnprotection: AutoDetect = AutoDetect::Default,
    #[garde(skip)]
    specialevent: SpecialEvent = SpecialEvent::Default,
    #[garde(skip)]
    spider_dropper: Frequency = Frequency::Default,
    #[garde(skip)]
    spider_hider: Frequency = Frequency::Default,
    #[garde(skip)]
    spider_spitter: Frequency = Frequency::Default,
    #[garde(skip)]
    spider_warriors: YesNo = YesNo::Default,
    #[garde(skip)]
    spiderqueen: Frequency = Frequency::Default,
    #[garde(skip)]
    spiders: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    spiders_setting: Frequency = Frequency::Default,
    #[garde(skip)]
    spring: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    start_location: CaveStartLocation = CaveStartLocation::Caves,
    #[garde(skip)]
    summer: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    task_set: CaveTaskSet = CaveTaskSet::CaveDefault,
    #[garde(skip)]
    temperaturedamage: DamageSetting = DamageSetting::Default,
    #[garde(skip)]
    tentacles: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    toadstool: Frequency = Frequency::Default,
    #[garde(skip)]
    touchstone: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    tree_rock: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    tree_rock_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    trees: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    twiggytrees_regrowth: RegrowthSpeed = RegrowthSpeed::Default,
    #[garde(skip)]
    weather: Frequency = Frequency::Default,
    #[garde(skip)]
    winter: SeasonLength = SeasonLength::Default,
    #[garde(skip)]
    winters_feast: EventSetting = EventSetting::Default,
    #[garde(skip)]
    world_size: WorldSize = WorldSize::Default,
    #[garde(skip)]
    wormattacks: Frequency = Frequency::Default,
    #[garde(skip)]
    wormattacks_boss: Frequency = Frequency::Default,
    #[garde(length(chars, min = 1))]
    wormhole_prefab: String = "tentacle_pillar".into(),
    #[garde(skip)]
    wormlights: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    worms: WorldgenFrequency = WorldgenFrequency::Default,
    #[garde(skip)]
    year_of_the_beefalo: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_bunnyman: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_carrat: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_catcoon: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_dragonfly: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_gobbler: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_knight: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_pig: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_snake: EventSetting = EventSetting::Default,
    #[garde(skip)]
    year_of_the_varg: EventSetting = EventSetting::Default,
} loaded {
    #[garde(skip)]
    roads: Option<LoadedCaveRoads> = None,
} }

sparse_model! {
    #[derive(Default)]
    QuagmireOverrides {
        required {}
        optional {
            #[garde(skip)]
            autumn: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            basicresource_regrowth: EnabledDisabled = EnabledDisabled::None,
            #[garde(skip)]
            beefaloheat: Frequency = Frequency::Default,
            #[garde(skip)]
            boons: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            branching: Branching = Branching::Random,
            #[garde(skip)]
            brightmarecreatures: Frequency = Frequency::Default,
            #[garde(skip)]
            crow_carnival: EventSetting = EventSetting::Default,
            #[garde(skip)]
            darkness: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            day: Day = Day::Default,
            #[garde(skip)]
            disease_delay: DiseaseDelay = DiseaseDelay::None,
            #[garde(skip)]
            dropeverythingondespawn: DropEverything = DropEverything::Default,
            #[garde(skip)]
            extrastartingitems: ExtraStartingItems = ExtraStartingItems::Default,
            #[garde(skip)]
            ghostenabled: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            ghostsanitydrain: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            hallowed_nights: EventSetting = EventSetting::Default,
            #[garde(skip)]
            healthpenalty: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            hunger: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            keep_disconnected_tiles: bool = false,
            #[garde(skip)]
            krampus: Frequency = Frequency::Default,
            #[garde(skip)]
            layout_mode: LayoutMode = LayoutMode::RestrictNodesByKey,
            #[garde(skip)]
            lessdamagetaken: DamageTaken = DamageTaken::None,
            #[garde(dive)]
            loop_percent: UnitInterval = UnitInterval(Number::from(0)),
            #[garde(skip)]
            no_joining_islands: bool = true,
            #[garde(skip)]
            no_wormholes_to_disconnected_tiles: bool = true,
            #[garde(skip)]
            petrification: Petrification = Petrification::None,
            #[garde(skip)]
            poi: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            portalresurection: EnabledDisabled = EnabledDisabled::None,
            #[garde(skip)]
            prefabswaps_start: StartingSwaps = StartingSwaps::Classic,
            #[garde(skip)]
            protected: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            resettime: ResetTime = ResetTime::Default,
            #[garde(skip)]
            roads: YesNo = YesNo::Never,
            #[garde(skip)]
            season_start: SeasonStart = SeasonStart::Default,
            #[garde(skip)]
            seasonalstartingitems: YesNo = YesNo::Default,
            #[garde(skip)]
            shadowcreatures: Frequency = Frequency::Default,
            #[garde(skip)]
            spawnmode: SpawnMode = SpawnMode::Fixed,
            #[garde(skip)]
            spawnprotection: AutoDetect = AutoDetect::Default,
            #[garde(skip)]
            specialevent: SpecialEvent = SpecialEvent::Default,
            #[garde(skip)]
            spring: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            start_location: QuagmireOverridesStartLocation = QuagmireOverridesStartLocation::QuagmireStartlocation,
            #[garde(skip)]
            summer: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            task_set: QuagmireOverridesTaskSet = QuagmireOverridesTaskSet::QuagmireTaskset,
            #[garde(skip)]
            temperaturedamage: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            touchstone: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            traps: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            wildfires: Frequency = Frequency::Never,
            #[garde(skip)]
            winter: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            winters_feast: EventSetting = EventSetting::Default,
            #[garde(skip)]
            world_size: WorldSize = WorldSize::Small,
            #[garde(skip)]
            year_of_the_beefalo: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_bunnyman: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_carrat: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_catcoon: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_dragonfly: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_gobbler: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_knight: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_pig: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_snake: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_varg: EventSetting = EventSetting::Default,
        }
    }
}

sparse_model! {
    #[derive(Default)]
    LavaArenaOverrides {
        required {}
        optional {
            #[garde(skip)]
            autumn: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            basicresource_regrowth: EnabledDisabled = EnabledDisabled::None,
            #[garde(skip)]
            beefaloheat: Frequency = Frequency::Default,
            #[garde(skip)]
            boons: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            brightmarecreatures: Frequency = Frequency::Default,
            #[garde(skip)]
            crow_carnival: EventSetting = EventSetting::Default,
            #[garde(skip)]
            darkness: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            day: Day = Day::Default,
            #[garde(skip)]
            dropeverythingondespawn: DropEverything = DropEverything::Default,
            #[garde(skip)]
            extrastartingitems: ExtraStartingItems = ExtraStartingItems::Default,
            #[garde(skip)]
            ghostenabled: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            ghostsanitydrain: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            hallowed_nights: EventSetting = EventSetting::Default,
            #[garde(skip)]
            healthpenalty: EnabledDisabled = EnabledDisabled::Always,
            #[garde(skip)]
            hunger: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            keep_disconnected_tiles: bool = true,
            #[garde(skip)]
            krampus: Frequency = Frequency::Default,
            #[garde(skip)]
            layout_mode: LayoutMode = LayoutMode::RestrictNodesByKey,
            #[garde(skip)]
            lessdamagetaken: DamageTaken = DamageTaken::None,
            #[garde(skip)]
            no_joining_islands: bool = true,
            #[garde(skip)]
            no_wormholes_to_disconnected_tiles: bool = true,
            #[garde(skip)]
            poi: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            portalresurection: EnabledDisabled = EnabledDisabled::None,
            #[garde(skip)]
            protected: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            resettime: ResetTime = ResetTime::Default,
            #[garde(skip)]
            roads: YesNo = YesNo::Never,
            #[garde(skip)]
            season_start: SeasonStart = SeasonStart::Default,
            #[garde(skip)]
            seasonalstartingitems: YesNo = YesNo::Default,
            #[garde(skip)]
            shadowcreatures: Frequency = Frequency::Default,
            #[garde(skip)]
            spawnmode: SpawnMode = SpawnMode::Fixed,
            #[garde(skip)]
            spawnprotection: AutoDetect = AutoDetect::Default,
            #[garde(skip)]
            specialevent: SpecialEvent = SpecialEvent::Default,
            #[garde(skip)]
            spring: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            start_location: LavaArenaOverridesStartLocation = LavaArenaOverridesStartLocation::Lavaarena,
            #[garde(skip)]
            summer: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            task_set: LavaArenaOverridesTaskSet = LavaArenaOverridesTaskSet::LavaarenaTaskset,
            #[garde(skip)]
            temperaturedamage: DamageSetting = DamageSetting::Default,
            #[garde(skip)]
            touchstone: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            traps: WorldgenFrequency = WorldgenFrequency::Never,
            #[garde(skip)]
            winter: SeasonLength = SeasonLength::Default,
            #[garde(skip)]
            winters_feast: EventSetting = EventSetting::Default,
            #[garde(skip)]
            world_size: WorldSize = WorldSize::Small,
            #[garde(skip)]
            year_of_the_beefalo: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_bunnyman: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_carrat: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_catcoon: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_dragonfly: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_gobbler: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_knight: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_pig: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_snake: EventSetting = EventSetting::Default,
            #[garde(skip)]
            year_of_the_varg: EventSetting = EventSetting::Default,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct CustomWorldOverrides(#[garde(dive)] pub BTreeMap<String, LuaScalar>);

scalar_resolve!(CustomWorldOverrides);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(tag = "kind", content = "values", deny_unknown_fields)]
pub enum WorldValues {
    #[serde(rename = "world")]
    World(#[garde(dive)] Box<WorldOverrides>),
    #[serde(rename = "forest")]
    Forest(#[garde(dive)] Box<ForestOverrides>),
    #[serde(rename = "cave")]
    Cave(#[garde(dive)] Box<CaveOverrides>),
    #[serde(rename = "quagmire")]
    Quagmire(#[garde(dive)] Box<QuagmireOverrides>),
    #[serde(rename = "lavaarena")]
    LavaArena(#[garde(dive)] Box<LavaArenaOverrides>),
    #[serde(rename = "custom")]
    Custom(#[garde(dive)] Box<CustomWorldOverrides>),
    #[serde(rename = "loaded_forest")]
    LoadedForest(#[garde(dive)] Box<LoadedForestOverrides>),
    #[serde(rename = "loaded_cave")]
    LoadedCave(#[garde(dive)] Box<LoadedCaveOverrides>),
}

impl Default for WorldValues {
    fn default() -> Self {
        Self::World(Box::default())
    }
}

impl WorldValues {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::World(_) => "world",
            Self::Forest(_) => "forest",
            Self::Cave(_) => "cave",
            Self::Quagmire(_) => "quagmire",
            Self::LavaArena(_) => "lavaarena",
            Self::Custom(_) => "custom",
            Self::LoadedForest(_) => "loaded_forest",
            Self::LoadedCave(_) => "loaded_cave",
        }
    }

    pub fn values(&self) -> Value {
        serde_json::to_value(self).expect("serializable world overrides")["values"].take()
    }
}

impl Resolve for WorldValues {
    fn resolved(&self) -> Value {
        let (kind, values) = match self {
            Self::World(values) => ("world", values.resolved()),
            Self::Forest(values) => ("forest", values.resolved()),
            Self::Cave(values) => ("cave", values.resolved()),
            Self::Quagmire(values) => ("quagmire", values.resolved()),
            Self::LavaArena(values) => ("lavaarena", values.resolved()),
            Self::Custom(values) => ("custom", values.resolved()),
            Self::LoadedForest(values) => ("loaded_forest", values.resolved()),
            Self::LoadedCave(values) => ("loaded_cave", values.resolved()),
        };
        json!({"kind": kind, "values": values})
    }
}
