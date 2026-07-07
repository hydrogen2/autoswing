"""Configuration loading and the paper/live safety interlock."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PAPER_PORT = 4002
LIVE_PORT = 4001

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class LiveTradingRefused(Exception):
    """Raised when a live-port connection is attempted without both interlocks."""


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    client_id: int
    live_trading: bool
    connect_timeout_s: int


@dataclass(frozen=True)
class Config:
    broker: BrokerConfig
    journal_dir: Path
    risk: dict
    path: Path


def load_config(path: Path | None = None) -> Config:
    path = Path(path or os.environ.get("AUTOSWING_CONFIG", DEFAULT_CONFIG_PATH))
    with open(path) as f:
        raw = yaml.safe_load(f)

    b = raw["broker"]
    broker = BrokerConfig(
        host=b.get("host", "127.0.0.1"),
        port=int(b.get("port", PAPER_PORT)),
        client_id=int(b.get("client_id", 1)),
        live_trading=bool(b.get("live_trading", False)),
        connect_timeout_s=int(b.get("connect_timeout_s", 15)),
    )
    enforce_paper_interlock(broker)

    journal_dir = PROJECT_ROOT / raw.get("journal", {}).get("dir", "journal")
    return Config(broker=broker, journal_dir=journal_dir, risk=raw.get("risk", {}), path=path)


def enforce_paper_interlock(broker: BrokerConfig) -> None:
    """Live trading requires BOTH the config flag and AUTOSWING_LIVE=1.

    Anything that is not provably an intentional live setup must be paper.
    """
    env_live = os.environ.get("AUTOSWING_LIVE") == "1"
    if broker.port == LIVE_PORT or broker.live_trading or env_live:
        if not (broker.port == LIVE_PORT and broker.live_trading and env_live):
            raise LiveTradingRefused(
                "Refusing ambiguous live-trading setup: connecting to the live port "
                f"requires all three of port={LIVE_PORT}, live_trading=true in config, "
                "and AUTOSWING_LIVE=1 in the environment. "
                f"Got port={broker.port}, live_trading={broker.live_trading}, "
                f"AUTOSWING_LIVE={'1' if env_live else 'unset'}."
            )
