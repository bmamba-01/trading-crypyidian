"""Identity tối thiểu cho các fixture repository kế thừa từ trước MB-01."""

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from trading_bot.adapters.database.models import BotInstance


class BotScopeSession:
    @property
    def info(self):
        if not hasattr(self, "_bot_info"):
            self._bot_info = {}
        return self._bot_info

    def is_bot_query(self, statement):
        descriptions = getattr(statement, "column_descriptions", ())
        return bool(descriptions and descriptions[0].get("entity") is BotInstance)

    def bot_identity(self, statement):
        values = statement.compile().params
        account = values.get("trading_account_id_1")
        bot_id = self.info.get("bot_id") if self.info.get("bot_account_id") == account else None
        return SimpleNamespace(id=bot_id or uuid5(NAMESPACE_URL, str(account)))

    async def execute(self, _statement, _params=None):
        return None
