"""Compose E2E so vector runtime/replay qua hai role PostgreSQL thật."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from scripts.docker_image import ensure_image_available

BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ['WP06_PARITY_STATE']=json.dumps(p['state']);"
    "exec(compile(p['code'],'wp06_learning_feature_vector_parity.py','exec'),"
    "{'__name__':'__main__'})"
)


@dataclass(frozen=True)
class ComposeParityResources:
    """Định danh và file cấu hình chỉ thuộc một lượt chạy C.1.1."""

    run_id: str
    project: str
    image: str
    compose_file: Path


@contextmanager
def isolated_compose_resources(root: Path) -> Iterator[ComposeParityResources]:
    """Tạo secrets riêng; mọi đường dẫn khác vẫn resolve theo repository gốc."""
    run_id = uuid.uuid4().hex
    with TemporaryDirectory(prefix=f"crypidian-c11-{run_id}-") as directory:
        run_directory = Path(directory)
        subprocess.run(
            [sys.executable, str(root / "scripts/prepare_local_secrets.py")],
            cwd=run_directory,
            capture_output=True,
            check=True,
        )
        compose_file = run_directory / "compose.yaml"
        compose_file.write_text(
            (root / "compose.yaml")
            .read_text(encoding="utf-8")
            .replace("./.secrets/", f"{run_directory / '.secrets'}/"),
            encoding="utf-8",
        )
        yield ComposeParityResources(
            run_id=run_id,
            project=f"crypidian-wp06-vector-parity-e2e-{run_id}",
            image=f"crypidian-app:e2e-c11-{run_id}",
            compose_file=compose_file,
        )


def test_postgres_runtime_replay_learning_feature_vector_exact_parity() -> None:
    root = Path(__file__).parents[2]
    garage_config = root / ".secrets/garage.env"
    garage_before = garage_config.read_bytes() if garage_config.exists() else None
    try:
        with isolated_compose_resources(root) as resources:
            _run_parity_compose(root, resources)
    finally:
        garage_after = garage_config.read_bytes() if garage_config.exists() else None
        # Không đưa nội dung cấu hình/secret vào failure output.
        garage_preserved = garage_before == garage_after
        assert garage_preserved, "Cấu hình Garage của người dùng đã bị đổi."
    assert not resources.compose_file.parent.exists()
    print(
        json.dumps(
            {
                "garage_config_existed_before": garage_before is not None,
                "garage_config_preserved": True,
                "isolated_secrets_removed": True,
            },
            sort_keys=True,
        )
    )


def _run_parity_compose(root: Path, resources: ComposeParityResources) -> None:
    probe = (root / "tests/e2e/probes/wp06_learning_feature_vector_parity.py").read_text()
    project = resources.project
    image = resources.image
    assert image == f"crypidian-app:e2e-c11-{resources.run_id}"
    state = {name: str(uuid.uuid4()) for name in ("workspace_id", "instrument_id", "raw_id")}
    state["now"] = datetime.now(UTC).isoformat()
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID": state["workspace_id"],
        "TRADING_BOT_BACKTEST_WORKER_WORKSPACE_ID": state["workspace_id"],
    }
    base = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(root),
        "-f",
        str(resources.compose_file),
    ]

    def compose(*args: str, stdin: str | None = None) -> str:
        result = subprocess.run(
            [*base, *args],
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert result.returncode == 0, (args, result.stdout[-3000:], result.stderr[-6000:])
        return result.stdout

    def run_probe(mode: str, service: str) -> dict[str, Any]:
        output = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            service,
            "python",
            "-c",
            BOOTSTRAP,
            stdin=json.dumps({"state": state | {"mode": mode}, "code": probe}),
        )
        return cast(dict[str, Any], json.loads(output.strip().splitlines()[-1]))

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        assert "No new upgrade operations detected" in compose(
            "run", "--rm", "--no-deps", "migrator", "alembic", "check"
        )
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert heads.count("(head)") == 1
        state.update(run_probe("seed", "migrator"))
        runtime = run_probe("runtime", "market-signal-worker")
        replay = run_probe("replay", "backtest-worker")
        assert runtime.pop("database_role") == "crypidian_market_signal_worker"
        assert replay.pop("database_role") == "crypidian_backtest_worker"
        assert runtime == replay
        assert runtime["bar_counts"] == {"1h": 224, "4h": 206}
        assert runtime["feature_keys"] == [
            "1h:ema_20",
            "1h:ema_80",
            "1h:adx_14",
            "1h:atr_14",
            "4h:ema_200",
        ]
        assert runtime["revision_2_exact"] is True
        assert runtime["raw_reference_exact"] is True
        assert runtime["no_vector_rights"] is True
        assert runtime["tampering"] == dict.fromkeys(
            ("raw_reference", "source_identity", "feature_value"), "SNAPSHOT_CONTENT_MISMATCH"
        )
        print(json.dumps({"project": project, "parity": True, **runtime}, sort_keys=True))
    finally:
        compose("down", "--volumes", "--remove-orphans")
        for resource in ("container", "volume", "network"):
            command = [
                "docker",
                resource,
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
            if resource == "container":
                command.append("--all")
            inventory = subprocess.run(command, capture_output=True, text=True, check=True)
            assert not inventory.stdout.strip(), (resource, inventory.stdout)
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image], capture_output=True, check=True, timeout=120
            )
        print(
            json.dumps(
                {
                    "cleanup_project": project,
                    "containers": 0,
                    "volumes": 0,
                    "networks": 0,
                    "app_image_removed": image_created,
                    "app_image_owned_by_run": image.endswith(resources.run_id),
                    "app_image": image,
                },
                sort_keys=True,
            )
        )
