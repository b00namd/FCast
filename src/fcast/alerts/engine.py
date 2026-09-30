"""Turns signals and collector events into notifications, with cooldown and quiet hours.

Nothing is queued: a signal suppressed by quiet hours or cooldown is simply evaluated again
after the next collector run and sent then if it still holds.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from fcast.alerts.config import AlertConfig, load_alert_config
from fcast.alerts.notifier import (
    LogNotifier,
    Notification,
    Notifier,
    NotifierError,
    NtfyNotifier,
    Priority,
)
from fcast.analysis import signals as sig
from fcast.analysis.service import current_signals
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.session import session_scope
from fcast.sources import futbin

logger = logging.getLogger(__name__)

SOURCE_PAUSED = "SOURCE_PAUSED"
COLLECT_FAILED = "COLLECT_FAILED"


class Outcome(StrEnum):
    SENT = "sent"
    COOLDOWN = "cooldown"
    QUIET = "quiet"
    FILTERED = "filtered"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(frozen=True)
class Candidate:
    rule: str  # alerts_log.rule, e.g. "BUY_DIP" or "SOURCE_PAUSED:futbin"
    ea_id: int | None
    notification: Notification
    expected_profit: int | None = None
    kind: str = "signal"  # "signal" or "system"


@dataclass
class AlertReport:
    decisions: list[tuple[Candidate, Outcome]] = field(default_factory=list)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for _, o in self.decisions if o is outcome)


def build_notifier(settings: Settings) -> Notifier:
    if settings.ntfy_url:
        token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        return NtfyNotifier(settings.ntfy_url, settings.ntfy_topic, token)
    return LogNotifier()


def _coins(value: int | None) -> str:
    return "-" if value is None else f"{value:,}".replace(",", ".")


_STYLE = {
    sig.Rule.BUY_DIP: (Priority.HIGH, "chart_with_downwards_trend"),
    sig.Rule.OVERPRICE_CHANCE: (Priority.HIGH, "moneybag"),
    sig.Rule.SELL_TARGET: (Priority.DEFAULT, "dart"),
    sig.Rule.PROMO_PREBUY: (Priority.HIGH, "crystal_ball"),
    sig.Rule.HOLO_SPREAD: (Priority.HIGH, "sparkles"),
}


def signal_notification(
    signal: sig.Signal, dashboard_url: str | None, futbin_ref: str | None
) -> Notification:
    lines = [f"Preis {_coins(signal.price)} (Ø 7 Tage {_coins(signal.reference)})"]
    if signal.rule is sig.Rule.BUY_DIP:
        lines.append(f"Kaufen bis max. {_coins(signal.recommended)}")
    elif signal.rule is sig.Rule.PROMO_PREBUY:
        lines.append("Vor dem Promo-Start kaufen, nach dem Start teurer verkaufen")
    elif signal.rule is sig.Rule.HOLO_SPREAD:
        lines.append(
            f"Normale Karte knapp unter Holo-Preis einstellen: {_coins(signal.recommended)}"
        )
    else:
        lines.append(f"Einstellen zu {_coins(signal.recommended)}")
    if signal.expected_profit is not None:
        lines.append(f"Erwarteter Profit nach Steuer: {_coins(signal.expected_profit)}")
    if signal.score is not None:
        lines.append(f"Score {signal.score:.0f}/100")
    if signal.reasons:
        lines.append(" · ".join(signal.reasons))

    priority, tag = _STYLE[signal.rule]
    actions: list[tuple[str, str]] = []
    click = None
    if dashboard_url:
        click = f"{dashboard_url.rstrip('/')}/players/{signal.ea_id}"
        actions.append(("Dashboard", click))
    if futbin_ref:
        actions.append(("FUTBIN", futbin.BASE_URL + futbin_ref))
    return Notification(
        title=f"{signal.label}: {signal.name}",
        message="\n".join(lines),
        priority=priority,
        tags=(tag,),
        click_url=click,
        actions=tuple(actions),
    )


class AlertEngine:
    def __init__(self, notifier: Notifier, settings: Settings) -> None:
        self.notifier = notifier
        self.settings = settings

    def _status_url(self) -> str | None:
        base = self.settings.dashboard_url
        return f"{base.rstrip('/')}/status" if base else None

    def system_candidates(
        self, paused: dict[str, str], failed_players: int | None
    ) -> list[Candidate]:
        found = []
        status = self._status_url()
        actions = (("Status", status),) if status else ()
        for source, reason in paused.items():
            found.append(
                Candidate(
                    rule=f"{SOURCE_PAUSED}:{source}"[:32],
                    ea_id=None,
                    kind="system",
                    notification=Notification(
                        title=f"Quelle pausiert: {source}",
                        message=(
                            f"{source} hat Anfragen abgelehnt ({reason}). FCast fragt dort "
                            f"{self.settings.source_pause_h} h nicht mehr an; andere Quellen "
                            "springen ein."
                        ),
                        priority=Priority.DEFAULT,
                        tags=("warning",),
                        click_url=status,
                        actions=actions,
                    ),
                )
            )
        if failed_players:
            found.append(
                Candidate(
                    rule=COLLECT_FAILED,
                    ea_id=None,
                    kind="system",
                    notification=Notification(
                        title="Sammellauf ohne Preise",
                        message=(
                            f"Für keine der {failed_players} Karten kam ein Preis. "
                            "Quellen gesperrt oder offline? Details auf der Statusseite."
                        ),
                        priority=Priority.HIGH,
                        tags=("rotating_light",),
                        click_url=status,
                        actions=actions,
                    ),
                )
            )
        return found

    def signal_candidates(self, session: Session, now: datetime) -> list[Candidate]:
        found = []
        for signal in current_signals(session, self.settings, now):
            ref = repo.get_source_ref(session, signal.ea_id, futbin.SOURCE_NAME)
            found.append(
                Candidate(
                    rule=signal.rule.value,
                    ea_id=signal.ea_id,
                    expected_profit=signal.expected_profit,
                    notification=signal_notification(signal, self.settings.dashboard_url, ref),
                )
            )
        return found

    def decide(
        self, session: Session, candidate: Candidate, config: AlertConfig, now: datetime
    ) -> Outcome:
        if not config.enabled or not self._rule_enabled(candidate, config):
            return Outcome.DISABLED
        if (
            candidate.kind == "signal"
            and config.min_profit
            and candidate.expected_profit is not None
            and candidate.expected_profit < config.min_profit
        ):
            return Outcome.FILTERED
        # Quiet hours apply to everything; a signal that still holds is sent afterwards.
        if config.is_quiet(now, self.settings.tz):
            return Outcome.QUIET
        player = repo.get_player_by_ea_id(session, candidate.ea_id) if candidate.ea_id else None
        last = repo.last_alert(session, candidate.rule, player)
        if last is not None and now - last.sent_at < timedelta(hours=config.cooldown_h):
            return Outcome.COOLDOWN
        return Outcome.SENT

    @staticmethod
    def _rule_enabled(candidate: Candidate, config: AlertConfig) -> bool:
        if candidate.kind == "system":
            return config.system
        return {
            sig.Rule.BUY_DIP.value: config.buy_dip,
            sig.Rule.SELL_TARGET.value: config.sell_target,
            sig.Rule.OVERPRICE_CHANCE.value: config.overprice,
            sig.Rule.PROMO_PREBUY.value: config.promo,
            sig.Rule.HOLO_SPREAD.value: config.holo,
        }.get(candidate.rule, True)

    def evaluate(
        self,
        session: Session,
        now: datetime,
        paused: dict[str, str] | None = None,
        failed_players: int | None = None,
    ) -> AlertReport:
        """Decide for every candidate without sending anything (dry run)."""
        config = load_alert_config(session, self.settings)
        candidates = self.system_candidates(paused or {}, failed_players)
        candidates += self.signal_candidates(session, now)
        report = AlertReport()
        for candidate in candidates:
            report.decisions.append((candidate, self.decide(session, candidate, config, now)))
        return report

    async def process(
        self,
        factory: sessionmaker[Session],
        now: datetime,
        paused: dict[str, str] | None = None,
        failed_players: int | None = None,
    ) -> AlertReport:
        """Evaluate, send what is due and log every delivered alert."""
        with session_scope(factory) as session:
            report = self.evaluate(session, now, paused, failed_players)

        final = AlertReport()
        for candidate, outcome in report.decisions:
            if outcome is not Outcome.SENT:
                final.decisions.append((candidate, outcome))
                continue
            try:
                await self.notifier.send(candidate.notification)
            except NotifierError as exc:
                logger.warning("alert %s not delivered: %s", candidate.rule, exc)
                final.decisions.append((candidate, Outcome.FAILED))
                continue
            with session_scope(factory) as session:
                player = (
                    repo.get_player_by_ea_id(session, candidate.ea_id) if candidate.ea_id else None
                )
                message = f"{candidate.notification.title}\n{candidate.notification.message}"
                repo.log_alert(session, candidate.rule, message, player, sent_at=now)
            final.decisions.append((candidate, Outcome.SENT))
        if final.count(Outcome.SENT) or final.count(Outcome.FAILED):
            logger.info(
                "alerts: %d sent, %d failed, %d cooldown, %d quiet",
                final.count(Outcome.SENT),
                final.count(Outcome.FAILED),
                final.count(Outcome.COOLDOWN),
                final.count(Outcome.QUIET),
            )
        return final

    async def aclose(self) -> None:
        await self.notifier.aclose()
