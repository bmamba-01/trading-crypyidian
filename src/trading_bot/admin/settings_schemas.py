"""Lược đồ HTTP cho quản trị cấu hình workspace."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from trading_bot.config.workspace_settings import WorkspaceSettings


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: WorkspaceSettings | None = None
    expected_parent_version_id: UUID | None = None


class UpdateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    settings: WorkspaceSettings


class ProposeDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class ApproveDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class PublishDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
