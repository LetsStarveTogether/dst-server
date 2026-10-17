//! Game event payloads shared by native validation and subscriptions.

use std::collections::BTreeMap;

use garde::Validate;
use serde::{Deserialize, Serialize};

use crate::validation::FiniteNumber;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct Envelope<T: Validate<Context = ()>> {
    #[garde(range(equal = 3))]
    pub v: u64,
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
    #[garde(dive)]
    pub data: T,
}

macro_rules! game_events {
    ($($variant:ident($data:ty) => $name:literal),+ $(,)?) => {
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
        #[serde(tag = "event", deny_unknown_fields)]
        pub enum GameEvent {
            $(#[serde(rename = $name)] $variant(#[garde(dive)] Envelope<$data>)),+
        }

        pub const EVENT_NAMES: &[&str] = &[$($name),+];

        impl GameEvent {
            pub fn name(&self) -> &'static str {
                match self { $(Self::$variant(_) => $name),+ }
            }
            pub fn nonce(&self) -> &str {
                match self { $(Self::$variant(event) => &event.nonce),+ }
            }
            pub fn generation(&self) -> u64 {
                match self { $(Self::$variant(event) => event.generation.0),+ }
            }
            pub fn sequence(&self) -> u64 {
                match self { $(Self::$variant(event) => event.seq.0),+ }
            }
        }
    };
}

