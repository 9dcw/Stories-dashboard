from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GoogleCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=1, max_length=8192)


class DismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Literal["not_relevant", "duplicate", "too_old", "weak_source", "other"]
    note: str = Field(default="", max_length=1000)
    request_id: UUID


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dismissal_feedback_id: int = Field(gt=0)
    request_id: UUID


class Phase2CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_id: int = Field(gt=0)
    completion_token: str = Field(min_length=1, max_length=256)


class Phase2Callback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    completion_token: str = Field(min_length=1, max_length=256)
    state: Literal['waiting', 'complete', 'failed']
    step: Literal['topic', 'drive', 'document', 'verification', 'complete', 'failed']
    error: str = Field(default='', max_length=1000)
    artifacts: dict[str, str] = Field(default_factory=dict, max_length=10)


class EditorialEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: str = Field(min_length=1, max_length=40)
    thread_id: str = Field(min_length=1, max_length=40)
    user_id: str = Field(min_length=1, max_length=40)
    message_id: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=6000)
