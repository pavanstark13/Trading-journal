"""Lot sizing. Pure functions, Decimal throughout, no I/O.

Every calculation returns the inputs it used so the result can be audited later --
"why did this member get 0.07 lots" must always be answerable from the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum

ZERO = Decimal("0")


class SizingMode(StrEnum):
    FIXED_LOT = "FIXED_LOT"
    LOT_MULTIPLIER = "LOT_MULTIPLIER"
    BALANCE_PROPORTIONAL = "BALANCE_PROPORTIONAL"
    EQUITY_PROPORTIONAL = "EQUITY_PROPORTIONAL"
    RISK_PERCENT = "RISK_PERCENT"
    FIXED_MONEY_RISK = "FIXED_MONEY_RISK"


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Broker contract specification for one symbol on the member's account."""

    symbol: str
    volume_min: Decimal = Decimal("0.01")
    volume_max: Decimal = Decimal("100")
    volume_step: Decimal = Decimal("0.01")
    #: Account-currency value of a one-point move on one lot. Required for the
    #: risk-based modes; None means those modes cannot be used for this symbol.
    tick_value: Decimal | None = None
    tick_size: Decimal | None = None
    digits: int = 5


@dataclass(frozen=True, slots=True)
class SizingInputs:
    mode: SizingMode
    master_volume: Decimal
    master_balance: Decimal
    master_equity: Decimal
    member_balance: Decimal
    member_equity: Decimal
    spec: SymbolSpec
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    fixed_lot: Decimal | None = None
    copy_multiplier: Decimal = Decimal("1")
    risk_percent: Decimal | None = None
    fixed_money_risk: Decimal | None = None


