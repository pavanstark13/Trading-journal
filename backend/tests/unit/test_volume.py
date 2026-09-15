"""Lot sizing. Every mode, plus the normalization edges that cost real money."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.volume import (
    SizingError,
    SizingInputs,
    SizingMode,
    SymbolSpec,
    calculate_lot,
    normalize_lot,
    scale_close_volume,
)

SPEC = SymbolSpec(
    symbol="EURUSD",
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
    tick_value=Decimal("1"),
    tick_size=Decimal("0.00001"),
)


def _inputs(mode: SizingMode, **kwargs) -> SizingInputs:
    base = {
        "mode": mode,
        "master_volume": Decimal("0.50"),
        "master_balance": Decimal("10000"),
        "master_equity": Decimal("10000"),
        "member_balance": Decimal("2000"),
        "member_equity": Decimal("2000"),
        "spec": SPEC,
        "entry_price": Decimal("1.17250"),
        "stop_loss": Decimal("1.17000"),
    }
    base.update(kwargs)
    return SizingInputs(**base)


def test_balance_proportional_matches_the_brief_example() -> None:
    """Master 10,000 balance with 0.50 lots; member 2,000 balance -> 0.10 lots."""
    result = calculate_lot(_inputs(SizingMode.BALANCE_PROPORTIONAL))
    assert result.final_lot == Decimal("0.10")


def test_equity_proportional_uses_equity_not_balance() -> None:
    result = calculate_lot(
        _inputs(
            SizingMode.EQUITY_PROPORTIONAL,
            member_equity=Decimal("1000"),
            master_equity=Decimal("10000"),
        )
    )
    assert result.final_lot == Decimal("0.05")


def test_fixed_lot_ignores_master_size() -> None:
    result = calculate_lot(_inputs(SizingMode.FIXED_LOT, fixed_lot=Decimal("0.03")))
    assert result.final_lot == Decimal("0.03")


def test_lot_multiplier() -> None:
    result = calculate_lot(
        _inputs(SizingMode.LOT_MULTIPLIER, copy_multiplier=Decimal("0.4"))
    )
    assert result.final_lot == Decimal("0.20")


def test_risk_percent_sizes_from_stop_distance() -> None:
    # 1% of 2000 = 20 risk. Stop is 250 points at 1 per point per lot -> 0.08 lots.
    result = calculate_lot(
        _inputs(SizingMode.RISK_PERCENT, risk_percent=Decimal("1"))
    )
    assert result.final_lot == Decimal("0.08")


def test_fixed_money_risk() -> None:
    result = calculate_lot(
        _inputs(SizingMode.FIXED_MONEY_RISK, fixed_money_risk=Decimal("50"))
    )
    assert result.final_lot == Decimal("0.20")


def test_risk_modes_refuse_without_a_stop_loss() -> None:
    with pytest.raises(SizingError) as exc:
        calculate_lot(
            _inputs(SizingMode.RISK_PERCENT, risk_percent=Decimal("1"), stop_loss=None)
        )
    assert exc.value.code == "NO_STOP_LOSS"


def test_balance_proportional_refuses_unknown_master_balance() -> None:
    with pytest.raises(SizingError) as exc:
        calculate_lot(_inputs(SizingMode.BALANCE_PROPORTIONAL, master_balance=Decimal("0")))
    assert exc.value.code == "NO_MASTER_BALANCE"


def test_normalize_rounds_down_never_up() -> None:
    """Rounding a member UP would put them above their configured risk."""
    snapped, reason = normalize_lot(Decimal("0.179"), SPEC)
    assert snapped == Decimal("0.17")
    assert reason == "ROUNDED_TO_STEP"


def test_below_broker_minimum_returns_zero_not_the_minimum() -> None:
    """Silently bumping to 0.01 would size a tiny account far above its risk budget."""
    snapped, reason = normalize_lot(Decimal("0.004"), SPEC)
    assert snapped == Decimal("0")
    assert reason == "BELOW_MIN_VOLUME"


def test_above_broker_maximum_is_capped() -> None:
    snapped, reason = normalize_lot(Decimal("250"), SPEC)
    assert snapped == Decimal("100")
    assert reason == "CAPPED_AT_MAX_VOLUME"


def test_tiny_member_account_produces_a_rejectable_zero() -> None:
    result = calculate_lot(
        _inputs(SizingMode.BALANCE_PROPORTIONAL, member_balance=Decimal("50"))
    )
    assert result.final_lot == Decimal("0")
    assert result.adjustment_reason == "BELOW_MIN_VOLUME"


def test_non_standard_volume_step_is_respected() -> None:
    spec = SymbolSpec(symbol="XAUUSD", volume_min=Decimal("0.1"), volume_step=Decimal("0.1"))
    snapped, _ = normalize_lot(Decimal("0.37"), spec)
    assert snapped == Decimal("0.3")


def test_sizing_detail_is_auditable() -> None:
    result = calculate_lot(_inputs(SizingMode.BALANCE_PROPORTIONAL))
    assert result.detail["mode"] == "BALANCE_PROPORTIONAL"
    assert result.detail["master_balance"] == "10000"
    assert result.detail["member_balance"] == "2000"
    assert "ratio" in result.detail


def test_partial_close_scales_proportionally() -> None:
    closed = scale_close_volume(
        master_closed=Decimal("0.25"),
        master_total=Decimal("0.50"),
        member_open=Decimal("0.10"),
        spec=SPEC,
    )
    assert closed == Decimal("0.05")


def test_full_close_never_leaves_a_dust_position() -> None:
    closed = scale_close_volume(
        master_closed=Decimal("0.50"),
        master_total=Decimal("0.50"),
        member_open=Decimal("0.03"),
        spec=SPEC,
    )
    assert closed == Decimal("0.03")
