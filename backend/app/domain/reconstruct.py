"""Deal -> trade reconstruction. Pure functions, no I/O, no ORM.

MetaTrader does not store "trades". It stores **deals**: atomic fills. One trade in a
trader's head -- long EURUSD, scaled in twice, took half off at 1R, trailed the rest --
is five deals, and on a netting account it can look like three unrelated trades unless
it is reconstructed properly.

Get this right and every statistic downstream is just SQL. Get it wrong and every
number in the product is a lie, confidently displayed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from itertools import pairwise

ZERO = Decimal("0")

#: Bumped whenever the algorithm changes. Stored on every trade so a rebuild can be
#: diffed against the previous version before it is accepted.
RECONSTRUCTION_VERSION = 1


class DealType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BALANCE = "balance"
    CREDIT = "credit"
    CHARGE = "charge"
    CORRECTION = "correction"
    BONUS = "bonus"
    COMMISSION = "commission"
    INTEREST = "interest"


class DealEntry(StrEnum):
    IN = "in"
    OUT = "out"
    INOUT = "inout"
    OUT_BY = "out_by"


#: Deals that move the account balance without being a trade. They must be excluded
#: from trade statistics but included in the equity curve -- otherwise a deposit shows
#: up as a winning trade.
NON_TRADE_TYPES = frozenset(
    {
        DealType.BALANCE, DealType.CREDIT, DealType.CHARGE,
        DealType.CORRECTION, DealType.BONUS, DealType.COMMISSION, DealType.INTEREST,
    }
)


@dataclass(frozen=True, slots=True)
class DealFact:
    """One broker deal, exactly as reported."""

    id: int
    deal_ticket: int
    position_id: int | None
    time_msc: int
    type: str
    entry: str
    symbol: str
    volume: Decimal
    price: Decimal
    sl: Decimal | None = None
    tp: Decimal | None = None
    commission: Decimal = ZERO
    swap: Decimal = ZERO
    profit: Decimal = ZERO
    fee: Decimal = ZERO
    digits: int = 5
    reason: str | None = None

    @property
    def is_trade_deal(self) -> bool:
        return self.type in (DealType.BUY, DealType.SELL)

    @property
    def signed_volume(self) -> Decimal:
        """Positive for a buy, negative for a sell."""
        return self.volume if self.type == DealType.BUY else -self.volume


@dataclass(slots=True)
class Leg:
    raw_deal_id: int
    deal_ticket: int
    leg_type: str          # entry | exit
    volume: Decimal
    price: Decimal
    time_msc: int


@dataclass(slots=True)
class ReconstructedTrade:
    trade_key: str
    symbol: str
    direction: str                     # long | short
    status: str                        # open | closed
    opened_at: datetime
    closed_at: datetime | None = None
    volume_opened: Decimal = ZERO
    volume_closed: Decimal = ZERO
    avg_entry_price: Decimal | None = None
    avg_exit_price: Decimal | None = None
    initial_sl: Decimal | None = None
    initial_tp: Decimal | None = None
    final_sl: Decimal | None = None
    gross_profit: Decimal = ZERO
    commission: Decimal = ZERO
    swap: Decimal = ZERO
    fee: Decimal = ZERO
    net_profit: Decimal = ZERO
    risk_amount: Decimal | None = None
    r_multiple: Decimal | None = None
    pips: Decimal | None = None
    duration_seconds: int | None = None
    exit_reason: str | None = None
    digits: int = 5
    legs: list[Leg] = field(default_factory=list)


# ── helpers ─────────────────────────────────────────────────────────────────────
def _vwap(items: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Volume-weighted average price.

    Always volume weighted: a plain average of prices is wrong the moment the trader
    scales in unevenly, which is exactly when it matters.
    """
    total_volume = sum((volume for _, volume in items), ZERO)
    if total_volume == ZERO:
        return None
    weighted = sum((price * volume for price, volume in items), ZERO)
    return (weighted / total_volume).quantize(Decimal("0.00001"), ROUND_HALF_UP)


