"""Alert configuration: environment defaults, overridable from the dashboard (database)."""

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, time, tzinfo

from sqlalchemy.orm import Session

from fcast.config import Settings
from fcast.db import repositories as repo

PREFIX = "alerts."


def parse_clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = True
    cooldown_h: float = 6.0
    quiet_start: str = "00:00"  # local time, HH:MM
    quiet_end: str = "07:00"
    min_profit: int = 0  # only for signals that carry an expected profit
    buy_dip: bool = True
    sell_target: bool = True
    overprice: bool = True
    system: bool = True  # source paused, collection failed
    totw: bool = True  # TOTW candidates before the weekly release

    @classmethod
    def defaults(cls, settings: Settings) -> "AlertConfig":
        return cls(
            cooldown_h=settings.alert_cooldown_h,
            quiet_start=settings.quiet_hours_start,
            quiet_end=settings.quiet_hours_end,
            min_profit=settings.alert_min_profit,
        )

    def is_quiet(self, now: datetime, tz: tzinfo) -> bool:
        """True inside the quiet window; the window may span midnight (e.g. 23:00-07:00)."""
        start, end = parse_clock(self.quiet_start), parse_clock(self.quiet_end)
        if start == end:
            return False
        local = now.astimezone(tz).time()
        if start < end:
            return start <= local < end
        return local >= start or local < end


def _convert(template: object, raw: str) -> object:
    if isinstance(template, bool):
        return raw == "true"
    if isinstance(template, int):
        return int(raw)
    if isinstance(template, float):
        return float(raw)
    return raw


def load_alert_config(session: Session, settings: Settings) -> AlertConfig:
    config = AlertConfig.defaults(settings)
    stored = repo.get_app_settings(session, PREFIX)
    overrides: dict[str, object] = {}
    for item in fields(AlertConfig):
        raw = stored.get(PREFIX + item.name)
        if raw is not None:
            try:
                overrides[item.name] = _convert(getattr(config, item.name), raw)
            except ValueError:
                continue  # ignore a broken value, keep the default
    return replace(config, **overrides)  # type: ignore[arg-type]


def save_alert_config(session: Session, config: AlertConfig) -> None:
    for key, value in asdict(config).items():
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        repo.set_app_setting(session, PREFIX + key, text)
