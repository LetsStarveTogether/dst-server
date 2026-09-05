"""Native emoji characters and emote commands from DST build 747465.

Definitions: emoji_items.lua, emotes.lua, and emote_items.lua.
"""

from enum import IntEnum, StrEnum, unique
from typing import Self


@unique
class Emoji(StrEnum):
    """Rendered character; chat_token is the corresponding chat input."""

    item_type: str

    ABIGAIL = "\U000f001c", "emoji_abigail"
    ALCHEMY = "\U000f001d", "emoji_alchemyengine"
    ARCANE = "\U000f0000", "emoji_arcane"
    BACKPACK = "\U000f001e", "emoji_backpack"
    BATTLE = "\U000f0018", "emoji_battle"
    BEEFALO = "\U000f0001", "emoji_beefalo"
    BEEHIVE = "\U000f001f", "emoji_beehive"
    BERRY = "\U000f0020", "emoji_berry_bush"
    CARROT = "\U000f0021", "emoji_carrot"
    CHEST = "\U000f0002", "emoji_chest"
    CHESTER = "\U000f0003", "emoji_chester"
    CROCKPOT = "\U000f0004", "emoji_crockpot"
    EGG = "\U000f0022", "emoji_egg"
    EYEBALL = "\U000f0005", "emoji_eyeball"
    EYEPLANT = "\U000f0023", "emoji_eyeplant"
    FAKETEETH = "\U000f0006", "emoji_faketeeth"
    FARM = "\U000f0007", "emoji_farm"
    FIRE = "\U000f0008", "emoji_fire"
    FIREPIT = "\U000f0024", "emoji_firepit"
    FLEX = "\U000f0019", "emoji_flex"
    GHOST = "\U000f0009", "emoji_ghost"
    GOLD = "\U000f001a", "emoji_gold"
    GRAVE = "\U000f000a", "emoji_grave"
    HAMBAT = "\U000f000b", "emoji_hambat"
    HAMMER = "\U000f000c", "emoji_hammer"
    HEART = "\U000f000d", "emoji_heart"
    HORN = "\U000f0025", "emoji_horn"
    HUNGER = "\U000f000e", "emoji_hunger"
    LIGHTBULB = "\U000f000f", "emoji_lightbulb"
    MEAT = "\U000f0026", "emoji_meat_big"
    PIG = "\U000f0010", "emoji_pig"
    POOP = "\U000f0011", "emoji_poop"
    PORTAL = "\U000f0030", "emoji_florid_postern"
    REDGEM = "\U000f0012", "emoji_redgem"
    REFINE = "\U000f0027", "emoji_refine"
    RESURRECTION = "\U000f0031", "emoji_resurrection_stone"
    SALT = "\U000f0028", "emoji_salt"
    SANITY = "\U000f0013", "emoji_sanity"
    SCIENCEMACHINE = "\U000f0014", "emoji_sciencemachine"
    SHADOW = "\U000f0029", "emoji_shadowmanipulator"
    SHOVEL = "\U000f002a", "emoji_shovel"
    SKULL = "\U000f0015", "emoji_skull"
    THUMBSUP = "\U000f002b", "emoji_thumbsup"
    TOPHAT = "\U000f0016", "emoji_tophat"
    TORCH = "\U000f001b", "emoji_torch"
    TRAP = "\U000f002c", "emoji_trap"
    TROPHY = "\U000f002d", "emoji_trophy"
    WAVE = "\U000f002e", "emoji_wave"
    WEB = "\U000f0017", "emoji_web"
    WORMHOLE = "\U000f002f", "emoji_wormhole"

    def __new__(cls, character: str, item_type: str) -> Self:
        member = str.__new__(cls, character)
        member._value_ = character
        member.item_type = item_type
        return member

    @property
    def chat_token(self) -> str:
        return f":{self.name.lower()}:"


@unique
class EmoteType(IntEnum):
    """Native wheel categories, not network command identifiers."""

    EMOTION = 0
    ACTION = 1
    UNLOCKABLE = 2


