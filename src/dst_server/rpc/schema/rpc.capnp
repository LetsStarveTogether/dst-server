@0xe46ba453ff72c8b1;

struct Outcome(T) {
  union {
    value @0 :T;
    error @1 :Data;
  }
}

struct Unit {}

struct Batch {
  union {
    items @0 :List(Data);
    closed @1 :Void;
    error @2 :Data;
  }
}

interface Subscription {
  next @0 (maxItems :UInt16) -> (batch :Batch);
  close @1 () -> (result :Outcome(Unit));
}

interface Endpoint {
  call @0 (request :Data) -> (result :Outcome(Data));
  subscribe @1 (kind :Text) -> (result :Outcome(Subscription));
}

interface Cluster extends(Endpoint) {
  shard @0 (shardName :Text) -> (result :Outcome(Endpoint));
}

interface Agent extends(Endpoint) {}

interface Bootstrap {
  connect @0 (schemaFingerprint :Text) -> (result :Outcome(Cluster));
}

interface WorkerRegistry {
  register @0 (schemaFingerprint :Text, agent :Agent) -> (result :Outcome(Unit));
  failed @1 () -> (result :Outcome(Unit));
}