class SizingError(ValueError):
    """Raised when a sizing mode is configured but cannot be evaluated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SizingResult:
    calculated_lot: Decimal
    final_lot: Decimal
    mode: SizingMode
    detail: dict[str, str] = field(default_factory=dict)
    #: True when normalization to the broker's step/min/max changed the value.
    adjusted: bool = False
    adjustment_reason: str | None = None


def normalize_lot(lot: Decimal, spec: SymbolSpec) -> tuple[Decimal, str | None]:
    """Snap a lot to the broker's step, then clamp to [volume_min, volume_max].

    Rounds DOWN to the step: never size a member up because of rounding.
    """
    if spec.volume_step <= ZERO:
        raise SizingError("BAD_SYMBOL_SPEC", f"volume_step must be > 0 for {spec.symbol}")

    steps = (lot / spec.volume_step).to_integral_value(rounding=ROUND_DOWN)
    snapped = (steps * spec.volume_step).quantize(spec.volume_step)

    reason: str | None = None
    if snapped != lot:
        reason = "ROUNDED_TO_STEP"
    if snapped < spec.volume_min:
        # Below the broker minimum we do NOT silently round up to the minimum:
        # that would size a member above their configured risk. The caller decides.
        return ZERO, "BELOW_MIN_VOLUME"
    if snapped > spec.volume_max:
        return spec.volume_max, "CAPPED_AT_MAX_VOLUME"
    return snapped, reason


def _risk_based_lot(inputs: SizingInputs, risk_money: Decimal) -> tuple[Decimal, dict[str, str]]:
    """lot = risk_money / (stop_distance_in_points * tick_value)."""
    spec = inputs.spec
    if inputs.entry_price is None or inputs.stop_loss is None or inputs.stop_loss == ZERO:
        raise SizingError(
            "NO_STOP_LOSS",
            "Risk-based sizing requires a stop loss on the master trade",
        )
    if spec.tick_value is None or spec.tick_size is None or spec.tick_size == ZERO:
        raise SizingError(
            "NO_TICK_VALUE",
            f"Risk-based sizing requires tick value/size for {spec.symbol}",
        )

    distance = abs(inputs.entry_price - inputs.stop_loss)
    if distance == ZERO:
        raise SizingError("ZERO_STOP_DISTANCE", "Stop loss equals entry price")

    ticks = distance / spec.tick_size
    loss_per_lot = ticks * spec.tick_value
    if loss_per_lot <= ZERO:
        raise SizingError("ZERO_RISK_PER_LOT", "Computed risk per lot is zero")

    lot = risk_money / loss_per_lot
    return lot, {
        "risk_money": str(risk_money),
        "stop_distance": str(distance),
        "loss_per_lot": str(loss_per_lot.quantize(Decimal("0.01"), ROUND_HALF_UP)),
    }


def calculate_lot(inputs: SizingInputs) -> SizingResult:
    """Compute the member's lot for one master trade."""
    mode = inputs.mode
    detail: dict[str, str] = {
        "mode": str(mode),
        "master_volume": str(inputs.master_volume),
        "master_balance": str(inputs.master_balance),
        "member_balance": str(inputs.member_balance),
    }

    if mode is SizingMode.FIXED_LOT:
        if inputs.fixed_lot is None or inputs.fixed_lot <= ZERO:
            raise SizingError("MISSING_FIXED_LOT", "fixed_lot is not configured")
        raw = inputs.fixed_lot

    elif mode is SizingMode.LOT_MULTIPLIER:
        raw = inputs.master_volume * inputs.copy_multiplier
        detail["copy_multiplier"] = str(inputs.copy_multiplier)

    elif mode is SizingMode.BALANCE_PROPORTIONAL:
        if inputs.master_balance <= ZERO:
            raise SizingError("NO_MASTER_BALANCE", "Master balance is unknown or zero")
        ratio = inputs.member_balance / inputs.master_balance
        raw = inputs.master_volume * ratio * inputs.copy_multiplier
        detail["ratio"] = str(ratio.quantize(Decimal("0.0001"), ROUND_HALF_UP))

    elif mode is SizingMode.EQUITY_PROPORTIONAL:
        if inputs.master_equity <= ZERO:
            raise SizingError("NO_MASTER_EQUITY", "Master equity is unknown or zero")
        ratio = inputs.member_equity / inputs.master_equity
        raw = inputs.master_volume * ratio * inputs.copy_multiplier
        detail["ratio"] = str(ratio.quantize(Decimal("0.0001"), ROUND_HALF_UP))
        detail["member_equity"] = str(inputs.member_equity)

    elif mode is SizingMode.RISK_PERCENT:
        if inputs.risk_percent is None or inputs.risk_percent <= ZERO:
            raise SizingError("MISSING_RISK_PERCENT", "risk_percent is not configured")
        risk_money = inputs.member_balance * inputs.risk_percent / Decimal(100)
        raw, extra = _risk_based_lot(inputs, risk_money)
        detail.update(extra)
        detail["risk_percent"] = str(inputs.risk_percent)

    elif mode is SizingMode.FIXED_MONEY_RISK:
        if inputs.fixed_money_risk is None or inputs.fixed_money_risk <= ZERO:
            raise SizingError("MISSING_FIXED_RISK", "fixed_money_risk is not configured")
        raw, extra = _risk_based_lot(inputs, inputs.fixed_money_risk)
        detail.update(extra)

    else:  # pragma: no cover - StrEnum is exhaustive
        raise SizingError("UNKNOWN_MODE", f"Unsupported sizing mode {mode}")

    calculated = raw.quantize(Decimal("0.00000001"), ROUND_DOWN)
    final, reason = normalize_lot(calculated, inputs.spec)
    detail["calculated_lot"] = str(calculated)
    detail["final_lot"] = str(final)

    return SizingResult(
        calculated_lot=calculated,
        final_lot=final,
        mode=mode,
        detail=detail,
        adjusted=final != calculated,
        adjustment_reason=reason,
    )


def scale_close_volume(
    master_closed: Decimal,
    master_total: Decimal,
    member_open: Decimal,
    spec: SymbolSpec,
) -> Decimal:
    """Volume to close on the member when the master partially closes.

    Proportional to the fraction the master closed, so a member holding 0.10 against a
    master's 0.50 closes 0.05 when the master closes 0.25.
    """
    if master_total <= ZERO or member_open <= ZERO:
        return ZERO
    fraction = master_closed / master_total
    if fraction >= Decimal("0.999"):
        return member_open  # full close: never leave a dust position behind
    target = member_open * fraction
    snapped, _ = normalize_lot(target, spec)
    return min(snapped, member_open)
