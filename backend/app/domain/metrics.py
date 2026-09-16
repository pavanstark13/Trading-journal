"""Trading statistics. Pure functions over closed trades, no I/O.

Two rules run through all of this:

  Report the sample size. "Win rate 62%" is not honest; "62% (n=13)" is.
  Never invent a number. A metric that cannot be computed returns None, not zero --
  a fabricated zero is indistinguishable from a real one on a chart.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")

#: Below this, a statistic is noise dressed as insight. The API flags it so the UI can
#: grey it out rather than letting someone re-plan their trading around 6 trades.
MIN_MEANINGFUL_SAMPLE = 20

#: A result this small either way is a scratch, not a win or a loss. Counting it as a
#: win inflates win rate; counting it as a loss inflates the loss count.
SCRATCH_THRESHOLD = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """The minimum a trade needs to expose for statistics."""

    trade_key: str
    symbol: str
    direction: str
    opened_at: datetime
    closed_at: datetime | None
    net_profit: Decimal
    r_multiple: Decimal | None
    duration_seconds: int | None
    trade_date: date | None = None
    hour_of_day: int | None = None
    day_of_week: int | None = None
    session: str | None = None
    setup: str | None = None
    followed_plan: bool | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Summary:
    trades: int
    wins: int
    losses: int
    scratches: int
    win_rate: Decimal | None
    net_profit: Decimal
    gross_win: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    #: None when no trade carried a stop. Expectancy in R is the number that predicts
    #: survival; win rate on its own does not.
    expectancy_r: Decimal | None
    avg_r: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal | None
    longest_win_streak: int
    longest_loss_streak: int
    trades_without_stop: int
    avg_duration_seconds: int | None
    sqn: Decimal | None
    #: True when the sample is too small for any of this to mean much.
    low_confidence: bool = field(default=False)


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), ROUND_HALF_UP)


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def _stddev(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    assert mean is not None
    variance = sum(((v - mean) ** 2 for v in values), ZERO) / Decimal(len(values) - 1)
    return Decimal(str(math.sqrt(float(variance))))


def classify(trade: TradeRecord) -> str:
    """win | loss | scratch. Scratches are reported separately, never hidden."""
    if abs(trade.net_profit) < SCRATCH_THRESHOLD:
        return "scratch"
    return "win" if trade.net_profit > ZERO else "loss"


def max_drawdown(
    trades: Iterable[TradeRecord], starting_balance: Decimal = ZERO
) -> tuple[Decimal, Decimal | None]:
    """Peak-to-trough of the closed-trade equity curve.

    Returns (absolute, percent-of-peak).

    The percentage is measured against **account equity**, which is why the starting
    balance matters: a 460 drawdown on a 10,000 account is 4.6%, but measured against
    cumulative profit alone it can read as 46% -- a number that would tell a trader
    they nearly blew up when they did not. Without a known starting balance the
    percentage is None rather than that misleading figure.
    """
    equity = starting_balance
    peak = starting_balance
    worst = ZERO
    worst_pct: Decimal | None = None

    for trade in sorted(trades, key=lambda t: t.closed_at or t.opened_at):
        equity += trade.net_profit
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst:
            worst = drop
            if starting_balance > ZERO and peak > ZERO:
                worst_pct = _q(drop / peak * Decimal(100))
    return _q(worst), worst_pct


def streaks(trades: Iterable[TradeRecord]) -> tuple[int, int]:
    """Longest consecutive win and loss runs. Scratches do not break a streak."""
    best_win = best_loss = current_win = current_loss = 0
    for trade in sorted(trades, key=lambda t: t.closed_at or t.opened_at):
        outcome = classify(trade)
        if outcome == "win":
            current_win += 1
            current_loss = 0
        elif outcome == "loss":
            current_loss += 1
            current_win = 0
        best_win = max(best_win, current_win)
        best_loss = max(best_loss, current_loss)
    return best_win, best_loss


def summarize(
    trades: list[TradeRecord], starting_balance: Decimal = ZERO
) -> Summary:
    """The headline numbers for a set of closed trades.

    `starting_balance` is only used to express drawdown as a percentage of the
    account; every other figure is independent of it.
    """
    total = len(trades)
    if total == 0:
        return Summary(
            trades=0, wins=0, losses=0, scratches=0, win_rate=None, net_profit=ZERO,
            gross_win=ZERO, gross_loss=ZERO, profit_factor=None, expectancy_r=None,
            avg_r=None, avg_win=None, avg_loss=None, largest_win=None,
            largest_loss=None, max_drawdown=ZERO, max_drawdown_pct=None,
            longest_win_streak=0, longest_loss_streak=0, trades_without_stop=0,
            avg_duration_seconds=None, sqn=None, low_confidence=True,
        )

    wins = [t for t in trades if classify(t) == "win"]
    losses = [t for t in trades if classify(t) == "loss"]
    scratches = [t for t in trades if classify(t) == "scratch"]

    gross_win = sum((t.net_profit for t in wins), ZERO)
    gross_loss = sum((t.net_profit for t in losses), ZERO)     # negative
    net = sum((t.net_profit for t in trades), ZERO)

    decided = len(wins) + len(losses)
    win_rate = (
        _q(Decimal(len(wins)) / Decimal(decided) * Decimal(100)) if decided else None
    )

    # Profit factor is undefined with no losses. Reporting it as infinity on a
    # three-trade sample is how people talk themselves into oversizing.
    profit_factor = (
        _q(gross_win / abs(gross_loss)) if gross_loss != ZERO else None
    )

    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r = _q(_mean(r_values), "0.001") if r_values else None

    # Expectancy from the R distribution of trades that actually carried a stop.
    expectancy_r: Decimal | None = None
    if r_values:
        r_wins = [r for r in r_values if r > ZERO]
        r_losses = [r for r in r_values if r <= ZERO]
        if r_wins or r_losses:
            n = Decimal(len(r_values))
            win_share = Decimal(len(r_wins)) / n
            loss_share = Decimal(len(r_losses)) / n
            avg_win_r = _mean(r_wins) or ZERO
            avg_loss_r = _mean(r_losses) or ZERO
            expectancy_r = _q(win_share * avg_win_r + loss_share * avg_loss_r, "0.001")

    # Van Tharp SQN. Meaningless under ~30 trades, so the caller gets low_confidence.
    sqn: Decimal | None = None
    if len(r_values) >= 2:
        deviation = _stddev(r_values)
        mean_r = _mean(r_values)
        if deviation and deviation != ZERO and mean_r is not None:
            sqn = _q(Decimal(str(math.sqrt(len(r_values)))) * mean_r / deviation, "0.01")

    durations = [t.duration_seconds for t in trades if t.duration_seconds is not None]
    drawdown, drawdown_pct = max_drawdown(trades, starting_balance)
    win_streak, loss_streak = streaks(trades)

    return Summary(
        trades=total,
        wins=len(wins),
        losses=len(losses),
        scratches=len(scratches),
        win_rate=win_rate,
        net_profit=_q(net),
        gross_win=_q(gross_win),
        gross_loss=_q(gross_loss),
        profit_factor=profit_factor,
        expectancy_r=expectancy_r,
        avg_r=avg_r,
        avg_win=_q(_mean([t.net_profit for t in wins]) or ZERO) if wins else None,
        avg_loss=_q(_mean([t.net_profit for t in losses]) or ZERO) if losses else None,
        largest_win=_q(max((t.net_profit for t in wins), default=ZERO)) if wins else None,
        largest_loss=_q(min((t.net_profit for t in losses), default=ZERO)) if losses else None,
        max_drawdown=drawdown,
        max_drawdown_pct=drawdown_pct,
        longest_win_streak=win_streak,
        longest_loss_streak=loss_streak,
        trades_without_stop=sum(1 for t in trades if t.r_multiple is None),
        avg_duration_seconds=int(sum(durations) / len(durations)) if durations else None,
        sqn=sqn,
        low_confidence=total < MIN_MEANINGFUL_SAMPLE,
    )


@dataclass(frozen=True, slots=True)
class Bucket:
    key: str
    trades: int
    net_profit: Decimal
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    low_confidence: bool


def _bucket(key: str, trades: list[TradeRecord]) -> Bucket:
    summary = summarize(trades)
    return Bucket(
        key=key,
        trades=summary.trades,
        net_profit=summary.net_profit,
        win_rate=summary.win_rate,
        expectancy_r=summary.expectancy_r,
        low_confidence=summary.low_confidence,
    )


def group_by(trades: list[TradeRecord], dimension: str) -> list[Bucket]:
    """Break performance down by symbol, hour, weekday, session, setup or tag.

    This is where an edge is actually found: most traders make all their money on two
    instruments and give it back on the other nine, and almost everyone has a dead hour.
    """
    groups: dict[str, list[TradeRecord]] = defaultdict(list)

    for trade in trades:
        if dimension == "symbol":
            groups[trade.symbol].append(trade)
        elif dimension == "hour":
            if trade.hour_of_day is not None:
                groups[f"{trade.hour_of_day:02d}"].append(trade)
        elif dimension == "weekday":
            if trade.day_of_week is not None:
                groups[str(trade.day_of_week)].append(trade)
        elif dimension == "session":
            groups[trade.session or "unknown"].append(trade)
        elif dimension == "setup":
            groups[trade.setup or "untagged"].append(trade)
        elif dimension == "direction":
            groups[trade.direction].append(trade)
        elif dimension == "tag":
            for tag in trade.tags or ("untagged",):
                groups[tag].append(trade)
        else:
            raise ValueError(f"Unknown dimension: {dimension}")

    return sorted(
        (_bucket(key, items) for key, items in groups.items()),
        key=lambda b: b.net_profit,
        reverse=True,
    )


def equity_curve(
    trades: list[TradeRecord], starting_balance: Decimal = ZERO
) -> list[dict[str, object]]:
    """Cumulative closed-trade equity, one point per trade."""
    equity = starting_balance
    peak = starting_balance
    points: list[dict[str, object]] = []
    for trade in sorted(trades, key=lambda t: t.closed_at or t.opened_at):
        equity += trade.net_profit
        peak = max(peak, equity)
        points.append(
            {
                "at": (trade.closed_at or trade.opened_at).isoformat(),
                "trade_key": trade.trade_key,
                "equity": str(_q(equity)),
                "drawdown": str(_q(peak - equity)),
                "net_profit": str(_q(trade.net_profit)),
            }
        )
    return points


def daily_pnl(trades: list[TradeRecord]) -> list[dict[str, object]]:
    """Per-day totals, for the calendar heatmap."""
    days: dict[date, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        day = trade.trade_date or (trade.closed_at or trade.opened_at).date()
        days[day].append(trade)

    out = []
    for day in sorted(days):
        items = days[day]
        out.append(
            {
                "date": day.isoformat(),
                "trades": len(items),
                "net_profit": str(_q(sum((t.net_profit for t in items), ZERO))),
                "wins": sum(1 for t in items if classify(t) == "win"),
                "losses": sum(1 for t in items if classify(t) == "loss"),
            }
        )
    return out


def performance_after_a_loss(trades: list[TradeRecord]) -> dict[str, object]:
    """Revenge trading, quantified.

    Compares the trade immediately following a loss against everything else. When the
    expectancy gap is large, that is a discipline problem with a number attached.
    """
    ordered = sorted(trades, key=lambda t: t.closed_at or t.opened_at)
    after_loss: list[TradeRecord] = []
    rest: list[TradeRecord] = []

    previous_was_loss = False
    for trade in ordered:
        (after_loss if previous_was_loss else rest).append(trade)
        previous_was_loss = classify(trade) == "loss"

    return {
        "after_loss": asdict(_bucket("after_loss", after_loss)),
        "otherwise": asdict(_bucket("otherwise", rest)),
    }


def plan_adherence(trades: list[TradeRecord]) -> dict[str, object]:
    """Followed-the-plan versus did not.

    The killer statistic: it converts "discipline" from a feeling into a money figure.
    """
    followed = [t for t in trades if t.followed_plan is True]
    deviated = [t for t in trades if t.followed_plan is False]
    return {
        "followed": asdict(_bucket("followed", followed)),
        "deviated": asdict(_bucket("deviated", deviated)),
        "unjournalled": sum(1 for t in trades if t.followed_plan is None),
    }
