"""Configuration YAML validée par un schéma strict (§63) et garde LIVE (§71)."""

from okxq.config.loader import load_config
from okxq.config.modes import Mode
from okxq.config.schema import AppConfig

__all__ = ["AppConfig", "Mode", "load_config"]
