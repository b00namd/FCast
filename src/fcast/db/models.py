"""ORM models. Prices are whole coins, all timestamps are UTC."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fcast.config import Platform
from fcast.db.base import Base, UTCDateTime, str_enum, utcnow


class PositionStatus(StrEnum):
    HOLDING = "holding"
    LISTED = "listed"
    SOLD = "sold"


class LinkType(StrEnum):
    PLAYER = "player"
    LEAGUE = "league"
    NATION = "nation"
    CLUB = "club"


class Player(Base):
    """A single card. Only `ea_id` is required; details are filled in by price sources."""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    ea_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str | None] = mapped_column(String(100))
    rating: Mapped[int | None]
    position: Mapped[str | None] = mapped_column(String(8))
    card_type: Mapped[str | None] = mapped_column(String(50))
    league: Mapped[str | None] = mapped_column(String(100))
    nation: Mapped[str | None] = mapped_column(String(100))
    club: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    watchlist_entry: Mapped["WatchlistEntry | None"] = relationship(back_populates="player")

    @property
    def display_name(self) -> str:
        if self.name is None:
            return f"#{self.ea_id}"
        return f"{self.name} ({self.rating})" if self.rating is not None else self.name


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index("ix_price_snapshots_player_id_captured_at", "player_id", "captured_at"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    price: Mapped[int]
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WatchlistEntry(Base):
    __tablename__ = "watchlist"
    __table_args__ = (
        CheckConstraint("target_buy IS NULL OR target_buy > 0", name="target_buy_positive"),
        CheckConstraint("target_sell IS NULL OR target_sell > 0", name="target_sell_positive"),
    )

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    target_buy: Mapped[int | None]
    target_sell: Mapped[int | None]
    note: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(default=True)

    player: Mapped[Player] = relationship(back_populates="watchlist_entry")


class PortfolioPosition(Base):
    __tablename__ = "portfolio"
    __table_args__ = (
        CheckConstraint("buy_price > 0", name="buy_price_positive"),
        CheckConstraint("sell_price IS NULL OR sell_price > 0", name="sell_price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"))
    buy_price: Mapped[int]
    bought_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Listing price while `listed`, final price once `sold`.
    sell_price: Mapped[int | None]
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[PositionStatus] = mapped_column(
        str_enum(PositionStatus), default=PositionStatus.HOLDING
    )

    player: Mapped[Player] = relationship()


class Promo(Base):
    __tablename__ = "promos"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(default=0.5)
    note: Mapped[str | None] = mapped_column(String(500))

    links: Mapped[list["PromoLink"]] = relationship(
        back_populates="promo", cascade="all, delete-orphan", passive_deletes=True
    )


class PromoLink(Base):
    __tablename__ = "promo_links"

    promo_id: Mapped[int] = mapped_column(
        ForeignKey("promos.id", ondelete="CASCADE"), primary_key=True
    )
    link_type: Mapped[LinkType] = mapped_column(str_enum(LinkType), primary_key=True)
    link_value: Mapped[str] = mapped_column(String(100), primary_key=True)

    promo: Mapped[Promo] = relationship(back_populates="links")


class AlertLog(Base):
    __tablename__ = "alerts_log"
    __table_args__ = (
        Index("ix_alerts_log_rule_player_id_sent_at", "rule", "player_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule: Mapped[str] = mapped_column(String(32))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"))
    message: Mapped[str]
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
