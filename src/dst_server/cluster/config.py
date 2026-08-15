from __future__ import annotations

import os
from collections.abc import Mapping
from configparser import ConfigParser
from configparser import Error as ConfigError
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, ClassVar, Literal, Self
from warnings import warn

from pydantic import (
    AfterValidator,
    Field,
    SecretStr,
    model_validator,
)

from dst_server.models.base import RevalidatedFrozenModel

from .layout import PERMISSION_FILES, _parse_bool, discover
from .overrides import (
    FrozenMapping,
    LevelDataOverride,
    ModOverrides,
    ModSettings,
    WorkshopDownloads,
    WorldgenOverride,
)
from .world import WorldOverrides

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
        or name in {".", ".."}
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


def _ini_value(value: object) -> str:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value)
    if any(character in text for character in "\0\r\n"):
        msg = "INI values cannot contain NUL, CR, or LF"
        raise ValueError(msg)
    return text


class _IniSettings(RevalidatedFrozenModel):
    _SECTIONS: ClassVar[tuple[tuple[str, tuple[str, ...]], ...]]

    @classmethod
    def load(cls, path: Path) -> Self:
        parser = ConfigParser(interpolation=None)
        try:
            with path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (ConfigError, OSError) as error:
            msg = f"invalid DST INI configuration: {path}: {error}"
            raise ValueError(msg) from error

        if parser.defaults():
            msg = f"DST INI configuration cannot contain DEFAULT values: {path}"
            raise ValueError(msg)
        sections = {
            section.casefold(): frozenset(fields) for section, fields in cls._SECTIONS
        }
        values: dict[str, str] = {}
        seen_sections: set[str] = set()
        for actual_section in parser.sections():
            section_name = actual_section.casefold()
            if section_name in seen_sections:
                msg = f"duplicate DST INI section: {actual_section}"
                raise ValueError(msg)
            seen_sections.add(section_name)
            fields = sections.get(section_name)
            if fields is None:
                msg = f"unknown DST INI section: {actual_section}"
                raise ValueError(msg)
            for option, value in parser.items(actual_section):
                field = option.casefold()
                if field not in fields:
                    msg = f"unknown DST INI option: {actual_section}.{option}"
                    raise ValueError(msg)
                model_field = cls.model_fields.get(field)
                if model_field is None:
                    continue
                if model_field.annotation is bool:
                    _parse_bool(value)
                values[field] = value
        return cls.model_validate_strings(values)

    def _render(self, forced: dict[str, object] | None = None) -> str:
        validated = type(self).model_validate(self)
        values = {
            field: getattr(validated, field)
            for field in validated.model_fields_set
            if getattr(validated, field) is not None
        }
        if forced is not None:
            values.update(forced)

        sections = []
        for section, fields in self._SECTIONS:
            options = [
                f"{field} = {_ini_value(values[field])}"
                for field in fields
                if field in values
            ]
            if options:
                sections.append(f"[{section}]\n" + "\n".join(options))
        return "\n\n".join(sections) + ("\n" if sections else "")


