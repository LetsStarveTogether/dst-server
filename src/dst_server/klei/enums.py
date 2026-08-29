from enum import IntFlag, StrEnum


class Platform(IntFlag):
    STEAM = 0b000001
    PSN = 0b000010
    RAIL = 0b000100
    XBONE = 0b010000
    SWITCH = 0b100000

    @property
    def lobby_name(self) -> str:
        return {
            Platform.STEAM: "Steam",
            Platform.PSN: "PSN",
            Platform.RAIL: "Rail",
            Platform.XBONE: "XBone",
            Platform.SWITCH: "Switch",
        }[self]


class Region(StrEnum):
    US_EAST = "us-east-1"
    EU_CENTRAL = "eu-central-1"
    AP_SOUTHEAST = "ap-southeast-1"
    AP_EAST = "ap-east-1"


class Role(StrEnum):
    WILSON = "wilson"
    WILLOW = "willow"
    WENDY = "wendy"
    WOLFGANG = "wolfgang"
    WX78 = "wx78"
    WICKERBOTTOM = "wickerbottom"
    WES = "wes"
    WAXWELL = "waxwell"
    WOODIE = "woodie"
    WATHGRITHR = "wathgrithr"
    WEBBER = "webber"
    WINONA = "winona"
    WORTOX = "wortox"
    WORMWOOD = "wormwood"
    WARLY = "warly"
    WURT = "wurt"
    WALTER = "walter"
    WANDA = "wanda"
    WONKEY = "wonkey"
    UNKNOWN = ""


class VersionType(StrEnum):
    RELEASE = "Release"
    TEST = "Test"
