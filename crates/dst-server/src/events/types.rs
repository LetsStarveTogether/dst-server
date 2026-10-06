//! Game event payloads shared by native validation and subscriptions.

use garde::Validate;
use serde::{Deserialize, Serialize};

use crate::validation::FiniteNumber;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct GameEvent {
    #[garde(pattern("^[0-7][0-9A-HJKMNP-TV-Z]{25}$"))]
    pub nonce: String,
    #[garde(dive)]
    pub generation: Counter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub session_id: Option<Identifier>,
    #[garde(dive)]
    pub seq: PositiveCounter,
    #[garde(dive)]
    pub tick: Counter,
    #[garde(dive)]
    pub monotonic_ms: Counter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub cycle: Option<Counter>,
    #[serde(flatten)]
    #[garde(dive)]
    pub body: EventBody,
}

macro_rules! game_events {
    ($($variant:ident($data:ty) => $name:literal),+ $(,)?) => {
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
        #[serde(tag = "event", content = "data", deny_unknown_fields)]
        pub enum EventBody {
            $(#[serde(rename = $name)] $variant(#[garde(dive)] $data)),+
        }

        pub const EVENT_NAMES: &[&str] = &[$($name),+];

        impl EventBody {
            pub fn name(&self) -> &'static str {
                match self { $(Self::$variant(_) => $name),+ }
            }
            pub(crate) fn into_data(self) -> serde_json::Value {
                match self { $(Self::$variant(data) => serde_json::to_value(data)),+ }
                    .expect("serializable event body")
            }
        }
    };
}

