from __future__ import annotations

import re
from datetime import date

from pydantic import ConfigDict, RootModel, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from .enums import VersionType
from .schema import KleiModel

VERSION_DATE_PATTERN = re.compile(r"\d{1,2}/\d{1,2}/\d{2}")
VERSION_NUMBER_PATTERN = re.compile(r"\b\d+\b")
PAGE_COUNT_PATTERN = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")
SHORT_YEAR_BASE = 2000


class Version(KleiModel):
    number: int
    type: VersionType
    date: date
    url: str
    row_id: int | None = None
    release_id: int | None = None
    is_current_release: bool = False
    is_hotfix: bool = False

    @model_validator(mode="before")
    @classmethod
    def parse_html_row(cls, value: object) -> object:
        if not isinstance(value, LexborNode):
            return value

        link = value.css_first("a.cRelease")
        heading = value.css_first("h3.ipsType_sectionHead")
        badge = value.css_first("h3.ipsType_sectionHead span.ipsBadge")
        meta = value.css_first(".ipsDataItem_meta")
        if link is None or heading is None or badge is None or meta is None:
            msg = "version row is missing required nodes"
            raise ValueError(msg)

        number_match = VERSION_NUMBER_PATTERN.search(
            heading.text(separator=" ", strip=True)
        )
        date_match = VERSION_DATE_PATTERN.search(meta.text(separator=" ", strip=True))
        url = link.attributes.get("href")
        if number_match is None or date_match is None or url is None:
            msg = "version row is missing number, date, or URL"
            raise ValueError(msg)

        return {
            "number": int(number_match.group()),
            "type": VersionType(badge.text(strip=True)),
            "date": cls.parse_date(date_match.group()),
            "url": url,
            "row_id": cls.parse_optional_int(value.attributes.get("data-rowid")),
            "release_id": cls.parse_optional_int(link.attributes.get("data-releaseid")),
            "is_current_release": "data-currentrelease" in link.attributes,
            "is_hotfix": value.css_first(".cUpdate_hotfix") is not None,
        }

    @staticmethod
    def parse_date(value: str) -> date:
        month, day, year = (int(part) for part in value.split("/"))
        return date(SHORT_YEAR_BASE + year, month, day)

    @staticmethod
    def parse_optional_int(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value.replace(",", ""))
        except ValueError:
            return None


class VersionPage(KleiModel):
    title: str
    page: int | None = None
    page_count: int | None = None
    followers: int | None = None
    versions: tuple[Version, ...]

    @model_validator(mode="before")
    @classmethod
    def parse_html(cls, value: object) -> object:
        if isinstance(value, str):
            tree = LexborHTMLParser(value)
        elif isinstance(value, LexborHTMLParser):
            tree = value
        else:
            return value

        versions = tuple(
            sorted(
                (Version.model_validate(row) for row in tree.css("li.cCmsRecord_row")),
                key=lambda version: (version.date, version.number),
                reverse=True,
            )
        )
        page, page_count = cls.parse_page_numbers(tree)
        return {
            "title": cls.parse_title(tree),
            "page": page,
            "page_count": page_count,
            "followers": cls.parse_followers(tree),
            "versions": versions,
        }

    @staticmethod
    def parse_title(tree: LexborHTMLParser) -> str:
        if title := tree.css_first("h1"):
            return title.text(strip=True)
        if title := tree.css_first("title"):
            return title.text(strip=True)
        return ""

    @staticmethod
    def parse_page_numbers(
        tree: LexborHTMLParser,
    ) -> tuple[int | None, int | None]:
        for pagination in tree.css(".ipsPagination"):
            if match := PAGE_COUNT_PATTERN.search(
                pagination.text(separator=" ", strip=True)
            ):
                return int(match.group(1)), int(match.group(2))
        return None, None

    @staticmethod
    def parse_followers(tree: LexborHTMLParser) -> int | None:
        if count := tree.css_first("[data-role='followButton'] .ipsCommentCount"):
            return Version.parse_optional_int(count.text(strip=True))
        return None


class Builds(RootModel[dict[str, tuple[int | str, ...]]]):
    model_config = ConfigDict(strict=True)


__all__ = ["Builds", "Version", "VersionPage"]