game_events! {
    ClientAuthenticated(ClientData) => "dst.client.authenticated",
    ClientDisconnected(ClientData) => "dst.client.disconnected",
    EntityDeath(EntityDeathData) => "dst.entity.death",
    ModOutdated(ModOutdatedData) => "dst.mod.outdated",
    PlayerAction(ActionData) => "dst.player.action",
    PlayerAppearanceRequested(AppearanceRequestedData) => "dst.player.appearance_requested",
    PlayerAte(AteData) => "dst.player.ate",
    PlayerChat(ChatData) => "dst.player.chat",
    PlayerCombatBlocked(CombatBlockedData) => "dst.player.combat_blocked",
    PlayerCombatHit(CombatHitData) => "dst.player.combat_hit",
    PlayerCombatReceived(CombatReceivedData) => "dst.player.combat_received",
    PlayerConditionChanged(ConditionData) => "dst.player.condition_changed",
    PlayerContainerTransfer(ContainerTransferData) => "dst.player.container_transfer",
    PlayerCrafted(CraftedData) => "dst.player.crafted",
    PlayerDeployed(DeployedData) => "dst.player.deployed",
    PlayerDiceRolled(DiceRolledData) => "dst.player.dice_rolled",
    PlayerDisconnected(DisconnectedData) => "dst.player.disconnected",
    PlayerDropped(DroppedData) => "dst.player.dropped",
    PlayerEmoteRequested(EmoteRequestedData) => "dst.player.emote_requested",
    PlayerEquipped(EquippedData) => "dst.player.equipped",
    PlayerFinishedWork(FinishedWorkData) => "dst.player.finished_work",
    PlayerFished(FishedData) => "dst.player.fished",
    PlayerGhosted(GhostedData) => "dst.player.ghosted",
    PlayerHarvested(HarvestedData) => "dst.player.harvested",
    PlayerHoundWarning(HoundWarningData) => "dst.player.hound_warning",
    PlayerIncident(IncidentData) => "dst.player.incident",
    PlayerLoaded(PlayerData) => "dst.player.loaded",
    PlayerMigrationStarted(MigrationStartedData) => "dst.player.migration_started",
    PlayerPicked(PickedData) => "dst.player.picked",
    PlayerPlanted(PlantedData) => "dst.player.planted",
    PlayerRescueRequested(PlayerRequestData) => "dst.player.rescue_requested",
    PlayerRevived(RevivedData) => "dst.player.revived",
    PlayerShardEntered(PlayerData) => "dst.player.shard_entered",
    PlayerShardLeft(PlayerData) => "dst.player.shard_left",
    PlayerSkillChanged(SkillChangedData) => "dst.player.skill_changed",
    PlayerSkinReceived(SkinReceivedData) => "dst.player.skin_received",
    PlayerSpawned(PlayerData) => "dst.player.spawned",
    PlayerTeleportStarted(TeleportStartedData) => "dst.player.teleport_started",
    PlayerUnequipped(UnequippedData) => "dst.player.unequipped",
    ServerAnnouncement(AnnouncementData) => "dst.server.announcement",
    ServerPauseChanged(PauseData) => "dst.server.pause_changed",
    ServerPresence(PresenceData) => "dst.server.presence",
    ServerSystemMessage(SystemMessageData) => "dst.server.system_message",
    ShardConnectionChanged(ShardConnectionData) => "dst.shard.connection_changed",
    TelemetryError(DriverDiagnostic) => "dst.telemetry.error",
    VoteClosed(VoteClosedData) => "dst.vote.closed",
    VoteResult(VoteResultData) => "dst.vote.result",
    VoteSubmitted(VoteSubmittedData) => "dst.vote.submitted",
    VoteUpdated(VoteUpdatedData) => "dst.vote.updated",
    WorldMapDeliveryStarted(MapDeliveryStartedData) => "dst.world.map_delivery_started",
    WorldRiftChanged(RiftChangedData) => "dst.world.rift_changed",
    WorldRiftUnlocked(RiftUnlockedData) => "dst.world.rift_unlocked",
    WorldShardBossDefeated(ShardBossDefeatedData) => "dst.world.shard_boss_defeated",
    WorldStateChanged(StateData) => "dst.world.state_changed",
    WorldVaultTrialGuardsDefeated(VaultTrialGuardsDefeatedData) => "dst.world.vault_trial_guards_defeated",
    WorldVaultTrialProgress(VaultTrialProgressData) => "dst.world.vault_trial_progress",
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ClientData {
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct Identifier(#[garde(length(chars, min = 1, max = 128))] pub String);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EntityDeathData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub afflicter: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub attributed_player: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(length(chars, max = 256))]
    pub cause: Option<String>,
    #[garde(skip)]
    pub corpsing: bool,
    #[garde(dive)]
    pub victim: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EntityRef {
    #[garde(dive)]
    pub guid: PositiveCounter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub position: Option<Position>,
    #[garde(dive)]
    pub prefab: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub userid: Option<Identifier>,
}

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

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ModOutdatedData {
    #[garde(length(chars, min = 1, max = 4096))]
    pub name: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ActionData {
    #[garde(dive)]
    pub action_id: Identifier,
    #[garde(dive)]
    pub actor: EntityRef,
    #[garde(skip)]
    pub forced: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub initial_target_owner: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub inventory_object: Option<ItemRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub position: Option<Position>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub reason: Option<EventText>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub recipe: Option<Identifier>,
    #[garde(skip)]
    pub success: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ItemRef {
    #[garde(dive)]
    pub guid: PositiveCounter,
    #[garde(dive)]
    pub prefab: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub skin: Option<Identifier>,
    #[garde(dive)]
    pub stack_size: PositiveCounter,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct EventText(#[garde(length(chars, max = 256))] pub String);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct AppearanceRequestedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub player: Option<EntityRef>,
    #[garde(dive)]
    pub requested: AppearanceData,
    #[garde(dive)]
    pub userid: Identifier,
    #[garde(dive)]
    pub validated: AppearanceData,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct AppearanceData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub clothing_body: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub clothing_feet: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub clothing_hand: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub clothing_legs: Option<Identifier>,
    #[garde(dive)]
    pub prefab: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub skin_base: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct AteData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub feeder: Option<EntityRef>,
    #[garde(dive)]
    pub food: ItemRef,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ChatData {
    #[garde(skip)]
    pub emote: bool,
    #[garde(skip)]
    pub message: String,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub player: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub prefab: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub userid: Option<Identifier>,
    #[garde(skip)]
    pub whisper: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CombatBlockedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub attacker: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub damage: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub from_doattack: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub original_damage: Option<FiniteNumber>,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub special_damage: Vec<SpecialDamage>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub stimuli: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub weapon: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct SpecialDamage {
    #[garde(dive)]
    pub kind: Identifier,
    #[garde(dive)]
    pub value: FiniteNumber,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CombatHitData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub damage: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub damage_resolved: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub from_doattack: Option<bool>,
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub redirected: Option<EntityRef>,
    #[garde(dive)]
    pub special_damage: Vec<SpecialDamage>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub stimuli: Option<Identifier>,
    #[garde(dive)]
    pub target: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub weapon: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CombatReceivedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub attacker: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub damage: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub damage_resolved: Option<FiniteNumber>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub from_doattack: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub original_damage: Option<FiniteNumber>,
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub redirected: Option<EntityRef>,
    #[garde(dive)]
    pub special_damage: Vec<SpecialDamage>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub stimuli: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub weapon: Option<EntityRef>,
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
pub enum ConditionState {
    #[serde(rename = "sane")]
    Sane,
    #[serde(rename = "insane")]
    Insane,
    #[serde(rename = "enlightened")]
    Enlightened,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ContainerTransferData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub after: Option<ItemRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub before: Option<ItemRef>,
    #[garde(dive)]
    pub holder: EntityRef,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub slot: PositiveCounter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct CraftedData {
    #[garde(dive)]
    pub item: ItemRef,
    #[garde(skip)]
    pub kind: CraftedKind,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub recipe: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub skin: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum CraftedKind {
    #[serde(rename = "item")]
    Item,
    #[serde(rename = "structure")]
    Structure,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DeployedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub prefab: Identifier,
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
pub struct DisconnectedData {
    #[garde(skip)]
    pub expected: bool,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DroppedData {
    #[garde(dive)]
    pub item: ItemRef,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EmoteRequestedData {
    #[garde(dive)]
    pub emote: Identifier,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub player: Option<EntityRef>,
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct EquippedData {
    #[garde(dive)]
    pub item: ItemRef,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub slot: Identifier,
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
    pub fish: ItemRef,
    #[garde(skip)]
    pub method: FishedMethod,
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum FishedMethod {
    #[serde(rename = "inland")]
    Inland,
    #[serde(rename = "ocean")]
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
pub struct HoundWarningData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(range(max = 8))]
    pub warning_type: u64,
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
pub enum IncidentKind {
    #[serde(rename = "sink")]
    Sink,
    #[serde(rename = "fall_in_void")]
    FallInVoid,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlayerData {
    #[garde(dive)]
    pub player: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct MigrationStartedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub destination: Option<Position>,
    #[garde(dive)]
    pub destination_shard_id: Identifier,
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub portal_id: Option<IntegerOrIdentifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct Counter(#[garde(range(max = 9_007_199_254_740_991))] pub u64);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(untagged)]
pub enum IntegerOrIdentifier {
    Integer(#[garde(dive)] Counter),
    Identifier(#[garde(dive)] Identifier),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PickedData {
    #[garde(dive)]
    pub loot: Vec<ItemRef>,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub source: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlantedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub position: Position,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PlayerRequestData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub player: Option<EntityRef>,
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct RevivedData {
    #[garde(skip)]
    pub corpse: bool,
    #[garde(skip)]
    pub method: RevivedMethod,
    #[garde(dive)]
    pub player: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub reviver: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum RevivedMethod {
    #[serde(rename = "ghost")]
    Ghost,
    #[serde(rename = "corpse")]
    Corpse,
    #[serde(rename = "charlie")]
    Charlie,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct SkillChangedData {
    #[garde(skip)]
    pub active: bool,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub skill: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct SkinReceivedData {
    #[garde(skip)]
    pub name: String,
    #[garde(skip)]
    pub skin: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct TeleportStartedData {
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(dive)]
    pub source: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target: Option<EntityRef>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct UnequippedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub item: Option<ItemRef>,
    #[garde(dive)]
    pub player: EntityRef,
    #[garde(skip)]
    pub slip: bool,
    #[garde(dive)]
    pub slot: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct AnnouncementData {
    #[garde(skip)]
    pub kind: String,
    #[garde(skip)]
    pub message: String,
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
    pub health: DriverHealth,
    #[garde(dive)]
    pub max_players: Counter,
    #[garde(dive)]
    pub players: Vec<PresencePlayer>,
    #[garde(skip)]
    pub reason: PresenceReason,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DriverHealth {
    #[serde(
        default,
        deserialize_with = "crate::validation::present",
        skip_serializing_if = "Option::is_none"
    )]
    #[garde(skip)]
    pub capabilities: Option<BTreeMap<String, DriverHealthCapabilitiesValue>>,
    #[garde(dive)]
    pub errors: Counter,
    #[garde(dive)]
    pub events_emitted: Counter,
    #[serde(
        default,
        deserialize_with = "crate::validation::present",
        skip_serializing_if = "Option::is_none"
    )]
    #[garde(skip)]
    pub faults: Option<BTreeMap<String, String>>,
    #[garde(dive)]
    pub generation: Counter,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub last_error: Option<DriverDiagnostic>,
    #[garde(range(equal = 3))]
    pub protocol: u64,
    #[serde(
        default,
        deserialize_with = "crate::validation::present",
        skip_serializing_if = "Option::is_none"
    )]
    #[garde(dive)]
    pub revision: Option<Counter>,
    #[garde(skip)]
    pub telemetry_status: DriverHealthTelemetryStatus,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum DriverHealthCapabilitiesValue {
    #[serde(rename = "disabled")]
    Disabled,
    #[serde(rename = "unsupported")]
    Unsupported,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "failed")]
    Failed,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct DriverDiagnostic {
    #[garde(dive)]
    pub count: PositiveCounter,
    #[garde(skip)]
    pub message: DriverDiagnosticMessage,
    #[serde(
        default,
        deserialize_with = "crate::validation::present",
        skip_serializing_if = "Option::is_none"
    )]
    #[garde(dive)]
    pub revision: Option<Counter>,
    #[garde(pattern("^[a-z][a-z0-9_.:]{0,127}$"))]
    pub stage: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum DriverDiagnosticMessage {
    #[serde(rename = "callback_failed")]
    CallbackFailed,
    #[serde(rename = "encoding_failed")]
    EncodingFailed,
    #[serde(rename = "event_too_large")]
    EventTooLarge,
    #[serde(rename = "installation_failed")]
    InstallationFailed,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum DriverHealthTelemetryStatus {
    #[serde(rename = "disabled")]
    Disabled,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "degraded")]
    Degraded,
    #[serde(rename = "failed")]
    Failed,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct PresencePlayer {
    #[garde(dive)]
    pub guid: PositiveCounter,
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum PresenceReason {
    #[serde(rename = "startup")]
    Startup,
    #[serde(rename = "periodic")]
    Periodic,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct SystemMessageData {
    #[garde(skip)]
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ShardConnectionData {
    #[garde(dive)]
    pub name: EventText,
    #[garde(skip)]
    pub ready: bool,
    #[garde(dive)]
    pub shard_id: Identifier,
    #[garde(dive)]
    pub tags: Vec<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteClosedData {
    #[garde(skip)]
    pub command: String,
    #[garde(skip)]
    pub source: VoteClosedSource,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub starter_userid: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum VoteClosedSource {
    #[serde(rename = "worldvoter")]
    Worldvoter,
    #[serde(rename = "gorge_voter")]
    GorgeVoter,
    #[serde(rename = "lobbyvote")]
    Lobbyvote,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteResultData {
    #[garde(skip)]
    pub command: String,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub count: Option<Counter>,
    #[garde(dive)]
    pub options: Vec<Counter>,
    #[garde(skip)]
    pub passed: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub selection: Option<IntegerOrIdentifier>,
    #[garde(skip)]
    pub source: VoteClosedSource,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
    #[garde(dive)]
    pub total: Counter,
    #[garde(dive)]
    pub total_not_voted: Counter,
    #[garde(dive)]
    pub total_voted: Counter,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteSubmittedData {
    #[garde(skip)]
    pub command: String,
    #[garde(dive)]
    pub selection: IntegerOrIdentifier,
    #[garde(skip)]
    pub source: VoteClosedSource,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteUpdatedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(skip)]
    pub command: Option<String>,
    #[garde(dive)]
    pub command_hash: Counter,
    #[garde(dive)]
    pub countdown: Counter,
    #[garde(skip)]
    pub source: VoteClosedSource,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub starter_userid: Option<Identifier>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub target_userid: Option<Identifier>,
    #[garde(dive)]
    pub voters: Vec<VoteSelection>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VoteSelection {
    #[garde(dive)]
    pub selection: Counter,
    #[garde(dive)]
    pub userid: Identifier,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct MapDeliveryStartedData {
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub actor: Option<EntityRef>,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub destination: Option<Position>,
    #[garde(dive)]
    pub item: EntityRef,
    #[serde(deserialize_with = "Option::deserialize")]
    #[garde(dive)]
    pub origin: Option<Position>,
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
pub enum RiftUnlockedKind {
    #[serde(rename = "lunar")]
    Lunar,
    #[serde(rename = "shadow")]
    Shadow,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct ShardBossDefeatedData {
    #[garde(dive)]
    pub prefab: Identifier,
    #[garde(dive)]
    pub shard_id: Identifier,
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

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VaultTrialGuardsDefeatedData {
    #[garde(custom(crate::validation::is_true))]
    pub bonus_loot: bool,
    #[garde(dive)]
    pub last_guard: EntityRef,
    #[garde(dive)]
    pub trial: EntityRef,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
pub struct VaultTrialProgressData {
    #[garde(range(max = 4))]
    pub sockets: u64,
    #[garde(range(max = 4))]
    pub sparks: u64,
    #[garde(dive)]
    pub trial: EntityRef,
    #[garde(skip)]
    pub trigger: VaultTrialProgressTrigger,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum VaultTrialProgressTrigger {
    #[serde(rename = "socket")]
    Socket,
    #[serde(rename = "activator")]
    Activator,
    #[serde(rename = "loaded")]
    Loaded,
}