def _to_dt(time_msc: int) -> datetime:
    return datetime.fromtimestamp(time_msc / 1000, UTC)


def _pip_size(symbol: str, digits: int) -> Decimal:
    """One pip in price terms. Derived from the terminal's digit count, not guessed."""
    if digits >= 4:
        return Decimal("0.0001") if digits in (4, 5) else Decimal(1).scaleb(-digits)
    if digits in (2, 3):
        return Decimal("0.01")
    return Decimal(1).scaleb(-digits) if digits > 0 else Decimal("1")


def _exit_reason(deals: list[DealFact]) -> str | None:
    """The broker tells us why it closed. Trust that over inferring from prices."""
    for deal in reversed(deals):
        if deal.entry in (DealEntry.OUT, DealEntry.OUT_BY, DealEntry.INOUT):
            reason = (deal.reason or "").lower()
            if reason in ("sl", "tp", "so"):
                return reason
            return "manual"
    return None


def _finalize(trade: ReconstructedTrade) -> ReconstructedTrade:
    """Derive everything that follows from the legs already attached."""
    entries = [(leg.price, leg.volume) for leg in trade.legs if leg.leg_type == "entry"]
    exits = [(leg.price, leg.volume) for leg in trade.legs if leg.leg_type == "exit"]

    trade.volume_opened = sum((volume for _, volume in entries), ZERO)
    trade.volume_closed = sum((volume for _, volume in exits), ZERO)
    trade.avg_entry_price = _vwap(entries)
    trade.avg_exit_price = _vwap(exits) if exits else None

    trade.net_profit = trade.gross_profit + trade.commission + trade.swap + trade.fee

    if trade.closed_at is not None:
        trade.duration_seconds = int(
            (trade.closed_at - trade.opened_at).total_seconds()
        )

    _derive_pips(trade)
    _derive_r_multiple(trade)
    return trade


def _derive_pips(trade: ReconstructedTrade) -> None:
    if trade.avg_entry_price is None or trade.avg_exit_price is None:
        return
    move = trade.avg_exit_price - trade.avg_entry_price
    if trade.direction == "short":
        move = -move
    pip = _pip_size(trade.symbol, trade.digits)
    if pip == ZERO:
        return
    trade.pips = (move / pip).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _derive_r_multiple(trade: ReconstructedTrade) -> None:
    """R multiple, calibrated from the broker's own profit figure.

    The money value of one price unit differs by symbol, account currency and contract
    size, and we deliberately do not keep a table of those. Instead it is derived from
    this very trade: the broker told us the profit for a known price move on a known
    volume, so value-per-price-unit falls straight out. That makes R exact for every
    instrument without maintaining reference data that would go stale.

    Left as None -- never zero -- when there was no stop, the trade is still open, or
    the price did not move enough to calibrate against.
    """
    if (
        trade.initial_sl is None
        or trade.avg_entry_price is None
        or trade.avg_exit_price is None
        or trade.volume_closed == ZERO
        or trade.status != "closed"
    ):
        return

    stop_distance = abs(trade.avg_entry_price - trade.initial_sl)
    if stop_distance == ZERO:
        return

    price_move = abs(trade.avg_exit_price - trade.avg_entry_price)
    if price_move == ZERO or trade.gross_profit == ZERO:
        return

    value_per_price_unit = abs(trade.gross_profit) / (price_move * trade.volume_closed)
    risk = stop_distance * trade.volume_opened * value_per_price_unit
    if risk <= ZERO:
        return

    trade.risk_amount = risk.quantize(Decimal("0.01"), ROUND_HALF_UP)
    trade.r_multiple = (trade.net_profit / risk).quantize(Decimal("0.001"), ROUND_HALF_UP)


def _new_trade(deal: DealFact, trade_key: str, direction: str) -> ReconstructedTrade:
    return ReconstructedTrade(
        trade_key=trade_key,
        symbol=deal.symbol,
        direction=direction,
        status="open",
        opened_at=_to_dt(deal.time_msc),
        # The FIRST entry defines the risk baseline. A later stop move must not
        # retroactively change what the trade risked.
        initial_sl=deal.sl if deal.sl and deal.sl != ZERO else None,
        initial_tp=deal.tp if deal.tp and deal.tp != ZERO else None,
        final_sl=deal.sl if deal.sl and deal.sl != ZERO else None,
        digits=deal.digits,
    )


