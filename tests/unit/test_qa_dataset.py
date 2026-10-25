"""Bất biến của bộ dữ liệu QA và chốt an toàn của seed CLI."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import fields, is_dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from subprocess import CompletedProcess
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from scripts import seed_qa_data
from scripts.qa_seed_layers import (
    SEED_LAYERS,
    DatabaseFacts,
    bot_config_publication,
    releasable_references,
    validate_database_facts,
)
from scripts.seed_qa_data import (
    SeedGuardError,
    build_apply_payload,
    load_or_create_credentials,
    parse_apply_payload,
    redact_secrets,
    validate_apply_environment,
    validate_credentials_path,
    validate_docker_endpoint,
)
from tests.fixtures.qa_dataset import (
    MARKET_END,
    QA_NAMESPACE,
    QA_OTHER_WORKSPACE_KEY,
    QA_WORKSPACE_KEY,
    QaDataset,
    build_qa_dataset,
    qa_id,
)

from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.auth.bot_policy import BotPermission, role_allows_bot_permission
from trading_bot.auth.policy import Role
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.config.workspace_settings import WorkspaceSettings
from trading_bot.domain.strategy.registry import StrategyRegistry

DATASET_PACKAGE = Path(__file__).parents[1] / "fixtures" / "qa_dataset"
LOCAL_APPLY_ENVIRONMENT = {
    "TRADING_BOT_APP_ENV": "local",
    "TRADING_BOT_TRADING_MODE": "PAPER",
    "TRADING_BOT_DATABASE_HOST": "postgres",
    "TRADING_BOT_DATABASE_NAME": "crypidian",
    "TRADING_BOT_DATABASE_USER": "crypidian_admin",
    "TRADING_BOT_DATABASE_PASSWORD_FILE": "/run/secrets/postgres_password",
    "TRADING_BOT_MFA_ENCRYPTION_KEY_FILE": "/run/secrets/mfa_encryption_key",
    "TRADING_BOT_MFA_SECRET_DIRECTORY": "/var/lib/crypidian/mfa-secrets",
}


@pytest.fixture(scope="module")
def dataset() -> QaDataset:
    return build_qa_dataset()


def _all_identifiers(dataset: QaDataset) -> list[UUID]:
    identifiers: list[UUID] = [workspace.id for workspace in dataset.workspaces]
    for member in dataset.members:
        identifiers.extend(
            (member.user_id, member.membership_id, member.mfa_factor_id, member.secret_reference_id)
        )
        identifiers.extend(member.role_binding_id(role) for role in member.roles)
    identifiers.extend(account.id for account in dataset.accounts)
    identifiers.extend(grant.id for grant in dataset.grants)
    identifiers.extend(profile.id for profile in dataset.risk_profiles)
    identifiers.extend(version.id for version in dataset.settings_versions)
    identifiers.extend(seed.id for seed in dataset.paper_seeds)
    identifiers.extend(release.id for release in dataset.strategy_releases)
    identifiers.extend(item.version.record_id for item in dataset.bars)
    return identifiers


def test_dataset_identifiers_are_deterministic_uuid5_and_unique(dataset: QaDataset) -> None:
    rebuilt = build_qa_dataset()
    identifiers = _all_identifiers(dataset)

    assert rebuilt == dataset
    assert _all_identifiers(rebuilt) == identifiers
    # Đổi namespace sẽ làm dữ liệu đã seed bị mồ côi; khóa giá trị để phát hiện đổi ngoài ý muốn.
    assert uuid5(NAMESPACE_URL, "https://crypidian.invalid/qa-dataset/v1") == QA_NAMESPACE
    assert qa_id("workspace", QA_WORKSPACE_KEY) == uuid5(QA_NAMESPACE, "workspace:qa")
    assert dataset.workspace(QA_WORKSPACE_KEY).id == qa_id("workspace", QA_WORKSPACE_KEY)
    assert all(identifier.version == 5 for identifier in identifiers)
    assert len(set(identifiers)) == len(identifiers)


def test_qa_workspace_has_one_member_per_role_with_lowercase_role_values(
    dataset: QaDataset,
) -> None:
    active = [
        member
        for member in dataset.members_of(QA_WORKSPACE_KEY)
        if member.membership_status == "ACTIVE"
    ]

    assert sorted(role for member in active for role in member.roles) == sorted(Role)
    assert all(len(member.roles) == 1 for member in active)
    for member in dataset.members:
        assert member.user_status == "ACTIVE"
        assert all(role.value == role.value.lower() for role in member.roles)
        assert member.email == member.email.casefold()
        assert member.email.endswith("@example.test")


def test_operator_and_analyst_grants_are_limited_to_bot_a(dataset: QaDataset) -> None:
    def grants_of(member_key: str) -> set[tuple[str, BotPermission]]:
        return {
            (grant.account_key, grant.permission)
            for grant in dataset.grants
            if grant.member_key == member_key
        }

    assert grants_of("qa.operator") == {
        ("A", BotPermission.READ),
        ("A", BotPermission.RUNTIME_PAUSE),
        ("A", BotPermission.RUNTIME_RESUME),
    }
    assert grants_of("qa.analyst_reviewer") == {
        ("A", BotPermission.READ),
        ("A", BotPermission.REPORT_READ),
    }
    assert {grant.account_key for grant in dataset.grants} == {"A"}
    for workspace_admin in ("qa.owner", "qa.strategy_admin", "qa.risk_admin"):
        assert grants_of(workspace_admin) == set()


def test_grants_stay_within_role_ceiling_and_are_issued_by_workspace_owner(
    dataset: QaDataset,
) -> None:
    for grant in dataset.grants:
        member = dataset.member(grant.member_key)
        grantor = dataset.member(grant.granted_by_member_key)
        account = dataset.account(grant.account_key)

        assert role_allows_bot_permission(frozenset(member.roles), grant.permission)
        assert Role.OWNER in grantor.roles
        assert grantor.membership_status == "ACTIVE"
        assert member.workspace_key == grantor.workspace_key == account.workspace_key
        assert grant.workspace_key == account.workspace_key


def test_suspended_member_keeps_role_and_grant_but_membership_is_not_active(
    dataset: QaDataset,
) -> None:
    suspended = dataset.member("qa.suspended")

    assert suspended.workspace_key == QA_WORKSPACE_KEY
    assert suspended.membership_status == "SUSPENDED"
    # Role workspace-wide và grant còn hiệu lực: phép thử từ chối phải đến từ trạng thái membership.
    assert suspended.roles == (Role.RISK_ADMIN,)
    assert [
        (grant.account_key, grant.permission) for grant in dataset.grants_of(suspended.key)
    ] == [("A", BotPermission.READ)]


def test_other_workspace_is_fully_separate_from_qa_workspace(dataset: QaDataset) -> None:
    qa = dataset.workspace(QA_WORKSPACE_KEY)
    other = dataset.workspace(QA_OTHER_WORKSPACE_KEY)
    other_members = dataset.members_of(QA_OTHER_WORKSPACE_KEY)
    qa_members = dataset.members_of(QA_WORKSPACE_KEY)

    assert qa.slug == "crypidian-qa"
    assert other.slug == "crypidian-qa-other"
    assert qa.name == "Crypidian QA"
    assert other.name == "Crypidian QA Other"
    assert {workspace.mode for workspace in dataset.workspaces} == {"PAPER"}
    assert [(member.key, member.roles) for member in other_members] == [
        ("qa_other.owner", (Role.OWNER,))
    ]
    assert [account.key for account in dataset.accounts_of(QA_OTHER_WORKSPACE_KEY)] == ["OTHER"]
    assert {member.user_id for member in qa_members}.isdisjoint(
        member.user_id for member in other_members
    )
    assert len({member.email for member in dataset.members}) == len(dataset.members)
    for seed in dataset.paper_seeds:
        assert dataset.account(seed.account_key).workspace_key == seed.workspace_key
    for version in dataset.settings_versions:
        assert dataset.member(version.created_by_member_key).workspace_key == (
            version.workspace_key
        )
    assert {instrument.workspace_key for instrument in dataset.instruments} == {QA_WORKSPACE_KEY}
    assert {bar.workspace_key for bar in dataset.bars} == {QA_WORKSPACE_KEY}


def test_paper_accounts_have_expected_status_and_seed_balances(dataset: QaDataset) -> None:
    statuses = {account.key: account.status for account in dataset.accounts}

    assert statuses == {"A": "ENABLED", "B": "ENABLED", "C": "DISABLED", "OTHER": "ENABLED"}
    assert {(a.mode, a.venue, a.environment) for a in dataset.accounts} == {
        ("PAPER", "binance_usdm", "local")
    }
    assert sorted(seed.account_key for seed in dataset.paper_seeds) == ["A", "B", "C", "OTHER"]
    for seed in dataset.paper_seeds:
        account = dataset.account(seed.account_key)
        settings = dataset.settings_for(seed.workspace_key)
        assert isinstance(seed.initial_equity, Decimal)
        assert seed.initial_equity == settings.settings.paper_execution.starting_equity
        assert seed.quote_asset == settings.settings.paper_execution.quote_asset == "USDT"
        # Khớp khóa của PaperAccountSeedRepository để runtime không xem seed là xung đột.
        assert seed.idempotency_key == f"paper-seed:{account.id}"
        assert (seed.settings_version_id, seed.settings_hash) == (
            settings.id,
            settings.settings_hash,
        )
    assert all(isinstance(account.max_notional, Decimal) for account in dataset.accounts)


def test_published_settings_validate_and_hash_like_repository(dataset: QaDataset) -> None:
    assert sorted(version.workspace_key for version in dataset.settings_versions) == sorted(
        [QA_WORKSPACE_KEY, QA_OTHER_WORKSPACE_KEY]
    )
    for version in dataset.settings_versions:
        profile = next(
            item for item in dataset.risk_profiles if item.workspace_key == version.workspace_key
        )
        owner = dataset.member(version.approved_by_member_key)

        assert version.version == 1
        assert WorkspaceSettings.model_validate(version.settings_json) == version.settings
        assert canonical_settings(version.settings) == version.settings_json
        assert settings_hash(version.settings_json) == version.settings_hash
        assert version.settings.risk.profile_version_id == profile.id
        assert Role.OWNER in owner.roles
        assert owner.workspace_key == version.workspace_key
        assert version.effective_at.tzinfo is UTC
        assert version.published_at == version.effective_at
    qa_settings = dataset.settings_for(QA_WORKSPACE_KEY).settings
    assert qa_settings.market_data.symbols == ("BTCUSDT", "ETHUSDT")
    assert qa_settings.strategy.default_release == "h03-v1"


def test_market_fixtures_are_closed_contiguous_and_cover_built_in_strategies(
    dataset: QaDataset,
) -> None:
    assert sorted(item.observation.instrument.symbol for item in dataset.instruments) == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        hourly = dataset.bars_for(symbol, "1h")
        four_hour = dataset.bars_for(symbol, "4h")
        # H06 cần 121 nến 1h, H03 cần 56; H01 cần EMA200 trên 4h.
        assert len(hourly) == 240
        assert len(four_hour) == 220
        for series, step in ((hourly, timedelta(hours=1)), (four_hour, timedelta(hours=4))):
            assert series[-1].close_time == MARKET_END
            for previous, current in zip(series, series[1:], strict=False):
                assert current.open_time - previous.open_time == step
            for bar in series:
                assert bar.is_closed
                assert bar.open_time.tzinfo is UTC
                assert bar.close_time - bar.open_time == step
                assert all(
                    isinstance(value, Decimal)
                    for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
                )
        for item in dataset.bars:
            version, bar = item.version, item.bar
            assert (version.stream.source, version.stream.symbol, version.stream.timeframe) == (
                bar.source,
                bar.symbol,
                bar.timeframe,
            )
            assert version.content_checksum == bar.checksum
            assert (version.event_at, version.received_at, version.available_at) == (
                bar.event_at,
                bar.received_at,
                bar.available_at,
            )
        # Nến 4h trùng giai đoạn với 240 nến 1h phải là tổng hợp OHLCV đúng của chúng.
        for index, aggregate in enumerate(four_hour[-60:]):
            parts = hourly[index * 4 : index * 4 + 4]
            assert aggregate.open_time == parts[0].open_time
            assert (aggregate.open, aggregate.close) == (parts[0].open, parts[-1].close)
            assert aggregate.high == max(part.high for part in parts)
            assert aggregate.low == min(part.low for part in parts)
            assert aggregate.volume == sum((part.volume for part in parts), Decimal(0))

    def breaks_out(symbol: str) -> bool:
        bars = dataset.bars_for(symbol, "1h")
        current, prior = bars[-1], bars[-56:-1]
        volumes = [bar.volume for bar in bars[-21:-1]]
        return current.close > max(bar.high for bar in prior) and current.volume > median(volumes)

    assert breaks_out("BTCUSDT") is True
    assert breaks_out("ETHUSDT") is False


def test_dataset_package_contains_no_plaintext_secrets(dataset: QaDataset) -> None:
    sources = {path.name: path.read_text(encoding="utf-8") for path in DATASET_PACKAGE.glob("*.py")}
    forbidden_words = re.compile(
        r"password\s*[=:]|totp_secret|token_urlsafe|random_base32|\$argon2", re.IGNORECASE
    )
    # TOTP seed Base32 viết hoa liền mạch, ví dụ 16+ ký tự A-Z/2-7.
    base32_seed = re.compile(r"(?<![A-Za-z0-9_])[A-Z2-7]{16,}(?![A-Za-z0-9_])")

    assert sources, "Thiếu package dataset QA"
    for name, source in sources.items():
        assert forbidden_words.search(source) is None, name
        assert base32_seed.search(source) is None, name

    def visit(value: object) -> None:
        if is_dataclass(value) and not isinstance(value, type):
            names = {item.name for item in fields(value)}
            assert names.isdisjoint({"password", "password_hash", "totp_secret", "mfa_secret"})
            for item in fields(value):
                visit(getattr(value, item.name))
        elif isinstance(value, tuple):
            for child in value[:3]:
                visit(child)

    visit(dataset)


def test_apply_guard_requires_flag_and_local_paper_environment() -> None:
    validate_apply_environment(LOCAL_APPLY_ENVIRONMENT, local_only=True)
    for host in ("localhost", "127.0.0.1", "::1", "db.localhost", "pg.test"):
        validate_apply_environment(
            LOCAL_APPLY_ENVIRONMENT | {"TRADING_BOT_DATABASE_HOST": host}, local_only=True
        )

    with pytest.raises(SeedGuardError, match="--local-only"):
        validate_apply_environment(LOCAL_APPLY_ENVIRONMENT, local_only=False)
    rejected = (
        {"TRADING_BOT_APP_ENV": "production"},
        {"TRADING_BOT_APP_ENV": ""},
        {"TRADING_BOT_TRADING_MODE": "DEMO"},
        {"TRADING_BOT_DATABASE_HOST": "db.example.com"},
        {"TRADING_BOT_DATABASE_HOST": "10.0.0.5"},
        {"TRADING_BOT_DATABASE_HOST": "postgres.prod.internal"},
    )
    for override in rejected:
        with pytest.raises(SeedGuardError):
            validate_apply_environment(LOCAL_APPLY_ENVIRONMENT | override, local_only=True)
    missing = dict(LOCAL_APPLY_ENVIRONMENT)
    del missing["TRADING_BOT_APP_ENV"]
    with pytest.raises(SeedGuardError):
        validate_apply_environment(missing, local_only=True)


def test_seed_guard_accepts_only_local_docker_endpoints() -> None:
    for endpoint in (
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "unix:///var/run/docker.sock",
        "tcp://127.0.0.1:2375",
        "tcp://localhost:2375",
    ):
        validate_docker_endpoint(endpoint)
    for endpoint in ("", "tcp://10.1.2.3:2376", "ssh://ops@prod-host", "https://docker.example"):
        with pytest.raises(SeedGuardError):
            validate_docker_endpoint(endpoint)


def test_credentials_path_must_stay_inside_secrets_directory(tmp_path: Path) -> None:
    expected = tmp_path / ".secrets" / "qa_accounts.json"

    assert validate_credentials_path(expected, tmp_path) == expected.resolve()
    for candidate in (
        tmp_path / "qa_accounts.json",
        tmp_path / ".secrets" / ".." / "docs" / "qa_accounts.json",
        tmp_path / ".secrets" / "qa_accounts.txt",
    ):
        with pytest.raises(SeedGuardError):
            validate_credentials_path(candidate, tmp_path)


def test_database_facts_guard_refuses_non_paper_or_foreign_rows(dataset: QaDataset) -> None:
    qa = dataset.workspace(QA_WORKSPACE_KEY)
    owner = dataset.member("qa.owner")
    clean = DatabaseFacts(
        privileged=True,
        missing_tables=(),
        non_paper_workspaces=0,
        non_paper_accounts=0,
        slug_owners={qa.slug: qa.id},
        email_owners={owner.email: owner.user_id},
        account_name_owners={},
        settings_v1_owners={},
    )

    validate_database_facts(clean, dataset)
    foreign = UUID(int=7)
    for override in (
        {"privileged": False},
        {"missing_tables": ("bot_instance",)},
        {"non_paper_workspaces": 1},
        {"non_paper_accounts": 2},
        {"slug_owners": {qa.slug: foreign}},
        {"email_owners": {owner.email: foreign}},
        {"account_name_owners": {(qa.id, "qa-paper-a"): foreign}},
        {"settings_v1_owners": {qa.id: foreign}},
    ):
        values = {item.name: getattr(clean, item.name) for item in fields(clean)} | override
        with pytest.raises(SeedGuardError):
            validate_database_facts(DatabaseFacts(**values), dataset)


def test_credentials_are_generated_once_reused_and_written_restricted(
    dataset: QaDataset, tmp_path: Path
) -> None:
    path = tmp_path / ".secrets" / "qa_accounts.json"

    created = load_or_create_credentials(path, dataset)
    document = json.loads(path.read_text(encoding="utf-8"))
    reused = load_or_create_credentials(path, dataset)

    assert set(created) == {member.key for member in dataset.members}
    assert reused == created
    assert len({entry.password for entry in created.values()}) == len(created)
    for key, entry in created.items():
        member = dataset.member(key)
        assert len(entry.password) >= 14
        assert re.fullmatch(r"[A-Z2-7]{16,}", entry.totp_secret)
        assert entry.password not in repr(entry)
        assert entry.totp_secret not in repr(entry)
        stored = document["accounts"][key]
        assert stored["email"] == member.email
        assert stored["workspace_slug"] == dataset.workspace(member.workspace_key).slug
        assert stored["roles"] == [role.value for role in member.roles]
        assert "password_hash" not in stored
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    else:
        assert path.is_file()
        assert os.access(path, os.R_OK | os.W_OK)


def test_credentials_regenerate_only_missing_or_invalid_entries(
    dataset: QaDataset, tmp_path: Path
) -> None:
    path = tmp_path / ".secrets" / "qa_accounts.json"
    original = load_or_create_credentials(path, dataset)
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["accounts"]["qa.operator"]
    document["accounts"]["qa.analyst_reviewer"]["password"] = "short"
    path.write_text(json.dumps(document), encoding="utf-8")

    repaired = load_or_create_credentials(path, dataset)

    assert repaired["qa.operator"] != original["qa.operator"]
    assert repaired["qa.analyst_reviewer"].password != "short"
    for key in set(original) - {"qa.operator", "qa.analyst_reviewer"}:
        assert repaired[key] == original[key]
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SeedGuardError):
        load_or_create_credentials(path, dataset)


def test_apply_payload_round_trip_and_rejects_incomplete_payloads(
    dataset: QaDataset, tmp_path: Path
) -> None:
    credentials = load_or_create_credentials(tmp_path / ".secrets" / "qa.json", dataset)
    payload = build_apply_payload(credentials)

    assert parse_apply_payload(payload, dataset) == credentials
    document = json.loads(payload)
    assert set(document["accounts"]["qa.owner"]) == {"password", "totp_secret"}
    broken_payloads = []
    missing = json.loads(payload)
    del missing["accounts"]["qa.owner"]
    broken_payloads.append(missing)
    weak = json.loads(payload)
    weak["accounts"]["qa.owner"]["password"] = "short"
    broken_payloads.append(weak)
    extra = json.loads(payload)
    extra["accounts"]["intruder"] = extra["accounts"]["qa.owner"]
    broken_payloads.append(extra)
    bad_totp = json.loads(payload)
    bad_totp["accounts"]["qa.owner"]["totp_secret"] = "not-base32"
    broken_payloads.append(bad_totp)
    for broken in broken_payloads:
        with pytest.raises(SeedGuardError):
            parse_apply_payload(json.dumps(broken), dataset)


def test_seed_layers_keep_documented_extension_order() -> None:
    assert [name for name, _ in SEED_LAYERS] == [
        "access",
        "root_settings",
        "strategy_releases",
        "bots",
        "bot_configs",
        "market",
    ]


def test_bots_a_and_b_have_published_configs_with_distinct_strategy_releases(
    dataset: QaDataset,
) -> None:
    configs = {config.account_key: config for config in dataset.bot_configs}
    registry = StrategyRegistry()

    # Ruling tích hợp: fan-out cần config PUBLISHED; QA bot A/B chạy release khác nhau.
    assert sorted(configs) == ["A", "B"]
    assert configs["A"].release_id != configs["B"].release_id
    assert {configs["A"].release_id, configs["B"].release_id} == {"h01-v1", "h03-v1"}
    qa_settings = dataset.settings_for(QA_WORKSPACE_KEY)
    for config in configs.values():
        overlay = BotConfigOverlay.model_validate(config.overlay_json)
        assert canonical_overlay(overlay) == config.overlay_json
        assert overlay_hash(config.overlay_json) == config.overlay_hash
        assert overlay.strategy is not None
        assert overlay.strategy.release_id == config.release_id
        assert config.workspace_key == QA_WORKSPACE_KEY
        assert dataset.account(config.account_key).status == "ENABLED"
        assert config.effective_at == qa_settings.effective_at
        assert Role.STRATEGY_ADMIN in dataset.member(config.created_by_member_key).roles
        assert Role.OWNER in dataset.member(config.approved_by_member_key).roles
    released = {
        (release.workspace_key, release.name): release for release in dataset.strategy_releases
    }
    for workspace_key in (QA_WORKSPACE_KEY, QA_OTHER_WORKSPACE_KEY):
        for name in ("h01-v1", "h03-v1"):
            release = released[(workspace_key, name)]
            assert release.status == "PUBLISHED"
            assert release.version == 1
            assert release.content_hash == registry.get_release(name).content_hash


def test_bot_config_publication_keeps_baseline_and_restores_drift() -> None:
    now = MARKET_END + timedelta(days=30)
    baseline = "a" * 64
    head = type("Head", (), {"id": qa_id("bot_config_version", "head"), "overlay_hash": baseline})

    # Lần đầu: publish với hiệu lực từ SEEDED_AT (dữ liệu replay lịch sử resolve được).
    assert bot_config_publication(None, baseline, effective_at=MARKET_END, now=now) == (
        None,
        MARKET_END,
    )
    # Head đã đúng baseline: không ghi gì (idempotent).
    assert bot_config_publication(head, baseline, effective_at=MARKET_END, now=now) is None
    # Head lệch baseline (publish thủ công): version mới nối sau head, hiệu lực từ now.
    drifted = type("Head", (), {"id": head.id, "overlay_hash": "b" * 64})
    assert bot_config_publication(drifted, baseline, effective_at=MARKET_END, now=now) == (
        head.id,
        now,
    )


def test_cli_refuses_without_local_only_before_docker_or_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_run(*_: object, **__: object) -> CompletedProcess[str]:
        raise AssertionError("Không được gọi Docker khi thiếu --local-only")

    monkeypatch.setattr(seed_qa_data.subprocess, "run", forbidden_run)
    credentials = tmp_path / ".secrets" / "qa_accounts.json"

    assert seed_qa_data.main(["seed", "--credentials-file", str(credentials)]) == 2
    assert seed_qa_data.main(["apply"]) == 2
    assert not credentials.exists()
    assert "--local-only" in capsys.readouterr().err


def test_seed_orchestrator_passes_secrets_only_via_stdin_and_never_prints_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    summary = {"dataset_version": "x", "counts": {"workspace": 2}, "changes": {"created": 1}}

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["docker", "context", "inspect"]:
            return CompletedProcess(command, 0, '"npipe:////./pipe/dockerDesktopLinuxEngine"\n', "")
        if command[:2] == ["git", "-C"]:
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 0, "log line\n" + json.dumps(summary) + "\n", "")

    monkeypatch.setattr(seed_qa_data.subprocess, "run", fake_run)
    monkeypatch.setattr(seed_qa_data, "repository_root", lambda: tmp_path)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    credentials_path = tmp_path / ".secrets" / "qa_accounts.json"

    exit_code = seed_qa_data.main(
        ["seed", "--local-only", "--project-name", "crypidian-qa-test", "--json"]
    )

    output = capsys.readouterr()
    secrets_document = json.loads(credentials_path.read_text(encoding="utf-8"))
    secret_values = [
        value
        for entry in secrets_document["accounts"].values()
        for value in (entry["password"], entry["totp_secret"])
    ]
    compose_command, compose_options = calls[-1]
    assert exit_code == 0
    assert compose_command[:2] == ["docker", "compose"]
    assert "qa-seed" in compose_command
    assert "--local-only" in compose_command
    assert "crypidian-qa-test" in compose_command
    assert json.loads(str(compose_options["input"]))["accounts"].keys() == (
        secrets_document["accounts"].keys()
    )
    for value in secret_values:
        assert value not in " ".join(compose_command)
        assert value not in json.dumps(compose_options.get("env") or {})
        assert value not in output.out
        assert value not in output.err
    assert json.loads(output.out.strip().splitlines()[-1]) == summary


def test_superseded_mfa_files_are_released_only_when_no_row_references_them() -> None:
    first, second = UUID(int=1), UUID(int=2)
    superseded = [
        # Factor lạ trỏ cùng tệp với factor thật: tệp còn sống, không được xóa.
        (first, "local-mfa:00000000-0000-0000-0000-00000000000a"),
        (first, "local-mfa:00000000-0000-0000-0000-00000000000b"),
        (second, "local-mfa:00000000-0000-0000-0000-00000000000b"),
        (first, "local-mfa:00000000-0000-0000-0000-00000000000c"),
    ]
    live = {"local-mfa:00000000-0000-0000-0000-00000000000a"}

    assert releasable_references(superseded, live) == [
        (first, "local-mfa:00000000-0000-0000-0000-00000000000b"),
        (first, "local-mfa:00000000-0000-0000-0000-00000000000c"),
    ]


def test_redact_secrets_masks_every_known_value() -> None:
    assert redact_secrets("lỗi abcSECRET và xyzTOTP", ["abcSECRET", "xyzTOTP", ""]) == (
        "lỗi *** và ***"
    )