@unique
class Emote(StrEnum):
    """Canonical command sent through SendSlashCmdToServer without a slash."""

    category: EmoteType
    item_type: str | None
    aliases: tuple[str, ...]

    ANGRY = (
        "angry",
        EmoteType.EMOTION,
        None,
        ("anger", "grimace", "grimaces", "frustrate", "frustrated", "frustration"),
    )
    BIGPET = "bigpet", EmoteType.ACTION, None, ("bigpat",)
    BONESAW = (
        "bonesaw",
        EmoteType.ACTION,
        None,
        ("ready", "goingnowhere", "playtime", "threeminutes"),
    )
    CAROL = "carol", EmoteType.UNLOCKABLE, "emote_carol", ()
    CHEER = "cheer", EmoteType.UNLOCKABLE, "emote_jumpcheer", ()
    CHICKEN = "chicken", EmoteType.UNLOCKABLE, "emote_dance_chicken", ()
    CRY = "cry", EmoteType.EMOTION, None, ("sad", "cries")
    DANCE = "dance", EmoteType.ACTION, None, ()
    FACEPALM = "facepalm", EmoteType.ACTION, None, ("doh", "slapintheface")
    FISTSHAKE = "fistshake", EmoteType.UNLOCKABLE, "emote_fistshake", ()
    FLEX = "flex", EmoteType.UNLOCKABLE, "emote_flex", ()
    HAPPY = "happy", EmoteType.EMOTION, None, ()
    IMPATIENT = "impatient", EmoteType.UNLOCKABLE, "emote_impatient", ()
    JOY = (
        "joy",
        EmoteType.EMOTION,
        None,
        ("click", "heelclick", "heels", "celebrate", "celebration"),
    )
    KISS = "kiss", EmoteType.EMOTION, None, ("blowkiss", "smooch", "mwa", "mwah")
    LAUGH = "laugh", EmoteType.UNLOCKABLE, "emote_laugh", ()
    NO = (
        "no",
        EmoteType.EMOTION,
        None,
        ("annoyed", "annoy", "shakehead", "shake", "confuse", "confused"),
    )
    PET = "pet", EmoteType.ACTION, None, ("pat",)
    POSE = "pose", EmoteType.ACTION, None, ("strut", "strikepose")
    ROBOT = "robot", EmoteType.UNLOCKABLE, "emote_dance_robot", ()
    RUDE = "rude", EmoteType.EMOTION, None, ("goaway", "threaten")
    SHRUG = "shrug", EmoteType.UNLOCKABLE, "emote_shrug", ()
    SIT = "sit", EmoteType.ACTION, None, ()
    SLEEPY = "sleepy", EmoteType.UNLOCKABLE, "emote_sleepy", ("eyerub",)
    SLOWCLAP = "slowclap", EmoteType.UNLOCKABLE, "emote_slowclap", ()
    SQUAT = "squat", EmoteType.ACTION, None, ()
    STEP = "step", EmoteType.UNLOCKABLE, "emote_dance_step", ()
    SWOON = "swoon", EmoteType.UNLOCKABLE, "emote_swoon", ()
    TIPHAT = "tiphat", EmoteType.UNLOCKABLE, "emote_tiphat", ()
    TOAST = "toast", EmoteType.ACTION, None, ("toasting", "cheers")
    WAVE = "wave", EmoteType.EMOTION, None, ("waves", "hi", "bye", "goodbye")
    YAWN = "yawn", EmoteType.UNLOCKABLE, "emote_yawn", ()

    def __new__(
        cls,
        command: str,
        category: EmoteType,
        item_type: str | None,
        aliases: tuple[str, ...],
    ) -> Self:
        member = str.__new__(cls, command)
        member._value_ = command
        member.category = category
        member.item_type = item_type
        member.aliases = aliases
        return member

    @property
    def slash_command(self) -> str:
        return f"/{self.value}"
