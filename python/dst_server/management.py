"""Results returned by room permission and vote operations."""

from typing import TypedDict


class Permissions(TypedDict, closed=True):
    adminlist: list[str]
    blocklist: list[str]
    whitelist: list[str]


class VoteCommand(TypedDict, closed=True):
    command: str
    command_hash: int
    requires_target: bool
    options: list[str]
    timeout: int
    minimum_pass_count: int
    minimum_starter_age: float | None


class VoteOption(TypedDict, closed=True):
    description: str
    count: int


class Voter(TypedDict, closed=True):
    userid: str
    can_vote: bool
    selection: int | None


class Vote(TypedDict, closed=True):
    command: str
    command_hash: int
    starter_userid: str | None
    target_userid: str | None
    countdown: int
    options: list[VoteOption]
    voters: list[Voter]


class VoteStatus(TypedDict, closed=True):
    enabled: bool
    current: Vote | None
