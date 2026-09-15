"""Slack channel adapters (observe webhook)."""

from .mirror import SlackWebhookMirror, slack_from_config
from .observe import attach_slack_mirror

__all__ = [
    "SlackWebhookMirror",
    "attach_slack_mirror",
    "slack_from_config",
]
