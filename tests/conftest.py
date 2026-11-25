"""Cấu hình dùng chung cho pytest: cô lập test khỏi tệp ``.env`` Compose ở gốc repo."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from trading_bot.config.settings import AppSettings

_REPO_ENV_FILE = (Path(__file__).resolve().parents[1] / ".env").resolve()


class _SkipRepoRootDotEnvSource(PydanticBaseSettingsSource):
    """Bỏ qua ``.env`` ở gốc repo khi chạy pytest, vẫn giữ ``.env`` trong ``tmp_path``."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._source = source

    def get_field_value(self, field: object, field_name: str) -> tuple[object, str, bool]:
        if (Path.cwd() / ".env").resolve() == _REPO_ENV_FILE:
            return None, field_name, False
        return self._source.get_field_value(field, field_name)  # type: ignore[arg-type]

    def __call__(self) -> dict[str, object]:
        if (Path.cwd() / ".env").resolve() == _REPO_ENV_FILE:
            return {}
        return dict(self._source())


_original_customise_sources = AppSettings.settings_customise_sources


def _test_settings_customise_sources(
    cls: type[BaseSettings],
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
    file_secret_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    sources = _original_customise_sources(
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    )
    return (
        sources[0],
        sources[1],
        _SkipRepoRootDotEnvSource(settings_cls, sources[2]),
        sources[3],
    )


AppSettings.settings_customise_sources = classmethod(_test_settings_customise_sources)  # type: ignore[assignment,method-assign]