class ClusterSettings(_IniSettings):
    max_snapshots: Annotated[int, Field(ge=1, le=2**31 - 1)] = 6
    console_enabled: bool = True
    use_alternate_gc: bool = False
    mods_enabled: bool = True

    shard_enabled: bool = False
    bind_ip: IPv4 = IPv4Address("127.0.0.1")
    master_ip: Host | None = None
    master_port: Port = 10888
    cluster_key: IniSecret | None = None

    steam_group_only: bool = False
    steam_group_id: Annotated[int, Field(ge=0, le=2**64 - 1)] = 0
    steam_group_admins: bool = False

    cluster_name: Annotated[IniText, Field(max_length=80)] | None = None
    cluster_password: IniPassword | None = None
    cluster_description: Annotated[IniText, Field(max_length=254)] = ""
    tick_rate: Annotated[int, Field(ge=1, le=60)] = 15
    offline_cluster: bool = False
    lan_only_cluster: bool = False
    autosaver_enabled: bool = True
    whitelist_slots: Annotated[int, Field(ge=0)] = 0
    cluster_language: ClusterLanguage = "en"
    connection_timeout: Annotated[int, Field(ge=0, le=2**31 - 1)] = 8000
    internet_broadcasting_enabled: bool = True
    idle_timeout: Annotated[int, Field(ge=0, le=2**31 - 1)] = 1800
    override_dns: IniText | None = None

    max_players: Annotated[int, Field(ge=1, le=64)] = 16
    pvp: bool = False
    game_mode: NonEmptyIniText = "survival"
    pause_when_empty: bool = False
    vote_enabled: bool = True

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

    _SECTIONS = (
        (
            "MISC",
            (
                "max_snapshots",
                "console_enabled",
                "use_alternate_gc",
                "mods_enabled",
            ),
        ),
        (
            "SHARD",
            (
                "shard_enabled",
                "bind_ip",
                "master_ip",
                "master_port",
                "cluster_key",
            ),
        ),
        (
            "STEAM",
            ("steam_group_only", "steam_group_id", "steam_group_admins"),
        ),
        (
            "NETWORK",
            (
                "cluster_name",
                "cluster_password",
                "cluster_description",
                "tick_rate",
                "offline_cluster",
                "lan_only_cluster",
                "autosaver_enabled",
                "whitelist_slots",
                "cluster_language",
                "connection_timeout",
                "internet_broadcasting_enabled",
                "idle_timeout",
                "override_dns",
            ),
        ),
        (
            "GAMEPLAY",
            (
                "max_players",
                "pvp",
                "game_mode",
                "pause_when_empty",
                "vote_enabled",
            ),
        ),
    )

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
        forced = None
        if multi_shard and "shard_enabled" not in self.model_fields_set:
            forced = {"shard_enabled": True}
        return self._render(forced)


class ShardSettings(_IniSettings):
    is_master: bool = True
    name: NonEmptyIniText | None = None
    id: Annotated[int, Field(gt=0, le=2**32 - 1)] | None = None
    bind_ip: IPv4 | None = None
    master_ip: Host | None = None
    master_port: Port | None = None
    cluster_key: IniSecret | None = None

    master_server_port: Port = 27016
    server_port: Port = 10999
    encode_user_path: bool = False

    _SECTIONS = (
        (
            "SHARD",
            (
                "is_master",
                "name",
                "id",
                "bind_ip",
                "master_ip",
                "master_port",
                "cluster_key",
            ),
        ),
        ("STEAM", ("authentication_port", "master_server_port")),
        ("NETWORK", ("server_port",)),
        ("ACCOUNT", ("encode_user_path",)),
    )

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
        return self._render({"is_master": self.is_master} if multi_shard else None)


def _validate_configuration_directory(path: Path) -> None:
    if path.is_symlink():
        msg = f"configuration directory cannot be a symlink: {path}"
        raise ValueError(msg)
    if not path.is_dir():
        raise NotADirectoryError(path)


def _configuration_file_exists(path: Path) -> bool:
    if path.is_symlink():
        msg = f"managed DST configuration cannot be a symlink: {path}"
        raise ValueError(msg)
    if path.exists() and not path.is_file():
        msg = f"managed DST configuration is not a file: {path}"
        raise ValueError(msg)
    return path.is_file()


