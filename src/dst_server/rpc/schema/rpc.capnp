@0xe46ba453ff72c8b1;

enum ErrorCode {
  invalidArgument @0;
  invalidState @1;
  notFound @2;
  conflict @3;
  unavailable @4;
  timeout @5;
  overflow @6;
  topologyChangeRequired @7;
  incompatibleSchema @8;
  internal @9;
  indeterminate @10;
}

struct RpcError {
  code @0 :ErrorCode;
  errorId @1 :Text;
  message @2 :Text;
  fields @3 :List(Text);
}

struct Outcome(T) {
  union {
    value @0 :T;
    error @1 :RpcError;
  }
}

struct Nullable(T) {
  union {
    none @0 :Void;
    value @1 :T;
  }
}

struct Unit {}

struct BoolValue {
  value @0 :Bool;
}

struct UInt64Value {
  value @0 :UInt64;
}

struct TextValue {
  value @0 :Text;
}

struct Float64Value {
  value @0 :Float64;
}

struct ConfigurationSnapshot {
  revision @0 :Text;
  configuration @1 :Data;
}

struct FieldPath {
  components @0 :List(Text);
}

struct InvalidConfiguration {
  revision @0 :Text;
  fields @1 :List(FieldPath);
}

struct ConfigurationRead {
  union {
    valid @0 :ConfigurationSnapshot;
    invalid @1 :InvalidConfiguration;
  }
}

struct ShardResult(T) {
  shard @0 :Text;
  result @1 :Outcome(T);
}

struct ShardSaved {
  shard @0 :Text;
  event @1 :Data;
}

struct ClusterSaveResult {
  snapshot @0 :Nullable(UInt64Value);
  shards @1 :List(ShardSaved);
}

struct Batch(T) {
  union {
    items @0 :T;
    closed @1 :Void;
    error @2 :RpcError;
  }
}

interface Subscription(T) {
  next @0 (maxItems :UInt16) -> (batch :Batch(T));
  close @1 () -> (result :Outcome(Unit));
}

interface DataSubscription extends(Subscription(List(Data))) {}

interface Bootstrap {
  connect @0 (schemaFingerprint :Text) -> (result :Outcome(Cluster));
}

interface Cluster {
  status @0 () -> (result :Outcome(Data));
  start @1 () -> (result :Outcome(Unit));
  stop @2 () -> (result :Outcome(Unit));
  restart @3 () -> (result :Outcome(Unit));
  kill @4 () -> (result :Outcome(Unit));
  updateMods @5 () -> (result :Outcome(Unit));
  readConfiguration @6 () -> (result :Outcome(ConfigurationRead));
  saveConfiguration @7 (
    expectedRevision :Text,
    configuration :Data
  ) -> (result :Outcome(ConfigurationSnapshot));
  executeAll @8 (
    source :Text,
    timeout :Float64
  ) -> (result :Outcome(List(ShardResult(TextValue))));
  announce @9 (message :Text) -> (result :Outcome(Unit));
  save @10 (timeout :Float64) -> (result :Outcome(ClusterSaveResult));
  pause @11 (paused :Bool) -> (result :Outcome(List(ShardResult(BoolValue))));
  reset @12 (timeout :Float64) -> (result :Outcome(Unit));
  rollback @13 (count :UInt64, timeout :Float64) -> (result :Outcome(Unit));
  regenerate @14 (timeout :Float64) -> (result :Outcome(Unit));
  listPlayers @15 () -> (result :Outcome(List(Data)));
  getPlayer @16 (userid :Text) -> (result :Outcome(Nullable(Data)));
  isWhitelisted @17 (userid :Text) -> (result :Outcome(BoolValue));
  whitelist @18 (userid :Text) -> (result :Outcome(BoolValue));
  unwhitelist @19 (userid :Text) -> (result :Outcome(BoolValue));
  subscribeLogs @20 () -> (result :Outcome(DataSubscription));
  subscribeLifecycle @21 () -> (result :Outcome(DataSubscription));
  subscribeEvents @22 () -> (result :Outcome(DataSubscription));
  shard @23 (shardName :Text) -> (result :Outcome(Shard));
}