def _apply_costs(trade: ReconstructedTrade, deal: DealFact, share: Decimal = Decimal("1")) -> None:
    """Attribute the broker's own figures. Never recompute profit from prices:
    spread, swap and rounding would make our number disagree with the statement, and
    the trader will believe the statement."""
    trade.gross_profit += deal.profit * share
    trade.commission += deal.commission * share
    trade.swap += deal.swap * share
    trade.fee += deal.fee * share


# ── hedging accounts ────────────────────────────────────────────────────────────
def reconstruct_hedging(deals: list[DealFact]) -> list[ReconstructedTrade]:
    """Every deal carries a position_id, so grouping is done for us."""
    groups: dict[int, list[DealFact]] = {}
    for deal in deals:
        if not deal.is_trade_deal or deal.position_id is None:
            continue
        groups.setdefault(deal.position_id, []).append(deal)

    trades: list[ReconstructedTrade] = []
    for position_id, group in groups.items():
        group.sort(key=lambda d: (d.time_msc, d.deal_ticket))
        entries = [d for d in group if d.entry == DealEntry.IN]
        if not entries:
            continue    # exits with no recorded entry: nothing trustworthy to build

        first = entries[0]
        trade = _new_trade(
            first, f"h:{position_id}", "long" if first.type == DealType.BUY else "short"
        )

        for deal in group:
            leg_type = "entry" if deal.entry == DealEntry.IN else "exit"
            trade.legs.append(
                Leg(deal.id, deal.deal_ticket, leg_type, deal.volume, deal.price, deal.time_msc)
            )
            _apply_costs(trade, deal)
            if deal.sl and deal.sl != ZERO:
                trade.final_sl = deal.sl

        opened = sum((d.volume for d in group if d.entry == DealEntry.IN), ZERO)
        closed = sum(
            (d.volume for d in group if d.entry in (DealEntry.OUT, DealEntry.OUT_BY)), ZERO
        )
        if closed >= opened and closed > ZERO:
            trade.status = "closed"
            trade.closed_at = _to_dt(group[-1].time_msc)
            trade.exit_reason = _exit_reason(group)

        trades.append(_finalize(trade))

    return sorted(trades, key=lambda t: t.opened_at)


# ── netting accounts ────────────────────────────────────────────────────────────
def reconstruct_netting(deals: list[DealFact]) -> list[ReconstructedTrade]:
    """One position per symbol, so trades are found with a running-net state machine.

    The trap is DEAL_ENTRY_INOUT: a single deal that both closes the existing position
    and opens a new one in the opposite direction. Treated as one event it either loses
    a trade or welds two unrelated ones together.
    """
    by_symbol: dict[str, list[DealFact]] = {}
    for deal in deals:
        if not deal.is_trade_deal:
            continue
        by_symbol.setdefault(deal.symbol, []).append(deal)

    trades: list[ReconstructedTrade] = []
    for symbol, symbol_deals in by_symbol.items():
        symbol_deals.sort(key=lambda d: (d.time_msc, d.deal_ticket))
        trades.extend(_walk_netting(symbol, symbol_deals))
    return sorted(trades, key=lambda t: t.opened_at)


