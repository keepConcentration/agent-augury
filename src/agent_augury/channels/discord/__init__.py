"""Discord channel adapters (bot, mirror, observe, inbound)."""

from .bot import BotManager, DiscordBotAdapter, DiscordBotError
from .inbound import attach_discord_inbound, dispatch_discord_inbound
from .mirror import DiscordWebhookMirror, mirror_from_config
from .observe import attach_discord_bots, attach_discord_mirror

__all__ = [
    "BotManager",
    "DiscordBotAdapter",
    "DiscordBotError",
    "DiscordWebhookMirror",
    "attach_discord_bots",
    "attach_discord_inbound",
    "attach_discord_mirror",
    "dispatch_discord_inbound",
    "mirror_from_config",
]