game_events! {
    ClientAuthenticated(ClientData) => "dst.client.authenticated",
    ClientDisconnected(ClientData) => "dst.client.disconnected",
    EntityDeath(EntityDeathData) => "dst.entity.death",
    PlayerActionPerformed(ActionPerformedData) => "dst.player.action_performed",
    PlayerAte(AteData) => "dst.player.ate",
    PlayerCombatContact(CombatContactData) => "dst.player.combat_contact",
    PlayerConditionChanged(ConditionData) => "dst.player.condition_changed",
    PlayerCrafted(CraftedData) => "dst.player.crafted",
    PlayerCreatureMurdered(CreatureMurderedData) => "dst.player.creature_murdered",
    PlayerDiceRolled(DiceRolledData) => "dst.player.dice_rolled",
    PlayerDishHarvested(DishHarvestedData) => "dst.player.dish_harvested",
    PlayerFinishedWork(FinishedWorkData) => "dst.player.finished_work",
    PlayerFished(FishedData) => "dst.player.fished",
    PlayerGhosted(GhostedData) => "dst.player.ghosted",
    PlayerHarvested(HarvestedData) => "dst.player.harvested",
    PlayerIncident(IncidentData) => "dst.player.incident",
    PlayerItemDeployed(ItemDeployedData) => "dst.player.item_deployed",
    PlayerLoaded(PlayerLoadedData) => "dst.player.loaded",
    PlayerMigrationStarted(MigrationStartedData) => "dst.player.migration_started",
    PlayerPicked(PickedData) => "dst.player.picked",
    PlayerPlanted(PlantedData) => "dst.player.planted",
    PlayerPublicChat(PublicChatData) => "dst.player.public_chat",
    PlayerRecipeUnlocked(RecipeUnlockedData) => "dst.player.recipe_unlocked",
    PlayerRevivalBound(RevivalBoundData) => "dst.player.revival_bound",
    PlayerRevived(RevivedData) => "dst.player.revived",
    PlayerShardLeft(PlayerData) => "dst.player.shard_left",
    PlayerTraded(TradedData) => "dst.player.traded",
    ServerPauseChanged(PauseData) => "dst.server.pause_changed",
    ServerPresence(PresenceData) => "dst.server.presence",
    VoteResult(VoteResultData) => "dst.vote.result",
    VoteStarted(VoteStartedData) => "dst.vote.started",
    WorldDaywalkerDefeated(DaywalkerDefeatedData) => "dst.world.daywalker_defeated",
    WorldRiftChanged(RiftChangedData) => "dst.world.rift_changed",
    WorldRiftUnlocked(RiftUnlockedData) => "dst.world.rift_unlocked",
    WorldStateChanged(StateData) => "dst.world.state_changed",
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct Identifier(#[garde(length(chars, min = 1, max = 128))] pub String);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct Counter(#[garde(range(max = 9_007_199_254_740_991))] pub u64);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct PositiveCounter(#[garde(range(min = 1, max = 9_007_199_254_740_991))] pub u64);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct Position {
    #[garde(dive)]
    pub x: FiniteNumber,
    #[garde(dive)]
    pub y: FiniteNumber,
    #[garde(dive)]
    pub z: FiniteNumber,
}

fn valid_age_seconds(value: &FiniteNumber, _: &()) -> garde::Result {
    if value
        .0
        .as_f64()
        .is_some_and(|age| age.is_finite() && age >= 0.0)
    {
        Ok(())
    } else {
        Err(garde::Error::new("must be a non-negative finite age"))
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EntityRef {
    #[garde(dive)]
    pub guid: PositiveCounter,
    #[garde(dive)]
    pub prefab: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub userid: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ClientData {
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlayerData {
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EntityDeathData {
    #[garde(dive)]
    pub victim: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub afflicter: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub attributed_userid: Option<Identifier>,
    #[garde(skip)]
    pub is_epic: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(length(chars, max = 256))]
    pub cause: Option<String>,
    #[garde(skip)]
    pub corpsing: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(inner(custom(valid_age_seconds)))]
    pub age_seconds: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub position: Option<Position>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ActionPerformedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub action_id: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub item: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct AteData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub food: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub feeder: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CombatContactData {
    #[garde(dive)]
    pub source: EntityRef,
    #[garde(dive)]
    pub target: EntityRef,
    #[garde(skip)]
    pub kind: CombatContactKind,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub weapon: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CombatContactKind {
    Pvp,
    Epic,
    Ordinary,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PublicChatData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub userid: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub prefab: Option<Identifier>,
    // The producer uses the running game's chat limit, which mods can change.
    #[garde(skip)]
    pub message: String,
    #[garde(skip)]
    pub emote: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(tag = "condition", deny_unknown_fields)]
pub enum ConditionData {
    #[serde(rename = "fire_damage")]
    FireDamage {
        #[garde(skip)]
        active: bool,
        #[garde(dive)]
        player: EntityRef,
    },
    #[serde(rename = "freezing")]
    Freezing {
        #[garde(skip)]
        active: bool,
        #[garde(dive)]
        player: EntityRef,
    },
    #[serde(rename = "lunar_burn")]
    LunarBurn {
        #[garde(skip)]
        active: bool,
        #[garde(dive)]
        player: EntityRef,
    },
    #[serde(rename = "overheating")]
    Overheating {
        #[garde(skip)]
        active: bool,
        #[garde(dive)]
        player: EntityRef,
    },
    #[serde(rename = "sanity")]
    Sanity {
        #[garde(dive)]
        player: EntityRef,
        #[garde(skip)]
        state: ConditionState,
    },
    #[serde(rename = "starving")]
    Starving {
        #[garde(skip)]
        active: bool,
        #[garde(dive)]
        player: EntityRef,
    },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConditionState {
    Sane,
    Insane,
    Enlightened,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CraftedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub recipe: Identifier,
    #[garde(skip)]
    pub kind: CraftedKind,
    #[garde(dive)]
    pub item: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CraftedKind {
    Item,
    Structure,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CreatureMurderedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub victim: Identifier,
    #[garde(dive)]
    pub count: PositiveCounter,
    #[garde(skip)]
    pub negligent: bool,
    #[garde(skip)]
    pub incinerated: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DiceRolledData {
    #[garde(dive)]
    pub max: PositiveCounter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub prefab: Option<Identifier>,
    #[garde(dive)]
    pub rolls: Vec<PositiveCounter>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub userid: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DishHarvestedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub product: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub chef_userid: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct FinishedWorkData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub action_id: Option<Identifier>,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub target: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct FishedData {
    #[garde(dive)]
    pub fish: Identifier,
    #[garde(skip)]
    pub method: FishedMethod,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FishedMethod {
    Inland,
    Ocean,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct GhostedData {
    #[garde(skip)]
    pub corpse: bool,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct HarvestedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub source: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct IncidentData {
    #[garde(skip)]
    pub kind: IncidentKind,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IncidentKind {
    WashedAshore,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ItemDeployedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub item: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub position: Option<Position>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlayerLoadedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(skip)]
    pub is_ghost: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(inner(custom(valid_age_seconds)))]
    pub age_seconds: Option<FiniteNumber>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct MigrationStartedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub destination_shard_id: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PickedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub source: Option<Identifier>,
    #[garde(dive)]
    pub count: PositiveCounter,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlantedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub position: Option<Position>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RecipeUnlockedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub recipe: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RevivalBoundData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub source: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RevivedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(skip)]
    pub method: RevivedMethod,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub reviver: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(inner(custom(valid_age_seconds)))]
    pub age_seconds: Option<FiniteNumber>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RevivedMethod {
    Ghost,
    Corpse,
    Charlie,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct TradedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub giver: EntityRef,
    #[garde(dive)]
    pub item: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(tag = "domain", deny_unknown_fields)]
pub enum PauseData {
    #[serde(rename = "server")]
    Server {
        #[garde(skip)]
        autopause: bool,
        #[garde(skip)]
        gameautopause: bool,
        #[garde(skip)]
        pause: bool,
        #[serde(deserialize_with = "Option::deserialize")]
        #[garde(skip)]
        source: Option<String>,
    },
    #[serde(rename = "simulation")]
    Simulation {
        #[garde(skip)]
        paused: bool,
    },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PresenceData {
    #[garde(dive)]
    pub clients: Vec<Identifier>,
    #[garde(dive)]
    pub players: Vec<PresencePlayer>,
    #[garde(dive)]
    pub world: PresenceWorld,
    #[garde(skip)]
    pub reason: PresenceReason,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PresencePlayer {
    #[garde(dive)]
    pub userid: Identifier,
    #[garde(dive)]
    pub prefab: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub loaded: Option<bool>,
    #[garde(skip)]
    pub is_ghost: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PresenceWorld {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub season: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub phase: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub moonphase: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub nightmarephase: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub israining: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub issnowing: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub isacidraining: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub islunarhailing: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub lunar_rifts_enabled: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub shadow_rifts_enabled: Option<bool>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PresenceReason {
    Startup,
    Periodic,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteStartedData {
    #[garde(dive)]
    pub command: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub starter_userid: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteResultData {
    #[garde(dive)]
    pub command: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub starter_userid: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
    #[garde(skip)]
    pub passed: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DaywalkerDefeatedData {
    #[garde(dive)]
    pub shard_id: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RiftChangedData {
    #[garde(skip)]
    pub active: bool,
    #[garde(dive)]
    pub rift: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RiftUnlockedData {
    #[garde(skip)]
    pub kind: RiftUnlockedKind,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RiftUnlockedKind {
    Lunar,
    Shadow,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(tag = "name", deny_unknown_fields)]
pub enum StateData {
    #[serde(rename = "cavemoonphase")]
    Cavemoonphase {
        #[garde(dive)]
        value: Identifier,
    },
    #[serde(rename = "cavephase")]
    Cavephase {
        #[garde(dive)]
        value: Identifier,
    },
    #[serde(rename = "cycles")]
    Cycles {
        #[garde(dive)]
        value: Counter,
    },
    #[serde(rename = "isacidraining")]
    Isacidraining {
        #[garde(skip)]
        value: bool,
    },
    #[serde(rename = "islunarhailing")]
    Islunarhailing {
        #[garde(skip)]
        value: bool,
    },
    #[serde(rename = "israining")]
    Israining {
        #[garde(skip)]
        value: bool,
    },
    #[serde(rename = "issnowing")]
    Issnowing {
        #[garde(skip)]
        value: bool,
    },
    #[serde(rename = "moonphase")]
    Moonphase {
        #[garde(dive)]
        value: Identifier,
    },
    #[serde(rename = "nightmarephase")]
    Nightmarephase {
        #[garde(dive)]
        value: Identifier,
    },
    #[serde(rename = "phase")]
    Phase {
        #[garde(dive)]
        value: Identifier,
    },
    #[serde(rename = "season")]
    Season {
        #[garde(dive)]
        value: Identifier,
    },
}