def _read_configuration_text(path: Path) -> str:
    if not _configuration_file_exists(path):
        raise FileNotFoundError(path)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as error:
        msg = f"invalid DST text configuration: {path}: {error}"
        raise ValueError(msg) from error


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
        _validate_configuration_directory(directory)
        server_ini = directory / "server.ini"
        if not _configuration_file_exists(server_ini):
            raise FileNotFoundError(server_ini)
        values: dict[str, object] = {"settings": ShardSettings.load(server_ini)}
        modoverrides = directory / "modoverrides.lua"
        if _configuration_file_exists(modoverrides):
            values["mods"] = ModOverrides.load(modoverrides)
        leveldataoverride = directory / "leveldataoverride.lua"
        if _configuration_file_exists(leveldataoverride):
            level = LevelDataOverride.load(
                leveldataoverride,
                overrides_type=level_overrides_type,
            )
            values["level"] = level
        worldgenoverride = directory / "worldgenoverride.lua"
        if _configuration_file_exists(worldgenoverride):
            values["world"] = WorldgenOverride.load(
                worldgenoverride,
                overrides_type=world_overrides_type,
            )
        return cls.model_validate(values)

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

    def save(
        self,
        directory: Path,
    ) -> tuple[Path, ...]:
        return _write_files(directory, self.files())


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
    def load(  # ruff: ignore[complex-structure]
        cls,
        directory: Path,
        *,
        level_overrides_types: Mapping[
            str,
            type[WorldOverrides],
        ]
        | None = None,
        world_overrides_types: Mapping[
            str,
            type[WorldOverrides],
        ]
        | None = None,
    ) -> Self:
        _validate_configuration_directory(directory)
        cluster_ini = directory / "cluster.ini"
        token_path = directory / "cluster_token.txt"
        if not _configuration_file_exists(cluster_ini):
            raise FileNotFoundError(cluster_ini)
        if not _configuration_file_exists(token_path):
            raise FileNotFoundError(token_path)

        shards = {}
        for shard in discover(directory):
            shards[shard.name] = ShardConfig.load(
                directory / shard.name,
                level_overrides_type=(level_overrides_types or {}).get(shard.name),
                world_overrides_type=(world_overrides_types or {}).get(shard.name),
            )
        token = (
            _read_configuration_text(token_path).removesuffix("\r\n").removesuffix("\n")
        )
        settings = ClusterSettings.load(cluster_ini)
        if len(shards) > 1 and "shard_enabled" not in settings.model_fields_set:
            msg = "shard_enabled = true is required in an existing multi-shard cluster"
            raise ValueError(msg)
        if len(shards) > 1 or settings.shard_enabled:
            implicit = sorted(
                name
                for name, shard in shards.items()
                if "is_master" not in shard.settings.model_fields_set
            )
            if implicit:
                msg = (
                    "is_master is required in every existing sharded server.ini: "
                    f"{implicit}"
                )
                raise ValueError(msg)
        values: dict[str, object] = {
            "settings": settings,
            "shards": shards,
            "token": SecretStr(token),
        }
        for name in PERMISSION_FILES:
            path = directory / name
            if _configuration_file_exists(path):
                values[path.stem] = _read_configuration_text(path).replace(
                    "\r\n",
                    "\n",
                )
        mods = directory / "mods"
        if mods.exists() or mods.is_symlink():
            _validate_configuration_directory(mods)
        modsettings = mods / "modsettings.lua"
        if _configuration_file_exists(modsettings):
            values["mod_settings"] = ModSettings.load(modsettings)
        setup = mods / "dedicated_server_mods_setup.lua"
        if _configuration_file_exists(setup):
            values["downloads"] = WorkshopDownloads.load(setup)
        return cls.model_validate(values)

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
        keys = set()
        for shard in self.shards.values():
            key = shard.settings.cluster_key
            if key is None:
                key = self.settings.cluster_key
            if key is None or not key.get_secret_value():
                msg = "a shared cluster_key is required when sharding is enabled"
                raise ValueError(msg)
            keys.add(key.get_secret_value())
        if len(keys) != 1:
            msg = "a shared cluster_key is required when sharding is enabled"
            raise ValueError(msg)

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

    def save(  # ruff: ignore[complex-structure, too-many-branches]
        self,
        directory: Path,
    ) -> tuple[Path, ...]:
        validated = type(self).model_validate(self)
        files = validated.files()
        if directory.is_symlink():
            msg = f"configuration root cannot be a symlink: {directory}"
            raise ValueError(msg)
        if directory.exists() and not directory.is_dir():
            msg = f"configuration root is not a directory: {directory}"
            raise ValueError(msg)
        if directory.is_dir():
            expected = set(validated.shards) | {"mods"}
            unexpected = sorted(
                path.name
                for path in directory.iterdir()
                if path.is_dir() and path.name not in expected
            )
            if unexpected:
                msg = f"unmanaged shard directories would remain active: {unexpected}"
                raise ValueError(msg)
            for name in expected:
                path = directory / name
                if path.is_symlink():
                    msg = f"managed DST directory cannot be a symlink: {path}"
                    raise ValueError(msg)
                if path.exists() and not path.is_dir():
                    msg = f"managed DST directory is not a directory: {path}"
                    raise ValueError(msg)
        token_path = Path("cluster_token.txt")
        preserved = [token_path, *(Path(name) for name in PERMISSION_FILES)]
        setup = Path("mods/dedicated_server_mods_setup.lua")
        for path in (*preserved, Path("mods/modsettings.lua"), setup):
            target = directory / path
            if target.is_symlink():
                msg = f"managed DST configuration cannot be a symlink: {target}"
                raise ValueError(msg)
        if (
            "token" not in validated.model_fields_set
            and (directory / token_path).is_file()
        ):
            files.pop(token_path)
        for name in PERMISSION_FILES:
            path = Path(name)
            if (
                path.stem not in validated.model_fields_set
                and (directory / path).is_file()
            ):
                files.pop(path)
        modsettings = Path("mods/modsettings.lua")
        if (
            "mod_settings" not in validated.model_fields_set
            and (directory / modsettings).is_file()
        ):
            from .mods import setup_downloads

            existing_items, existing_collections = setup_downloads(directory / setup)
            downloads = validated.resolved_downloads()
            files[setup] = WorkshopDownloads(
                items=downloads.items.union(existing_items),
                collections=downloads.collections.union(existing_collections),
            ).render()
            files.pop(modsettings)

        return _write_files(directory, files, directories=(Path("mods/ugc"),))


