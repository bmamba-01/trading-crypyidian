"""Fail-closed loader cho phép replay chỉ sau khi manifest và quyền còn hợp lệ."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetManifest,
    backtest_dataset_bar_identity_hash,
    backtest_dataset_bars_hash,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import OHLCVBar

_BACKFILL_SOURCE = "BINANCE_USDM_REST_BACKFILL"


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayInput:
    """Đầu vào replay đã được kiểm tra quyền và toàn vẹn bar."""

    manifest: BacktestDatasetManifest
    bars: tuple[OHLCVBar, ...]
    current_rights: BackfillRightsEvidence


class BacktestDatasetManifestLookup(Protocol):
    """Đọc manifest theo hash trong workspace hiện hành."""

    async def get_by_hash(
        self,
        *,
        workspace_id: UUID,
        dataset_hash: str,
    ) -> BacktestDatasetManifest | None: ...


class CurrentBackfillRightsGate(Protocol):
    """Kiểm policy hiện hành bằng Source Registry và database clock."""

    async def authorize(
        self,
        *,
        request: RestBackfillRequest,
        now: datetime,
    ) -> BackfillRightsEvidence: ...


class ReplayMarketData(Protocol):
    """Đọc bar point-in-time theo workspace và nguồn đã được cấp quyền."""

    async def list_bars(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
        closed_only: bool = False,
        source: str | None = None,
    ) -> list[OHLCVBar]: ...


class BacktestDatasetRightsChanged(PermissionError):
    """Manifest được tạo theo policy cũ hoặc fingerprint quyền không còn khớp."""


class BacktestDatasetDataChanged(RuntimeError):
    """Dữ liệu tại mốc snapshot thiếu hoặc khác nội dung manifest."""


class BacktestDatasetReplayLoader:
    """Đọc READY manifest và revalidate policy trong cùng Unit of Work."""

    def __init__(
        self,
        *,
        repository: BacktestDatasetManifestLookup,
        rights_gate: CurrentBackfillRightsGate,
        market_data: ReplayMarketData,
    ) -> None:
        self._repository = repository
        self._rights_gate = rights_gate
        self._market_data = market_data

    async def load_for_replay(
        self,
        *,
        workspace_id: UUID,
        dataset_hash: str,
        now: datetime,
    ) -> BacktestDatasetReplayInput:
        """Chỉ trả snapshot khi coverage, quyền và toàn vẹn bar còn hợp lệ."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        manifest = await self._repository.get_by_hash(
            workspace_id=workspace_id,
            dataset_hash=dataset_hash,
        )
        if manifest is None:
            raise LookupError("Không tìm thấy backtest dataset manifest trong workspace.")
        if type(manifest) is not BacktestDatasetManifest:
            raise ValueError("Repository trả về kiểu manifest không hợp lệ.")
        if manifest.rights_workspace_id != workspace_id:
            raise PermissionError("Manifest không thuộc workspace đã được duyệt quyền.")
        manifest.require_ready()
        evidence = await self._authorize_current_rights(
            manifest=manifest, workspace_id=workspace_id, now=now
        )
        bars = tuple(
            await self._market_data.list_bars(
                workspace_id=workspace_id,
                symbol=manifest.symbol,
                timeframe=manifest.interval,
                start=manifest.warmup_start,
                end=manifest.range_end,
                as_of=manifest.prepared_at,
                closed_only=True,
                source=_BACKFILL_SOURCE,
            )
        )
        if len(bars) != manifest.expected_bar_count:
            raise BacktestDatasetDataChanged(
                "Số lượng bar đọc lại không khớp snapshot READY của manifest."
            )
        try:
            if manifest.schema_version == 2 and any(bar.quote_volume is not None for bar in bars):
                raise BacktestDatasetDataChanged(
                    "Manifest schema v2 không ghim quote volume hiện có trong dữ liệu."
                )
            observed_hash = backtest_dataset_bars_hash(bars, schema_version=manifest.schema_version)
            observed_identity_hash = (
                backtest_dataset_bar_identity_hash(bars, workspace_id=workspace_id)
                if manifest.schema_version == 4
                else None
            )
        except ValueError as error:
            raise BacktestDatasetDataChanged("Dữ liệu bar đọc lại không hợp lệ.") from error
        if observed_hash != manifest.bars_content_hash:
            raise BacktestDatasetDataChanged("Hash bar đọc lại không khớp snapshot của manifest.")
        if manifest.schema_version == 4 and observed_identity_hash != manifest.bar_identity_hash:
            raise BacktestDatasetDataChanged(
                "Identity record/revision bar đọc lại không khớp manifest."
            )
        return BacktestDatasetReplayInput(
            manifest=manifest,
            bars=bars,
            current_rights=evidence,
        )

    async def revalidate_current_rights(
        self,
        replay_input: BacktestDatasetReplayInput,
        *,
        workspace_id: UUID,
        now: datetime,
    ) -> BacktestDatasetReplayInput:
        """Lấy evidence mới cho input đã nạp mà không đọc lại bars từ kho."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        if type(replay_input) is not BacktestDatasetReplayInput:
            raise ValueError("replay_input phải là đầu vào replay đã nạp.")
        manifest = replay_input.manifest
        if manifest.rights_workspace_id != workspace_id:
            raise PermissionError("Manifest không thuộc workspace được revalidate quyền.")
        manifest.require_ready()
        evidence = await self._authorize_current_rights(
            manifest=manifest, workspace_id=workspace_id, now=now
        )
        return replace(replay_input, current_rights=evidence)

    async def _authorize_current_rights(
        self,
        *,
        manifest: BacktestDatasetManifest,
        workspace_id: UUID,
        now: datetime,
    ) -> BackfillRightsEvidence:
        request = RestBackfillRequest(
            symbol=manifest.symbol,
            interval=manifest.interval,
            start=manifest.warmup_start,
            end=manifest.range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        evidence = await self._rights_gate.authorize(request=request, now=now)
        if (
            type(evidence) is not BackfillRightsEvidence
            or evidence.workspace_id != workspace_id
            or evidence.provider != manifest.rights_provider
            or evidence.purpose.value != manifest.rights_purpose
        ):
            raise PermissionError("Rights gate trả evidence không khớp manifest backtest.")
        if evidence.policy_fingerprint != manifest.policy_fingerprint:
            raise BacktestDatasetRightsChanged(
                "Policy quyền hiện hành đã đổi; cần chuẩn bị lại hoặc review manifest."
            )
        return evidence
