"""Channel adapters: Discord/Slack observation (+ optional inbound HITL)."""

from .base import ChannelAdapter
from .discord.inbound import attach_discord_inbound, dispatch_discord_inbound
from .discord.mirror import DiscordWebhookMirror, mirror_from_config
from .discord.observe import attach_discord_bots, attach_discord_mirror
from .slack.mirror import SlackWebhookMirror, slack_from_config
from .slack.observe import attach_slack_mirror

__all__ = [
    "ChannelAdapter",
    "DiscordWebhookMirror",
    "SlackWebhookMirror",
    "attach_discord_bots",
    "attach_discord_inbound",
    "attach_discord_mirror",
    "attach_slack_mirror",
    "dispatch_discord_inbound",
    "mirror_from_config",
    "slack_from_config",
]