def _write_files(  # ruff: ignore[complex-structure, too-many-branches, too-many-statements]
    root: Path,
    files: dict[Path, str],
    *,
    directories: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    if root.is_symlink():
        msg = f"configuration root cannot be a symlink: {root}"
        raise ValueError(msg)
    if root.exists() and not root.is_dir():
        msg = f"configuration root is not a directory: {root}"
        raise ValueError(msg)
    relatives = (*files, *directories)
    for path in relatives:
        if path.is_absolute() or not path.parts or ".." in path.parts:
            msg = f"unsafe relative configuration path: {path}"
            raise ValueError(msg)
        try:
            path.as_posix().encode("utf-8")
        except UnicodeEncodeError as error:
            msg = f"configuration paths must contain valid UTF-8: {path!r}"
            raise ValueError(msg) from error
    for content in files.values():
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as error:
            msg = "DST configuration files must contain valid UTF-8"
            raise ValueError(msg) from error

    file_paths = set(files)
    if file_paths.intersection(directories):
        msg = "configuration path is both a file and directory"
        raise ValueError(msg)
    for relative in relatives:
        if any(parent in file_paths for parent in relative.parents):
            msg = f"configuration file is an ancestor of another path: {relative}"
            raise ValueError(msg)

    required_directories = set(directories)
    for relative in relatives:
        required_directories.update(
            parent for parent in relative.parents if parent != Path()
        )

    root_resolved = root.resolve()
    ordered_directories = sorted(
        required_directories,
        key=lambda path: (len(path.parts), path.as_posix()),
    )
    for relative in ordered_directories:
        path = root / relative
        if path.is_symlink():
            msg = f"managed DST directory cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.exists() and not path.is_dir():
            msg = f"managed DST directory is not a directory: {path}"
            raise ValueError(msg)
    targets: set[Path] = set()
    for relative in files:
        path = root / relative
        if path.is_symlink():
            msg = f"managed DST configuration cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.exists() and not path.is_file():
            msg = f"managed DST configuration is not a file: {path}"
            raise ValueError(msg)
        parent = path.parent.resolve()
        if not parent.is_relative_to(root_resolved):
            msg = f"configuration path escapes cluster root: {path}"
            raise ValueError(msg)
        target = parent / path.name
        if target in targets:
            msg = f"configuration paths resolve to the same target: {path}"
            raise ValueError(msg)
        targets.add(target)

    root.mkdir(parents=True, exist_ok=True)
    for relative in ordered_directories:
        (root / relative).mkdir(exist_ok=True)

    written = []
    for relative, content in sorted(files.items(), key=lambda item: item[0].as_posix()):
        path = root / relative
        mode = (
            0o600
            if relative.name
            in {
                "cluster.ini",
                "cluster_token.txt",
                "server.ini",
            }
            else 0o644
        )
        _atomic_write(path, content, mode)
        written.append(path)
    return tuple(written)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "ClusterConfig",
    "ClusterSettings",
    "ShardConfig",
    "ShardSettings",
]
