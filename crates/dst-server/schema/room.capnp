@0xa45c4cb9a94aebbe;

struct Outcome {
  union {
    value @0 :Data;
    error @1 :Data;
  }
}

struct Batch {
  records @0 :List(Data);
  dropped @1 :UInt64;
  closed @2 :Bool;
}

interface Subscription {
  next @0 (maxItems :UInt16) -> (batch :Batch);
  close @1 ();
}

interface Room {
  call @0 (request :Data) -> (result :Outcome);
  describe @1 () -> (result :Outcome);
  subscribe @2 (kind :Text) -> (subscription :Subscription);
}
