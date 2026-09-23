"""Strategy registry. Add new strategies here so config.yaml can select them."""
from .base import Signal, Strategy
from .ema_trend_pullback import EmaTrendPullback

REGISTRY: dict[str, type[Strategy]] = {
    EmaTrendPullback.name: EmaTrendPullback,
}


def load_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        return REGISTRY[name](**(params or {}))
    except KeyError:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}") from None


__all__ = ["Signal", "Strategy", "REGISTRY", "load_strategy"]
