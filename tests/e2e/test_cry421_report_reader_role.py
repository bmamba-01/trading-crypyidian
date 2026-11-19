"""Compose E2E CRY-421: kiểm chứng least-privilege và RLS của role crypidian_report_reader."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

_PROBE = r"""
import asyncio, json, os
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret

async def main():
    admin_engine = create_database_engine(
        database_url_from_secret(
            username="crypidian_admin",
            password_file=Path("/run/secrets/postgres_password"),
            host="postgres",
            database="crypidian",
        )
    )
    reader_engine = create_database_engine(
        database_url_from_secret(
            username="crypidian_report_reader",
            password_file=Path("/run/secrets/database_report_reader_password"),
            host="postgres",
            database="crypidian",
        )
    )
    ws_a = uuid4()
    ws_b = uuid4()
    report_a = uuid4()
    report_b = uuid4()
    session_a = uuid4()
    try:
        async with admin_engine.begin() as conn:
            role_row = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolinherit, rolbypassrls, rolcanlogin "
                        "FROM pg_roles WHERE rolname = 'crypidian_report_reader'"
                    )
                )
            ).one()
            memberships = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_auth_members m "
                        "JOIN pg_roles r ON r.oid = m.member "
                        "WHERE r.rolname = 'crypidian_report_reader'"
                    )
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO public.workspace (id, slug, name, mode, status) "
                    "VALUES (:a, :sa, 'WS A', 'PAPER', 'ACTIVE'), "
                    "(:b, :sb, 'WS B', 'PAPER', 'ACTIVE')"
                ),
                {"a": ws_a, "sa": f"ws-a-{ws_a.hex[:8]}", "b": ws_b, "sb": f"ws-b-{ws_b.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO public.run_report "
                    "(id, workspace_id, report_hash, report_version, run_label, "
                    "window_start, window_end, bot_count, payload) "
                    "VALUES (:ra, :wa, :ha, 'ai1-run-report-v2', 'lbl-a', "
                    "now() - interval '1 hour', now(), 0, CAST(:pa AS jsonb)), "
                    "(:rb, :wb, :hb, 'ai1-run-report-v2', 'lbl-b', "
                    "now() - interval '1 hour', now(), 0, CAST(:pb AS jsonb))"
                ),
                {
                    "ra": report_a, "wa": ws_a, "ha": "a" * 64,
                    "pa": json.dumps({"report_hash": "a" * 64}),
                    "rb": report_b, "wb": ws_b, "hb": "b" * 64,
                    "pb": json.dumps({"report_hash": "b" * 64}),
                },
            )

        out = {
            "role_flags_ok": (role_row == (False, False, False, True)),
            "no_group_membership": (memberships == 0),
        }

        async with reader_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"),
                    {"ws": str(ws_a)},
                )
                # 1. SELECT trong workspace thấy đúng 1 hàng (không thấy ws_b nhờ RLS)
                rows = (
                    await conn.execute(text("SELECT id FROM public.run_report"))
                ).scalars().all()
                out["rls_select_isolated"] = (rows == [report_a])

                # 2. INSERT vào analyst_session thuộc ws_a thành công
                await conn.execute(
                    text(
                        "INSERT INTO public.analyst_session "
                        "(id, workspace_id, topic_key, engine, session_id, status, run_count) "
                        "VALUES (:id, :ws, 'family:H03', 'claude', 'sess-1', 'ACTIVE', 1)"
                    ),
                    {"id": session_a, "ws": ws_a},
                )
                out["insert_allowed_table_ok"] = True

                # 3. INSERT sang ws_b bị RLS chặn
                try:
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO public.analyst_session "
                                "(id, workspace_id, topic_key, engine, session_id, status, "
                                "run_count) VALUES (:id, :ws, 'family:H03', 'claude', 'sess-b', "
                                "'ACTIVE', 1)"
                            ),
                            {"id": uuid4(), "ws": ws_b},
                        )
                    out["cross_ws_insert_denied"] = False
                except Exception:
                    out["cross_ws_insert_denied"] = True

                # 4. UPDATE / DELETE trên analyst_session bị từ chối
                for name, stmt in (
                    ("update_denied", "UPDATE public.analyst_session SET status = 'RETIRED'"),
                    ("delete_denied", "DELETE FROM public.analyst_session"),
                    ("user_account_select_denied", "SELECT count(*) FROM public.user_account"),
                    ("user_session_select_denied", "SELECT count(*) FROM public.user_session"),
                    ("audit_event_select_denied", "SELECT count(*) FROM public.audit_event"),
                ):
                    try:
                        async with conn.begin_nested():
                            await conn.execute(text(stmt))
                        out[name] = False
                    except Exception:
                        out[name] = True

        print(json.dumps(out, sort_keys=True))
    finally:
        await reader_engine.dispose()
        await admin_engine.dispose()

asyncio.run(main())
"""


def test_cry421_report_reader_role_least_privilege_and_rls() -> None:
    """Kiểm chứng PostgreSQL thật cho role crypidian_report_reader."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-cry421-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def compose(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode != 0:
            raise AssertionError(f"Compose {args[:6]!r} thất bại:\n{result.stderr[-4000:]}")
        return result

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        secret_path = root / ".secrets" / "database_report_reader_password"
        run = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--volume",
            f"{secret_path}:/run/secrets/database_report_reader_password:ro",
            "migrator",
            "python",
            "-c",
            _PROBE,
        )
        result = json.loads(run.stdout.strip().splitlines()[-1])
        assert result == {
            "audit_event_select_denied": True,
            "cross_ws_insert_denied": True,
            "delete_denied": True,
            "insert_allowed_table_ok": True,
            "no_group_membership": True,
            "rls_select_isolated": True,
            "role_flags_ok": True,
            "update_denied": True,
            "user_account_select_denied": True,
            "user_session_select_denied": True,
        }
    finally:
        subprocess.run(
            base + ["down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=120,
        )
