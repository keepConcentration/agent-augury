"""Channel adapters: Discord/Slack observation (+ optional inbound HITL).

M4: Discord webhook mirror and bots attach as Gateway observe surfaces.
M5: ``bots[].inbound: true`` enables Discord → Wire ``human.*`` (opt-in).
M6: Slack Incoming Webhook observe (``slack.url_env``).
"""

from .base import ChannelAdapter
from .discord_inbound import attach_discord_inbound, dispatch_discord_inbound
from .discord_mirror import DiscordWebhookMirror, mirror_from_config
from .discord_observe import attach_discord_bots, attach_discord_mirror
from .slack_mirror import SlackWebhookMirror, slack_from_config
from .slack_observe import attach_slack_mirror

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
