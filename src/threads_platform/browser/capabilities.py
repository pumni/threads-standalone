"""Versioned result shapes for local browser reads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserFeedItemResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_ref: str | None = Field(default=None, max_length=255)
    author_username: str | None = Field(default=None, max_length=255)
    text_excerpt: str | None = Field(default=None, max_length=500)
    position: int = Field(ge=0, le=19)


class BrowserFeedResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    observations: tuple[BrowserFeedItemResultV1, ...] = Field(max_length=20)
    truncated: bool


class BrowserSearchItemResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_ref: str = Field(min_length=1, max_length=255)
    author_username: str = Field(min_length=1, max_length=30)
    text_excerpt: str | None = Field(default=None, max_length=500)
    position: int = Field(ge=0, le=19)
    source: Literal["SYNTHETIC_FIXTURE", "MANUAL_URL"]
    context_status: Literal["COMPLETE", "PARTIAL", "UNKNOWN"]
    truncated: bool


class BrowserSearchResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    query: str = Field(min_length=1, max_length=120)
    observations: tuple[BrowserSearchItemResultV1, ...] = Field(max_length=20)
    truncated: bool
    source: Literal["SYNTHETIC_FIXTURE"]


class BrowserConversationItemResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_ref: str = Field(min_length=1, max_length=255)
    author_username: str = Field(min_length=1, max_length=30)
    text_excerpt: str | None = Field(default=None, max_length=500)
    parent_ref: str | None = Field(default=None, max_length=255)
    position: int = Field(ge=0, le=19)
    truncated: bool


class BrowserConversationResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    root_thread_ref: str = Field(min_length=1, max_length=255)
    observations: tuple[BrowserConversationItemResultV1, ...] = Field(max_length=20)
    context_status: Literal["COMPLETE", "PARTIAL", "UNKNOWN"]
    truncated: bool
    source: Literal["SYNTHETIC_FIXTURE"]


class BrowserTargetOpenResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_version: Literal[1] = 1
    target_kind: Literal["THREAD", "PROFILE"]
    target_ref: str = Field(min_length=1, max_length=255)
    recognized: Literal[True] = True