interface Shard {
  status @0 () -> (result :Outcome(Data));
  start @1 () -> (result :Outcome(Unit));
  stop @2 () -> (result :Outcome(Unit));
  restart @3 () -> (result :Outcome(Unit));
  kill @4 () -> (result :Outcome(Unit));
  execute @5 (source :Text, timeout :Float64) -> (result :Outcome(TextValue));
  executeJson @6 (source :Text) -> (result :Outcome(Data));
  health @7 () -> (result :Outcome(Data));
  room @8 () -> (result :Outcome(Data));
  world @9 () -> (result :Outcome(Data));
  runtime @10 () -> (result :Outcome(Data));
  mods @11 () -> (result :Outcome(List(Data)));
  connectedShards @12 () -> (result :Outcome(List(Data)));
  pause @13 (paused :Bool) -> (result :Outcome(BoolValue));
  regenerateShard @14 (
    preserveSettings :Bool,
    timeout :Float64
  ) -> (result :Outcome(Unit));
  listPlayers @15 () -> (result :Outcome(List(Data)));
  getPlayer @16 (userid :Text) -> (result :Outcome(Nullable(Data)));
  inventory @17 (userid :Text) -> (result :Outcome(Nullable(Data)));
  kick @18 (userid :Text) -> (result :Outcome(Unit));
  ban @19 (userid :Text, seconds :Nullable(UInt64Value)) -> (result :Outcome(Unit));
  blocklist @20 () -> (result :Outcome(List(Text)));
  isBlocked @21 (userid :Text) -> (result :Outcome(BoolValue));
  unban @22 (userid :Text) -> (result :Outcome(BoolValue));
  isAdmin @23 (userid :Text) -> (result :Outcome(Nullable(BoolValue)));
  setVitals @24 (
    userid :Text,
    health :Nullable(Float64Value),
    hunger :Nullable(Float64Value),
    sanity :Nullable(Float64Value),
    temperature :Nullable(Float64Value),
    moisture :Nullable(Float64Value)
  ) -> (result :Outcome(BoolValue));
  killPlayer @25 (userid :Text) -> (result :Outcome(BoolValue));
  revive @26 (userid :Text) -> (result :Outcome(BoolValue));
  despawn @27 (userid :Text) -> (result :Outcome(BoolValue));
  migrate @28 (
    userid :Text,
    shardId :Text,
    portalId :UInt64
  ) -> (result :Outcome(BoolValue));
  teleport @29 (
    userid :Text,
    x :Float64,
    y :Float64,
    z :Float64
  ) -> (result :Outcome(BoolValue));
  give @30 (userid :Text, item :Text, count :UInt64) -> (result :Outcome(UInt64Value));
  remove @31 (userid :Text, item :Text, count :UInt64) -> (result :Outcome(UInt64Value));
  subscribeLogs @32 () -> (result :Outcome(DataSubscription));
  subscribeLifecycle @33 () -> (result :Outcome(DataSubscription));
  subscribeEvents @34 () -> (result :Outcome(DataSubscription));
  save @35 (timeout :Float64) -> (result :Outcome(Data));
}

interface Agent extends(Shard) {
  saveMarker @0 () -> (result :Outcome(UInt64Value));
  waitSaved @1 (
    afterSequence :UInt64,
    snapshot :Nullable(UInt64Value),
    timeout :Float64
  ) -> (result :Outcome(Data));
  generationMarker @2 () -> (result :Outcome(UInt64Value));
  waitGeneration @3 (
    afterGeneration :UInt64,
    timeout :Float64
  ) -> (result :Outcome(UInt64Value));
  isWhitelisted @4 (userid :Text) -> (result :Outcome(BoolValue));
  whitelist @5 (userid :Text) -> (result :Outcome(BoolValue));
  unwhitelist @6 (userid :Text) -> (result :Outcome(BoolValue));
  activate @7 () -> (result :Outcome(Unit));
  announce @8 (message :Text) -> (result :Outcome(Unit));
  reset @9 (timeout :Float64) -> (result :Outcome(Unit));
  rollback @10 (count :UInt64, timeout :Float64) -> (result :Outcome(Unit));
  regenerate @11 (timeout :Float64) -> (result :Outcome(Unit));
}

interface WorkerRegistry {
  register @0 (
    schemaFingerprint :Text,
    agent :Agent
  ) -> (result :Outcome(Unit));
  failed @1 () -> (result :Outcome(Unit));
}
