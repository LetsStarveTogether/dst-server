import re
from dataclasses import dataclass

from pydantic import JsonValue

from dst_server.events import server

NATIVE_TIMESTAMP = re.compile(rb"^\[[0-9]+:[0-9]{2}:[0-9]{2}\]: ?")
_NETWORK_ERROR = re.compile(r"^CURL ERROR: \(([^)]{1,253})\) \[([0-9]{1,3})\]")
_AUTHENTICATION_DENIED = re.compile(
    r"^\[Warning\] Authentication denied for '[^']{1,128}':\s*\(([0-9]{1,3})\)\s*$"
)
_AUTHENTICATION_STATE = re.compile(
    r"^\[(Warning|Error)\] cSteamPunchthrough::onAuthentication(Approved|Denied)"
    r" - (Client already authenticated|Missing client object)$"
)
_WORLDGEN_ERROR = re.compile(
    r"^An error occured during world (gen we will retry|and we give up)! "
    r"\[was\s+([0-9]{1,3})\s+of\s+([0-9]{1,3})\s*\]$"
)
_MOD_LOAD_ERROR = re.compile(
    r"^(?:Error loading mod: |Mod: )workshop-([0-9]{1,20})\b.*"
    r"(?:!|Error loading mod!)\s*$"
)
_SERVICES = {
    "dst.metrics.klei.com": "metrics",
    "lobby-v2.klei.com": "lobby",
    "login.kleientertainment.com": "login",
    "items.kleientertainment.com": "items",
}
_PAUSE_STATES = {
    "Server Paused": "paused",
    "Server Autopaused": "autopaused",
    "Server Unpaused": "running",
}
_SETPIECE_FAILURES = {
    (
        "ERROR: wagpunk_arena_manager expected to be able to calculate the set "
        "piece angle using hermitcrab_marker and beebox_hermit "
        "but found neither of these."
    ): "wagpunk_arena_manager",
    (
        "ERROR: hermitcrab_relocation_manager expected to be able to calculate "
        "the set piece angle using monkeyqueen and monkeyportal "
        "but found neither of these."
    ): "hermitcrab_relocation_manager",
}


@dataclass(frozen=True, slots=True)
class OperationalRecord:
    uid: str
    event_name: str
    body: dict[str, JsonValue]
    observed_timestamp_ns: int
    severity_text: str


def lifecycle_body(event: server.Event) -> dict[str, JsonValue] | None:
    if isinstance(event, server.UnknownEvent):
        return None
    if isinstance(event, server.SessionEvent):
        return {"session_id": event.session_id}
    if isinstance(event, server.SavedEvent):
        return {"snapshot": event.snapshot}
    return {}


def classify_log(message: str) -> tuple[str, dict[str, JsonValue], str] | None:
    message = message.rstrip()
    if state := _PAUSE_STATES.get(message):
        return "dst.server.pause_changed", {"state": state}, "INFO"

    severity = "ERROR"
    body: dict[str, JsonValue]
    if match := _NETWORK_ERROR.match(message):
        body = {
            "kind": "network_error",
            "service": _SERVICES.get(match[1], "other"),
            "code": int(match[2]),
        }
        severity = "WARN"
    elif match := _AUTHENTICATION_DENIED.fullmatch(message):
        body = {"kind": "authentication_denied", "code": int(match[1])}
        severity = "WARN"
    elif match := _AUTHENTICATION_STATE.fullmatch(message):
        body = {
            "kind": "authentication_state_error",
            "operation": match[2].lower(),
            "reason": "missing_client"
            if match[3] == "Missing client object"
            else "already_authenticated",
        }
        severity = "WARN" if match[1] == "Warning" else "ERROR"
    elif match := _WORLDGEN_ERROR.fullmatch(message):
        retry = match[1] == "gen we will retry"
        body = {
            "kind": "worldgen_retry" if retry else "worldgen_failed",
            "attempt": int(match[2]),
            "max_attempts": int(match[3]),
        }
        severity = "WARN" if retry else "ERROR"
    elif prefab := _SETPIECE_FAILURES.get(message):
        body = {"kind": "setpiece_angle_unavailable", "prefab": prefab}
    elif message == "DownloadServerMods timed out with no response from Workshop...":
        body = {"kind": "workshop_timeout"}
    elif message == "LUA ERROR stack traceback:":
        body = {"kind": "lua_error"}
    elif match := _MOD_LOAD_ERROR.fullmatch(message):
        body = {"kind": "mod_load_error", "mod_id": match[1]}
    else:
        return None
    return "dst.runtime.diagnostic", body, severity
