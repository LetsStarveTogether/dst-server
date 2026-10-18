@0xc835c081a4d6e52b;

# Dynamic game and MOD data stays structured on the wire.
struct Value {
  union {
    null @0 :Void;
    boolean @1 :Bool;
    signed @2 :Int64;
    unsigned @3 :UInt64;
    float @4 :Float64;
    text @5 :Text;
    list @6 :List(Value);
    object @7 :List(Entry);
    decimal @8 :Text; # Exact numbers outside the native integer/float representations.
  }
  struct Entry {
    key @0 :Text;
    value @1 :Value;
  }
}

struct Target {
  union {
    room @0 :Void;
    shard @1 :Text;
  }
}

# Absent pointers represent None; zero, false and empty text remain distinct.
struct BoolValue { value @0 :Bool; }
struct UInt64Value { value @0 :UInt64; }
struct Float64Value { value @0 :Float64; }

struct Countdown {
  template @0 :Text;
  delay @1 :Float64 = 60;
  interval @2 :Float64 = 30;
  parameters @3 :List(Value.Entry);
}

struct Envelope {
  target @0 :Target;
  request @1 :Request;
  timeout @2 :Float64Value;
}

struct Request {
  union {
    status @0 :Void;
    start @1 :Void;
    stop @2 :NoticeArguments;
    restart @3 :NoticeArguments;
    kill @4 :Void;
    updateMods @5 :UpdateModsArguments;
    readConfiguration @6 :Void;
    configure @7 :ConfigureArguments;
    setPolicy @8 :SetPolicyArguments;
    execute @9 :SourceArguments;
    executeJson @10 :SourceArguments;
    evaluate @11 :SourceArguments;
    executeAll @12 :SourceArguments;
    announce @13 :AnnounceArguments;
    save @14 :Void;
    pause @15 :PauseArguments;
    reset @16 :Void;
    rollback @17 :RollbackArguments;
    regenerate @18 :RegenerateArguments;
    regenerateShard @19 :RegenerateShardArguments;
    snapshots @20 :SnapshotsArguments;
    rollbackToDay @21 :RollbackToDayArguments;
    rollbackToSnapshot @22 :RollbackToSnapshotArguments;
    health @23 :Void;
    presence @24 :Void;
    room @25 :Void;
    world @26 :Void;
    runtime @27 :Void;
    mods @28 :Void;
    connectedShards @29 :Void;
    listPlayers @30 :Void;
    getPlayer @31 :PlayerArguments;
    inventory @32 :PlayerArguments;
    kick @33 :PlayerArguments;
    ban @34 :BanArguments;
    blocklist @35 :Void;
    isBlocked @36 :PlayerArguments;
    unban @37 :PlayerArguments;
    isAdmin @38 :PlayerArguments;
    readPermissions @39 :Void;
    setAdmin @40 :SetAdminArguments;
    setVitals @41 :SetVitalsArguments;
    killPlayer @42 :PlayerArguments;
    revive @43 :PlayerArguments;
    despawn @44 :PlayerArguments;
    migrate @45 :MigrateArguments;
    teleport @46 :TeleportArguments;
    give @47 :ItemArguments;
    remove @48 :ItemArguments;
    isWhitelisted @49 :PlayerArguments;
    whitelist @50 :PlayerArguments;
    unwhitelist @51 :PlayerArguments;
  }

  struct NoticeArguments {
    notice @0 :Countdown;
  }
  struct UpdateModsArguments {
    restart @0 :Bool;
    notice @1 :Countdown;
  }
  struct ConfigureArguments {
    configuration @0 :Value;
    replacePermissions @1 :Bool;
  }
  struct SetPolicyArguments {
    policy @0 :Value;
  }
  struct SourceArguments {
    source @0 :Text;
  }
  struct AnnounceArguments {
    message @0 :Text;
    count @1 :UInt64 = 1;
    interval @2 :Float64 = 30;
  }
  struct PauseArguments {
    paused @0 :Bool;
  }
  struct RollbackArguments {
    count @0 :UInt64 = 1;
  }
  struct RegenerateArguments {
    expectedSessionId @0 :Text;
    requireEmpty @1 :BoolValue;
  }
  struct RegenerateShardArguments {
    preserveSettings @0 :Bool = true;
  }
  struct SnapshotsArguments {
    limit @0 :UInt64 = 100;
    before @1 :UInt64Value;
  }
  struct RollbackToDayArguments {
    day @0 :UInt64;
  }
  struct RollbackToSnapshotArguments {
    sessionId @0 :Text;
    snapshotId @1 :UInt64;
  }
  struct PlayerArguments {
    userid @0 :Text;
  }
  struct BanArguments {
    userid @0 :Text;
    seconds @1 :UInt64Value;
  }
  struct SetAdminArguments {
    userid @0 :Text;
    remove @1 :Bool;
  }
  struct SetVitalsArguments {
    userid @0 :Text;
    health @1 :Float64Value;
    hunger @2 :Float64Value;
    sanity @3 :Float64Value;
    temperature @4 :Float64Value;
    moisture @5 :Float64Value;
  }
  struct MigrateArguments {
    userid @0 :Text;
    shardId @1 :Text;
    portalId @2 :UInt64 = 1;
  }
  struct TeleportArguments {
    userid @0 :Text;
    x @1 :Float64;
    y @2 :Float64;
    z @3 :Float64;
  }
  struct ItemArguments {
    userid @0 :Text;
    item @1 :Text;
    count @2 :UInt64 = 1;
  }
}

enum ErrorCode {
  invalid @0;
  notReady @1;
  busy @2;
  unsupported @3;
  notFound @4;
  conflict @5;
  partialFailure @6;
  unknown @7;
  transport @8;
  timeout @9;
  overflow @10;
  staleReference @11;
  lua @12;
  protocol @13;
  internal @14;
}

struct Error {
  code @0 :ErrorCode;
  message @1 :Text;
  details @2 :Value;
}

struct Outcome {
  union {
    value @0 :Value;
    error @1 :Error;
  }
}

struct Batch {
  records @0 :List(Value);
  dropped @1 :UInt64;
  closed @2 :Bool;
}

enum StreamKind {
  logs @0;
  lifecycle @1;
  events @2;
}

interface Subscription {
  next @0 (maxItems :UInt16) -> (batch :Batch);
  close @1 ();
}

interface Room {
  call @0 (request :Envelope) -> (result :Outcome);
  describe @1 () -> (result :Outcome);
  subscribe @2 (kind :StreamKind) -> (subscription :Subscription);
}
