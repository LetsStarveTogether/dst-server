import secrets
from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self
from warnings import warn

from pydantic import (
    AfterValidator,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from dst_server.models.base import RevalidatedFrozenModel

from .ini import IniSection, render_ini
from .overrides import (
    FrozenMapping,
    LevelDataOverride,
    ModOverrides,
    ModSettings,
    WorkshopDownloads,
    WorldgenOverride,
)
from .world import WorldOverrides

PERMISSION_FILES = ("adminlist.txt", "whitelist.txt", "blocklist.txt")
CLUSTER_STRUCTURE_FIELDS = (
    "master_port",
    "bind_ip",
    "master_ip",
    "shard_enabled",
    "cluster_key",
)

type Port = Annotated[int, Field(ge=1024, le=65535)]
type IniText = Annotated[str, Field(pattern=r"^[^\x00\r\n]*$")]
type NonEmptyIniText = Annotated[
    str,
    Field(min_length=1, pattern=r"^[^\x00\r\n]*$"),
]
type IPv4 = IPv4Address
type Host = NonEmptyIniText
type ClusterLanguage = Literal[
    "en",
    "fr",
    "es",
    "mex",
    "tr",
    "de",
    "it",
    "pt",
    "pl",
    "ru",
    "ko",
    "zh",
    "zht",
    "zhr",
]

MAX_PASSWORD_LENGTH = 254
MASTER_SHARD_ID = 1
MIN_SECONDARY_SHARD_ID = 2
MAX_PATH_COMPONENT_BYTES = 255


def _validate_ini_secret(value: SecretStr) -> SecretStr:
    secret = value.get_secret_value()
    if any(character in secret for character in "\0\r\n"):
        msg = "DST INI secrets cannot contain NUL, CR, or LF"
        raise ValueError(msg)
    try:
        secret.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "DST INI secrets must contain valid UTF-8"
        raise ValueError(msg) from error
    return value


type IniSecret = Annotated[SecretStr, AfterValidator(_validate_ini_secret)]
type IniPassword = Annotated[
    IniSecret,
    Field(max_length=MAX_PASSWORD_LENGTH),
]


def _validate_cluster_token(value: SecretStr) -> SecretStr:
    token = value.get_secret_value()
    if any(not "!" <= character <= "~" for character in token):
        msg = "cluster tokens can contain only printable non-space ASCII"
        raise ValueError(msg)
    return value


def _validate_permission_list(value: str) -> str:
    if "\0" in value or "\r" in value:
        msg = "DST permission files cannot contain NUL or CR"
        raise ValueError(msg)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "DST permission files must contain valid UTF-8"
        raise ValueError(msg) from error
    return value


type ClusterToken = Annotated[SecretStr, AfterValidator(_validate_cluster_token)]
type PermissionList = Annotated[str, AfterValidator(_validate_permission_list)]


def _validate_shard_name(name: str) -> str:
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = f"unsafe DST shard directory name: {name!r}"
        raise ValueError(msg) from error
    if (
        not name.strip()
        or len(encoded) > MAX_PATH_COMPONENT_BYTES
        or name.startswith(".")
        or name.casefold()
        in {
            "console",
            "mods",
            "cluster.ini",
            "cluster_token.txt",
            *map(str.casefold, PERMISSION_FILES),
        }
        or any(character in name for character in "\0/\\\r\n")
    ):
        msg = f"unsafe DST shard directory name: {name!r}"
        raise ValueError(msg)
    return name


type ShardName = Annotated[str, AfterValidator(_validate_shard_name)]


class _IniSettings(RevalidatedFrozenModel):
    _IGNORED_OPTIONS: ClassVar[dict[str, frozenset[str]]] = {}

    @classmethod
    def load(cls, path: Path) -> Self:
        from .files import load_ini

        return load_ini(path, cls)

    def _render(self, *, include: set[str] | None = None) -> str:
        return render_ini(self, include=include)


class ClusterSettings(_IniSettings):
    max_snapshots: Annotated[int, Field(ge=1, le=2**31 - 1), IniSection("MISC")] = 6
    console_enabled: Annotated[bool, IniSection("MISC")] = True
    use_alternate_gc: Annotated[bool, IniSection("MISC")] = False
    mods_enabled: Annotated[bool, IniSection("MISC")] = True

    shard_enabled: Annotated[bool, IniSection("SHARD")] = False
    bind_ip: Annotated[IPv4, IniSection("SHARD")] = IPv4Address("127.0.0.1")
    master_ip: Annotated[Host | None, IniSection("SHARD")] = None
    master_port: Annotated[Port, IniSection("SHARD")] = 10888
    cluster_key: Annotated[IniSecret | None, IniSection("SHARD")] = Field(
        default=None, validate_default=True
    )

    steam_group_only: Annotated[bool, IniSection("STEAM")] = False
    steam_group_id: Annotated[int, Field(ge=0, le=2**64 - 1), IniSection("STEAM")] = 0
    steam_group_admins: Annotated[bool, IniSection("STEAM")] = False

    cluster_name: Annotated[
        IniText | None, Field(max_length=80), IniSection("NETWORK")
    ] = None
    cluster_password: Annotated[IniPassword | None, IniSection("NETWORK")] = None
    cluster_description: Annotated[
        IniText, Field(max_length=254), IniSection("NETWORK")
    ] = ""
    tick_rate: Annotated[int, Field(ge=1, le=60), IniSection("NETWORK")] = 15
    offline_cluster: Annotated[bool, IniSection("NETWORK")] = False
    lan_only_cluster: Annotated[bool, IniSection("NETWORK")] = False
    autosaver_enabled: Annotated[bool, IniSection("NETWORK")] = True
    whitelist_slots: Annotated[int, Field(ge=0), IniSection("NETWORK")] = 0
    cluster_language: Annotated[ClusterLanguage, IniSection("NETWORK")] = "en"
    connection_timeout: Annotated[
        int, Field(ge=0, le=2**31 - 1), IniSection("NETWORK")
    ] = 8000
    internet_broadcasting_enabled: Annotated[bool, IniSection("NETWORK")] = True
    idle_timeout: Annotated[int, Field(ge=0, le=2**31 - 1), IniSection("NETWORK")] = (
        1800
    )
    override_dns: Annotated[IniText | None, IniSection("NETWORK")] = None

    max_players: Annotated[int, Field(ge=1, le=64), IniSection("GAMEPLAY")] = 16
    pvp: Annotated[bool, IniSection("GAMEPLAY")] = False
    game_mode: Annotated[NonEmptyIniText, IniSection("GAMEPLAY")] = "survival"
    pause_when_empty: Annotated[bool, IniSection("GAMEPLAY")] = False
    vote_enabled: Annotated[bool, IniSection("GAMEPLAY")] = True

    @field_validator("cluster_key")
    @classmethod
    def _materialize_cluster_key(
        cls, key: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if (
            key is None
            and isinstance(info.context, dict)
            and "cluster_key" in info.context
        ):
            return info.context["cluster_key"] or SecretStr(secrets.token_urlsafe(32))
        return key

    @classmethod
    def load(cls, path: Path) -> Self:
        settings = super().load(path)
        if settings.game_mode in {"endless", "wilderness"}:
            warn(
                f"cluster.ini game_mode={settings.game_mode!r} is deprecated; "
                "use game_mode='survival' and configure worldgenoverride.lua "
                f"with settings_preset={settings.game_mode.upper()!r}",
                FutureWarning,
                stacklevel=2,
            )
        return settings

    @model_validator(mode="after")
    def _validate_related_values(self) -> Self:
        if (
            self.steam_group_only or self.steam_group_admins
        ) and not self.steam_group_id:
            msg = "steam_group_id is required when Steam group restrictions are enabled"
            raise ValueError(msg)
        if self.whitelist_slots > self.max_players:
            msg = "whitelist_slots cannot exceed max_players"
            raise ValueError(msg)
        return self

    def render(self, *, multi_shard: bool = False) -> str:
        if multi_shard and "shard_enabled" not in self.model_fields_set:
            return self.model_copy(update={"shard_enabled": True}).render()
        return self._render(include={"cluster_key"})


class ShardSettings(_IniSettings):
    _IGNORED_OPTIONS: ClassVar[dict[str, frozenset[str]]] = {
        "STEAM": frozenset({"authentication_port"})
    }

    is_master: Annotated[bool, IniSection("SHARD")] = True
    name: Annotated[NonEmptyIniText | None, IniSection("SHARD")] = None
    id: Annotated[int | None, Field(gt=0, le=2**32 - 1), IniSection("SHARD")] = None
    bind_ip: Annotated[IPv4 | None, IniSection("SHARD")] = None
    master_ip: Annotated[Host | None, IniSection("SHARD")] = None
    master_port: Annotated[Port | None, IniSection("SHARD")] = None
    cluster_key: Annotated[IniSecret | None, IniSection("SHARD")] = None

    master_server_port: Annotated[Port, IniSection("STEAM")] = 27016
    server_port: Annotated[Port, IniSection("NETWORK")] = 10999
    encode_user_path: Annotated[bool, IniSection("ACCOUNT")] = True

    @model_validator(mode="after")
    def _validate_id(self) -> Self:
        if self.id is not None and (
            (self.is_master and self.id != MASTER_SHARD_ID)
            or (not self.is_master and self.id < MIN_SECONDARY_SHARD_ID)
        ):
            msg = "master shard id must be 1; secondary shard ids must be at least 2"
            raise ValueError(msg)
        return self

    def render(self, *, multi_shard: bool = False) -> str:
        include = {"encode_user_path"}
        if multi_shard:
            include.add("is_master")
        return self._render(include=include)


def _shared_cluster_key(
    settings: ClusterSettings, shards: Iterable[ShardSettings]
) -> SecretStr | None:
    keys = {
        shard.cluster_key if shard.cluster_key is not None else settings.cluster_key
        for shard in shards
    } or {settings.cluster_key}
    if len(keys) != 1 or SecretStr("") in keys:
        msg = "all shards must use the same non-empty cluster_key or omit it"
        raise ValueError(msg)
    return keys.pop()


def cluster_structure(
    settings: ClusterSettings, shards: Mapping[str, ShardSettings]
) -> dict[tuple[str, ...], object]:
    structure: dict[tuple[str, ...], object] = {
        ("settings", field): getattr(settings, field)
        for field in CLUSTER_STRUCTURE_FIELDS
    }
    if len(shards) > 1 and "shard_enabled" not in settings.model_fields_set:
        structure["settings", "shard_enabled"] = True
    structure.update(
        (("shards", name, "settings", field), getattr(shard, field))
        for name, shard in shards.items()
        for field in ShardSettings.model_fields
    )
    return structure


class ShardConfig(RevalidatedFrozenModel):
    settings: ShardSettings
    level: LevelDataOverride | None = None
    world: WorldgenOverride | None = None
    mods: ModOverrides = Field(default_factory=ModOverrides)

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        level_overrides_type: type[WorldOverrides] | None = None,
        world_overrides_type: type[WorldOverrides] | None = None,
    ) -> Self:
        from .files import load_shard

        return cls.model_validate(
            load_shard(
                directory,
                level_overrides_type=level_overrides_type,
                world_overrides_type=world_overrides_type,
            )
        )

    def files(self, *, multi_shard: bool = False) -> dict[Path, str]:
        validated = type(self).model_validate(self)
        files = {
            Path("server.ini"): validated.settings.render(multi_shard=multi_shard),
            Path("modoverrides.lua"): validated.mods.render(),
        }
        if validated.world is not None:
            files[Path("worldgenoverride.lua")] = validated.world.render()
        if validated.level is not None:
            files[Path("leveldataoverride.lua")] = validated.level.render()
        return files

    def save(self, directory: Path) -> tuple[Path, ...]:
        from .files import write_files

        return write_files(directory, self.files())


class ClusterConfig(RevalidatedFrozenModel):
    settings: ClusterSettings = Field(default_factory=ClusterSettings)
    shards: FrozenMapping[ShardName, ShardConfig] = Field(min_length=1)
    token: ClusterToken = SecretStr("")
    adminlist: PermissionList = ""
    whitelist: PermissionList = ""
    blocklist: PermissionList = ""
    downloads: WorkshopDownloads = Field(default_factory=WorkshopDownloads)
    mod_settings: ModSettings = Field(default_factory=ModSettings)

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        level_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
        world_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
    ) -> Self:
        from .files import load_cluster

        return cls.model_validate(
            load_cluster(
                directory,
                level_overrides_types=level_overrides_types,
                world_overrides_types=world_overrides_types,
            )
        )

    @model_validator(mode="after")
    def _validate_topology(  # ruff: ignore[complex-structure, too-many-branches]
        self,
    ) -> Self:
        folded_names: dict[str, str] = {}
        for name in self.shards:
            folded = name.casefold()
            if previous := folded_names.get(folded):
                msg = f"duplicate DST shard directory names: {previous!r}, {name!r}"
                raise ValueError(msg)
            folded_names[folded] = name

        masters = [
            name for name, shard in self.shards.items() if shard.settings.is_master
        ]
        if len(masters) != 1:
            msg = f"expected exactly one master shard, found {len(masters)}: {masters}"
            raise ValueError(msg)

        multi_shard = len(self.shards) > 1
        if multi_shard:
            if (
                "shard_enabled" in self.settings.model_fields_set
                and not self.settings.shard_enabled
            ):
                msg = "shard_enabled cannot be false for a multi-shard cluster"
                raise ValueError(msg)
            if any(
                not shard.settings.is_master and shard.settings.name is None
                for shard in self.shards.values()
            ):
                msg = "every secondary shard requires a name"
                raise ValueError(msg)
        if multi_shard or self.settings.shard_enabled:
            self._validate_shard_network()

        if self.settings.game_mode in {"quagmire", "lavaarena"}:
            missing = [
                name for name, shard in self.shards.items() if shard.level is None
            ]
            if missing:
                msg = (
                    f"game_mode={self.settings.game_mode!r} requires level data "
                    f"for every shard: {missing}"
                )
                raise ValueError(msg)

        ids = [
            shard.settings.id
            for shard in self.shards.values()
            if not shard.settings.is_master and shard.settings.id is not None
        ]
        if len(ids) != len(set(ids)):
            msg = "DST shard ids must be unique"
            raise ValueError(msg)

        master_port = (
            self.shards[masters[0]].settings.master_port or self.settings.master_port
        )
        ports: dict[int, str] = {master_port: "cluster master_port"}
        for name, shard in self.shards.items():
            for field in (
                "server_port",
                "master_server_port",
            ):
                port = getattr(shard.settings, field)
                if previous := ports.get(port):
                    msg = f"UDP port {port} is shared by {previous} and {name}.{field}"
                    raise ValueError(msg)
                ports[port] = f"{name}.{field}"
        return self

    def _validate_shard_network(self) -> None:
        _shared_cluster_key(
            self.settings, (shard.settings for shard in self.shards.values())
        )

        ports = {
            shard.settings.master_port or self.settings.master_port
            for shard in self.shards.values()
        }
        if len(ports) != 1:
            msg = "all shards must use the same master_port"
            raise ValueError(msg)
        if any(
            not shard.settings.is_master
            and shard.settings.master_ip is None
            and self.settings.master_ip is None
            for shard in self.shards.values()
        ):
            msg = "master_ip is required for every secondary shard"
            raise ValueError(msg)

    def files(self) -> dict[Path, str]:
        validated = type(self).model_validate(self)
        files = {
            Path("cluster.ini"): validated.settings.render(
                multi_shard=len(validated.shards) > 1
            ),
            Path("cluster_token.txt"): (
                f"{token}\n" if (token := validated.token.get_secret_value()) else ""
            ),
            Path("mods/modsettings.lua"): validated.mod_settings.render(),
            Path("adminlist.txt"): validated.adminlist,
            Path("whitelist.txt"): validated.whitelist,
            Path("blocklist.txt"): validated.blocklist,
        }
        multi_shard = len(validated.shards) > 1 or validated.settings.shard_enabled
        for name, shard in sorted(
            validated.shards.items(), key=lambda item: item[0].casefold()
        ):
            for path, content in shard.files(multi_shard=multi_shard).items():
                files[Path(name) / path] = content
        files[Path("mods/dedicated_server_mods_setup.lua")] = (
            validated.resolved_downloads().render()
        )
        return files

    def resolved_downloads(self) -> WorkshopDownloads:
        validated = type(self).model_validate(self)
        workshop_items = set(validated.downloads.items)
        workshop_items.update(validated.mod_settings.workshop_items)
        for shard in validated.shards.values():
            workshop_items.update(shard.mods.workshop_items)
        return WorkshopDownloads(
            items=frozenset(workshop_items),
            collections=validated.downloads.collections,
        )

    def save(self, directory: Path) -> tuple[Path, ...]:
        from .files import save_cluster

        return save_cluster(self, directory)