def _walk_netting(symbol: str, deals: list[DealFact]) -> list[ReconstructedTrade]:
    out: list[ReconstructedTrade] = []
    net = ZERO
    current: ReconstructedTrade | None = None

    for deal in deals:
        signed = deal.signed_volume
        direction = "long" if deal.type == DealType.BUY else "short"

        if net == ZERO or current is None:
            current = _new_trade(deal, f"n:{symbol}:{deal.deal_ticket}", direction)
            current.legs.append(
                Leg(deal.id, deal.deal_ticket, "entry", deal.volume, deal.price, deal.time_msc)
            )
            _apply_costs(current, deal)

        elif (net > ZERO) == (signed > ZERO):
            # Scaling into the existing position.
            current.legs.append(
                Leg(deal.id, deal.deal_ticket, "entry", deal.volume, deal.price, deal.time_msc)
            )
            _apply_costs(current, deal)
            if deal.sl and deal.sl != ZERO:
                current.final_sl = deal.sl

        else:
            # Reducing, closing, or reversing.
            closing = min(abs(net), abs(signed))
            current.legs.append(
                Leg(deal.id, deal.deal_ticket, "exit", closing, deal.price, deal.time_msc)
            )

            if abs(signed) > abs(net):
                # A reversal. The realised profit belongs entirely to the part that
                # closed; the remainder opens a fresh trade at the same instant.
                _apply_costs(current, deal, share=Decimal("1"))
                current.status = "closed"
                current.closed_at = _to_dt(deal.time_msc)
                current.exit_reason = "manual"
                out.append(_finalize(current))

                remainder = abs(signed) - abs(net)
                current = _new_trade(deal, f"n:{symbol}:{deal.deal_ticket}:r", direction)
                current.legs.append(
                    Leg(deal.id, deal.deal_ticket, "entry", remainder, deal.price, deal.time_msc)
                )
            else:
                share = closing / abs(signed) if signed != ZERO else Decimal("1")
                _apply_costs(current, deal, share=share)

        net += signed

        if net == ZERO and current is not None:
            current.status = "closed"
            current.closed_at = _to_dt(deal.time_msc)
            current.exit_reason = _exit_reason([deal])
            out.append(_finalize(current))
            current = None

    if current is not None:
        out.append(_finalize(current))
    return out


# ── entry point ─────────────────────────────────────────────────────────────────
def reconstruct(deals: list[DealFact], margin_mode: str) -> list[ReconstructedTrade]:
    """Rebuild every trade in this account from its deals."""
    if margin_mode == "netting":
        return reconstruct_netting(deals)
    return reconstruct_hedging(deals)


def detect_margin_mode(deals: list[DealFact]) -> str | None:
    """Work out from the history whether this account hedges or nets.

    Nobody should have to answer this question about their own broker, and a trader
    who guesses wrong corrupts every statistic on the site without anything looking
    broken. The deals already know.

    Two things are proof:

      a reversal          a deal that closes one position and opens the opposite one
                          in a single fill. Only a netting account can produce it.
      two at once         two positions open on the same symbol at the same time.
                          Only a hedging account can hold them.

    Returns None when the history shows neither, which is the ordinary case and does
    not matter: with no reversals and no overlaps the two algorithms group the deals
    identically, so either answer reconstructs the same trades. `test_reconstruct`
    holds that claim to account.
    """
    spans: dict[int, tuple[str, int, int]] = {}

    for deal in deals:
        if deal.type in NON_TRADE_TYPES:
            continue
        if deal.entry == DealEntry.INOUT:
            return "netting"
        if deal.position_id is None:
            continue
        symbol, start, end = spans.get(
            deal.position_id, (deal.symbol, deal.time_msc, deal.time_msc)
        )
        spans[deal.position_id] = (symbol, min(start, deal.time_msc), max(end, deal.time_msc))

    by_symbol: dict[str, list[tuple[int, int]]] = {}
    for symbol, start, end in spans.values():
        by_symbol.setdefault(symbol, []).append((start, end))

    for windows in by_symbol.values():
        windows.sort()
        for (_, first_end), (second_start, _) in pairwise(windows):
            # Strict: two positions that merely touch -- one closing as the next opens
            # -- are not proof of anything, and being wrong here is worse than being
            # silent.
            if second_start < first_end:
                return "hedging"

    return None


def balance_movements(deals: list[DealFact]) -> list[DealFact]:
    """Deposits, withdrawals and adjustments.

    Excluded from trade statistics, but they belong on the equity curve -- otherwise a
    deposit looks like a winning trade.
    """
    return [deal for deal in deals if deal.type in NON_TRADE_TYPES]
