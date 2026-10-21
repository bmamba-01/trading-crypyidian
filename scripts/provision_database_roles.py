import os
from pathlib import Path

import psycopg
from psycopg import sql

PROCESS_ROLES = (
    ("crypidian_admin_web", "/run/secrets/database_admin_web_password"),
    ("crypidian_trading_runtime", "/run/secrets/database_trading_runtime_password"),
    ("crypidian_execution_gateway", "/run/secrets/database_execution_gateway_password"),
    ("crypidian_ingestion_worker", "/run/secrets/database_ingestion_worker_password"),
    ("crypidian_trainer_worker", "/run/secrets/database_trainer_worker_password"),
)


def main() -> None:
    """Tạo hoặc xoay mật khẩu login riêng cho từng process local."""
    admin_password_path = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
    connection = psycopg.connect(
        host=os.environ["TRADING_BOT_DATABASE_HOST"],
        dbname=os.environ["TRADING_BOT_DATABASE_NAME"],
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=admin_password_path.read_text(encoding="utf-8").strip(),
        autocommit=True,
    )
    with connection:
        for role_name, password_path in PROCESS_ROLES:
            password = Path(password_path).read_text(encoding="utf-8").strip()
            if not password:
                raise ValueError(f"Mật khẩu database local rỗng cho role {role_name}")
            role_exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
            ).fetchone()
            action = sql.SQL("ALTER ROLE" if role_exists else "CREATE ROLE")
            statement = sql.SQL(
                "{} {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            ).format(action, sql.Identifier(role_name), sql.Literal(password))
            connection.execute(statement)
    connection.close()
    print("Đã đồng bộ login database riêng cho từng process local.")


if __name__ == "__main__":
    main()
