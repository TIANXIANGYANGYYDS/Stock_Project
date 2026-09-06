"""量化模块单个交易日的预选、成交、持仓和结果展示模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Mapping, Sequence

from app.quant.core.execution import money
from app.quant.core.models import BacktestConfig
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    INTRADAY_INTERVAL,
    MINIMUM_SHRINK_RATIO,
    STRATEGY_ID,
    STRATEGY_LABEL,
    STRATEGY_VERSION,
    official_backtest_config,
)


from app.quant.strategies.provisional_daily_macd_3m.config import RECORDING_START_DATE


TradeAction = Literal["buy", "sell"]
CandidateStatus = Literal["watching", "bought", "not_triggered"]
DAILY_RESULTS_COLLECTION = "quant_daily_results"


@dataclass(frozen=True)
class PreselectionItem:
    """盘后选出并在下一交易日进入分钟级监控的一只股票。"""

    code: str
    name: str
    reason: str
    reference_price: float
    status: CandidateStatus = "watching"


@dataclass(frozen=True)
class SellCandidateItem:
    """盘后日线卖出条件成立并在下一交易日进入分钟级监控的持仓。"""

    code: str
    name: str
    reason: str
    reference_price: float


@dataclass(frozen=True)
class SignalExecution:
    """一个在信号确认后按明确行情参考价完成撮合的盘中成交。"""

    event_id: str
    code: str
    name: str
    action: TradeAction
    signal_at: str
    signal_price: float
    execution_at: str
    execution_reference_price: float
    execution_price_source: str
    previous_close: float
    daily_price_limit: float
    execution_bar_low: float | None
    execution_bar_high: float | None
    slippage_rate: float
    execution_price: float
    shares: int
    notional: float
    commission: float
    stamp_duty: float
    reason: str
    status: str = "filled"


@dataclass(frozen=True)
class HoldingItem:
    """买入成交后仍未卖出的持仓。"""

    code: str
    name: str
    shares: int
    entry_event_id: str
    entry_signal_at: str
    entry_signal_price: float
    entry_execution_at: str
    entry_reference_price: float
    entry_execution_price: float
    entry_notional: float
    buy_commission: float
    marked_at: str
    mark_price: float
    previous_close: float | None = None


@dataclass(frozen=True)
class ClosedTrade:
    """同一股票完成买入和卖出后的闭合交易。"""

    code: str
    name: str
    shares: int
    entry_event_id: str
    exit_event_id: str
    entry_signal_at: str
    exit_signal_at: str
    entry_execution_at: str
    exit_execution_at: str
    entry_signal_price: float
    exit_signal_price: float
    entry_reference_price: float
    exit_reference_price: float
    entry_execution_price: float
    exit_execution_price: float
    entry_notional: float
    exit_notional: float
    buy_commission: float
    sell_commission: float
    stamp_duty: float
    gross_pnl: float
    net_pnl: float
    net_return: float


@dataclass(frozen=True)
class IndependentAccount:
    """每股独立资金，现金和累计已实现盈亏跨交易日保留。"""

    code: str
    name: str
    initial_cash: float = 100_000.0
    cash: float = 100_000.0
    realized_pnl: float = 0.0
    first_buy_at: str | None = None

    @property
    def has_traded(self) -> bool:
        return self.first_buy_at is not None


@dataclass(frozen=True)
class DailyFlow:
    """一个交易日内可持续更新、最终供前端查询的完整量化结果。"""

    trade_date: str
    selection_date: str
    generated_at: str
    updated_at: str
    config: BacktestConfig
    preselection: tuple[PreselectionItem, ...]
    sell_candidates: tuple[SellCandidateItem, ...] = ()
    executions: tuple[SignalExecution, ...] = ()
    holdings: tuple[HoldingItem, ...] = ()
    closed_trades: tuple[ClosedTrade, ...] = ()
    monitoring_started_at: str | None = None
    closed_at: str | None = None
    market_closed: bool = False
    accounts: tuple[IndependentAccount, ...] = ()
    opening_total_assets: float | None = None


def create_daily_flow(
    *,
    trade_date: str,
    selection_date: str,
    generated_at: str,
    candidates: Sequence[PreselectionItem],
    sell_candidates: Sequence[SellCandidateItem] = (),
    holdings: Sequence[HoldingItem] = (),
    accounts: Sequence[IndependentAccount] = (),
    opening_total_assets: float | None = None,
) -> DailyFlow:
    """创建盘后预选结果，并带入需要在目标交易日继续监控的持仓。"""

    codes = [candidate.code for candidate in candidates]
    if len(codes) != len(set(codes)):
        raise ValueError("预选池股票代码不能重复")
    if any(
        len(candidate.code) != 6 or not candidate.code.isdigit()
        for candidate in candidates
    ):
        raise ValueError("预选池股票代码必须是六位数字")
    if any(candidate.reference_price <= 0 for candidate in candidates):
        raise ValueError("预选池参考价格必须大于零")
    sell_candidate_codes = [candidate.code for candidate in sell_candidates]
    if len(sell_candidate_codes) != len(set(sell_candidate_codes)):
        raise ValueError("日线卖出候选池股票代码不能重复")
    if any(
        len(candidate.code) != 6 or not candidate.code.isdigit()
        for candidate in sell_candidates
    ):
        raise ValueError("日线卖出候选池股票代码必须是六位数字")
    if any(candidate.reference_price <= 0 for candidate in sell_candidates):
        raise ValueError("日线卖出候选池参考价格必须大于零")
    holding_codes = [holding.code for holding in holdings]
    if len(holding_codes) != len(set(holding_codes)):
        raise ValueError("持有池股票代码不能重复")
    if any(
        len(holding.code) != 6 or not holding.code.isdigit()
        for holding in holdings
    ):
        raise ValueError("持有池股票代码必须是六位数字")
    if any(
        holding.shares <= 0
        or holding.entry_notional <= 0
        or holding.mark_price <= 0
        for holding in holdings
    ):
        raise ValueError("持有池股数、买入金额和标记价格必须大于零")
    account_codes = {item.code for item in accounts}
    if len(account_codes) != len(accounts):
        raise ValueError("独立账户代码不能重复")
    if accounts and any(item.code not in account_codes for item in holdings):
        raise ValueError("持仓缺少对应独立账户")
    if opening_total_assets is None and accounts:
        opening_total_assets = money(sum(item.cash for item in accounts if item.has_traded) + sum(
            money(item.shares * item.mark_price) for item in holdings))
    return DailyFlow(
        trade_date=trade_date,
        selection_date=selection_date,
        generated_at=generated_at,
        updated_at=generated_at,
        config=official_backtest_config(code="000000"),
        preselection=tuple(sorted(candidates, key=lambda item: item.code)),
        sell_candidates=tuple(
            sorted(sell_candidates, key=lambda item: item.code)
        ),
        holdings=tuple(sorted(holdings, key=lambda item: item.code)),
        accounts=tuple(accounts),
        opening_total_assets=opening_total_assets,
    )


def start_daily_flow(flow: DailyFlow, *, started_at: str) -> DailyFlow:
    """标记分钟级监控已经在目标交易日启动。"""

    if not started_at.startswith(flow.trade_date):
        raise ValueError("盘中监控启动时间必须属于当前交易日")
    if flow.market_closed:
        raise ValueError("已收盘的每日结果不能重新启动监控")
    return replace(
        flow,
        updated_at=started_at,
        monitoring_started_at=flow.monitoring_started_at or started_at,
    )


def _buy_size(config: BacktestConfig, execution_price: float) -> tuple[int, float, float]:
    shares = int(
        config.initial_cash
        / (execution_price * (1.0 + config.commission_rate))
    )
    shares = shares // config.lot_size * config.lot_size
    while shares > 0:
        notional = money(execution_price * shares)
        commission = money(notional * config.commission_rate)
        if money(notional + commission) <= config.initial_cash:
            return shares, notional, commission
        shares -= config.lot_size
    raise ValueError("独立账户资金不足以买入一手")


def _price_limit_rate(*, code: str, name: str, trade_date: str) -> Decimal:
    """返回当前回放范围适用的 A 股单日涨跌幅限制。"""

    if code.startswith(("300", "301", "688")):
        return Decimal("0.20")
    if code.startswith(("4", "8", "92")):
        return Decimal("0.30")
    if "ST" in name.upper() and trade_date < "2026-07-06":
        return Decimal("0.05")
    return Decimal("0.10")


def daily_price_limit(
    *,
    action: TradeAction,
    code: str,
    name: str,
    trade_date: str,
    previous_close: float,
) -> float:
    """按股票板块和前收盘价计算涨停价或跌停价。"""

    if action not in ("buy", "sell"):
        raise ValueError("涨跌停方向只能是 buy 或 sell")
    if previous_close <= 0:
        raise ValueError("上一交易日收盘价必须大于零")
    rate = _price_limit_rate(code=code, name=name, trade_date=trade_date)
    multiplier = Decimal("1") + rate if action == "buy" else Decimal("1") - rate
    price = (Decimal(str(previous_close)) * multiplier).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(price)


def at_daily_price_limit(
    *,
    action: TradeAction,
    code: str,
    name: str,
    trade_date: str,
    previous_close: float,
    price: float,
) -> bool:
    """判断撮合参考价是否已经到达当日对应方向的涨跌停价。"""

    limit_price = daily_price_limit(
        action=action,
        code=code,
        name=name,
        trade_date=trade_date,
        previous_close=previous_close,
    )
    rounded_price = float(
        Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    return (
        rounded_price >= limit_price
        if action == "buy"
        else rounded_price <= limit_price
    )


def _execution_price(
    *,
    action: TradeAction,
    reference_price: float,
    bar_low: float | None,
    bar_high: float | None,
    slippage_rate: float,
) -> float:
    """在成交 K 线真实高低价内施加不利滑点。"""

    if reference_price <= 0:
        raise ValueError("成交参考价格必须大于零")
    if (bar_low is None) != (bar_high is None):
        raise ValueError("成交K线最低价和最高价必须同时提供")
    if bar_low is not None and bar_high is not None:
        if bar_low <= 0 or bar_high < bar_low:
            raise ValueError("成交K线价格范围非法")
        if not bar_low <= reference_price <= bar_high:
            raise ValueError("成交参考价格必须位于成交K线高低价范围内")
    slipped = reference_price * (
        1.0 + slippage_rate if action == "buy" else 1.0 - slippage_rate
    )
    if bar_low is None or bar_high is None:
        return slipped
    return min(slipped, bar_high) if action == "buy" else max(slipped, bar_low)


def apply_trade_signal(
    flow: DailyFlow,
    *,
    action: TradeAction,
    code: str,
    signal_at: str,
    signal_price: float,
    previous_close: float,
    reason: str,
    execution_at: str | None = None,
    execution_reference_price: float | None = None,
    execution_bar_low: float | None = None,
    execution_bar_high: float | None = None,
    execution_price_source: str = "signal_price",
) -> DailyFlow:
    """按显式成交行情撮合信号，并同步更新持有池或闭合交易。"""

    if flow.market_closed:
        raise ValueError("收盘后的每日结果不能再写入交易信号")
    if action not in ("buy", "sell"):
        raise ValueError("交易信号只能是 buy 或 sell")
    if signal_price <= 0:
        raise ValueError("信号价格必须大于零")
    if previous_close <= 0:
        raise ValueError("上一交易日收盘价必须大于零")
    actual_execution_at = execution_at or signal_at
    reference_price = execution_reference_price or signal_price
    if not actual_execution_at.startswith(flow.trade_date):
        raise ValueError("成交时间必须属于当前交易日")
    if signal_at > actual_execution_at:
        raise ValueError("成交时间不能早于信号确认时间")
    if not execution_price_source:
        raise ValueError("成交价格来源不能为空")
    execution_price = _execution_price(
        action=action,
        reference_price=reference_price,
        bar_low=execution_bar_low,
        bar_high=execution_bar_high,
        slippage_rate=flow.config.slippage_rate,
    )

    event_id = f"{flow.trade_date}-{len(flow.executions) + 1:04d}"
    if action == "buy":
        candidate = next(
            (item for item in flow.preselection if item.code == code),
            None,
        )
        if candidate is None:
            raise ValueError("买入信号股票不在当日预选池")
        if candidate.status != "watching" or any(
            item.code == code for item in flow.holdings
        ):
            raise ValueError("该股票当日已经买入")
        limit_price = daily_price_limit(
            action="buy",
            code=code,
            name=candidate.name,
            trade_date=flow.trade_date,
            previous_close=previous_close,
        )
        if at_daily_price_limit(
            action="buy",
            code=code,
            name=candidate.name,
            trade_date=flow.trade_date,
            previous_close=previous_close,
            price=reference_price,
        ):
            raise ValueError(f"撮合参考价达到涨停价{limit_price:.2f}，不能买入")

        account = next((item for item in flow.accounts if item.code == code), None)
        if flow.accounts and account is None:
            raise ValueError("买入股票不在固定独立账户池")
        cash_config = replace(flow.config, initial_cash=account.cash) if account else flow.config
        shares, notional, commission = _buy_size(cash_config, execution_price)
        execution = SignalExecution(
            event_id=event_id,
            code=code,
            name=candidate.name,
            action="buy",
            signal_at=signal_at,
            signal_price=signal_price,
            execution_at=actual_execution_at,
            execution_reference_price=reference_price,
            execution_price_source=execution_price_source,
            previous_close=previous_close,
            daily_price_limit=limit_price,
            execution_bar_low=execution_bar_low,
            execution_bar_high=execution_bar_high,
            slippage_rate=flow.config.slippage_rate,
            execution_price=execution_price,
            shares=shares,
            notional=notional,
            commission=commission,
            stamp_duty=0.0,
            reason=reason,
        )
        holding = HoldingItem(
            code=code,
            name=candidate.name,
            shares=shares,
            entry_event_id=event_id,
            entry_signal_at=signal_at,
            entry_signal_price=signal_price,
            entry_execution_at=actual_execution_at,
            entry_reference_price=reference_price,
            entry_execution_price=execution_price,
            entry_notional=notional,
            buy_commission=commission,
            marked_at=actual_execution_at,
            mark_price=reference_price,
            previous_close=previous_close,
        )
        preselection = tuple(
            replace(item, status="bought") if item.code == code else item
            for item in flow.preselection
        )
        return replace(
            flow,
            updated_at=actual_execution_at,
            monitoring_started_at=flow.monitoring_started_at or actual_execution_at,
            preselection=preselection,
            executions=(*flow.executions, execution),
            holdings=(*flow.holdings, holding),
            accounts=tuple(replace(item, cash=money(item.cash - notional - commission),
                                   first_buy_at=item.first_buy_at or actual_execution_at)
                           if item.code == code else item for item in flow.accounts),
        )

    holding = next((item for item in flow.holdings if item.code == code), None)
    if holding is None:
        raise ValueError("卖出信号股票不在持有池")
    if holding.entry_execution_at.startswith(flow.trade_date):
        raise ValueError("A股 T+1：买入当日不能卖出")
    sell_candidate = next(
        (item for item in flow.sell_candidates if item.code == code), None
    )
    if sell_candidate is None:
        raise ValueError("卖出信号股票不在当日日线卖出候选池")
    limit_price = daily_price_limit(
        action="sell",
        code=code,
        name=sell_candidate.name,
        trade_date=flow.trade_date,
        previous_close=previous_close,
    )
    if at_daily_price_limit(
        action="sell",
        code=code,
        name=sell_candidate.name,
        trade_date=flow.trade_date,
        previous_close=previous_close,
        price=reference_price,
    ):
        raise ValueError(f"撮合参考价达到跌停价{limit_price:.2f}，不能卖出")
    notional = money(execution_price * holding.shares)
    sell_commission = money(notional * flow.config.commission_rate)
    stamp_duty = money(notional * flow.config.stamp_duty_rate)
    entry_cost = money(holding.entry_notional + holding.buy_commission)
    exit_proceeds = money(notional - sell_commission - stamp_duty)
    net_pnl = money(exit_proceeds - entry_cost)
    execution = SignalExecution(
        event_id=event_id,
        code=code,
        name=holding.name,
        action="sell",
        signal_at=signal_at,
        signal_price=signal_price,
        execution_at=actual_execution_at,
        execution_reference_price=reference_price,
        execution_price_source=execution_price_source,
        previous_close=previous_close,
        daily_price_limit=limit_price,
        execution_bar_low=execution_bar_low,
        execution_bar_high=execution_bar_high,
        slippage_rate=flow.config.slippage_rate,
        execution_price=execution_price,
        shares=holding.shares,
        notional=notional,
        commission=sell_commission,
        stamp_duty=stamp_duty,
        reason=reason,
    )
    closed_trade = ClosedTrade(
        code=code,
        name=holding.name,
        shares=holding.shares,
        entry_event_id=holding.entry_event_id,
        exit_event_id=event_id,
        entry_signal_at=holding.entry_signal_at,
        exit_signal_at=signal_at,
        entry_execution_at=holding.entry_execution_at,
        exit_execution_at=actual_execution_at,
        entry_signal_price=holding.entry_signal_price,
        exit_signal_price=signal_price,
        entry_reference_price=holding.entry_reference_price,
        exit_reference_price=reference_price,
        entry_execution_price=holding.entry_execution_price,
        exit_execution_price=execution_price,
        entry_notional=holding.entry_notional,
        exit_notional=notional,
        buy_commission=holding.buy_commission,
        sell_commission=sell_commission,
        stamp_duty=stamp_duty,
        gross_pnl=money(notional - holding.entry_notional),
        net_pnl=net_pnl,
        net_return=net_pnl / entry_cost,
    )
    return replace(
        flow,
        updated_at=actual_execution_at,
        monitoring_started_at=flow.monitoring_started_at or actual_execution_at,
        executions=(*flow.executions, execution),
        holdings=tuple(item for item in flow.holdings if item.code != code),
        closed_trades=(*flow.closed_trades, closed_trade),
        accounts=tuple(replace(item, cash=money(item.cash + exit_proceeds),
                               realized_pnl=money(item.realized_pnl + net_pnl))
                       if item.code == code else item for item in flow.accounts),
    )


def mark_holdings(
    flow: DailyFlow,
    *,
    prices: Mapping[str, float],
    marked_at: str,
    previous_closes: Mapping[str, float] | None = None,
) -> DailyFlow:
    """用最新分钟价格更新仍在持有池中的股票估值。"""

    if flow.market_closed:
        raise ValueError("收盘后的每日结果不能再更新持仓价格")
    if not marked_at.startswith(flow.trade_date):
        raise ValueError("持仓标记时间必须属于当前交易日")
    if any(price <= 0 for price in prices.values()):
        raise ValueError("持仓标记价格必须大于零")
    previous_closes = previous_closes or {}
    if any(price <= 0 for price in previous_closes.values()):
        raise ValueError("持仓前收盘价必须大于零")
    holdings = tuple(
        replace(
            item,
            marked_at=marked_at,
            mark_price=prices[item.code],
            previous_close=previous_closes.get(
                item.code,
                item.previous_close,
            ),
        )
        if item.code in prices
        else item
        for item in flow.holdings
    )
    return replace(
        flow,
        updated_at=marked_at,
        monitoring_started_at=flow.monitoring_started_at or marked_at,
        holdings=holdings,
    )


def close_daily_flow(flow: DailyFlow, *, closed_at: str) -> DailyFlow:
    """冻结当日流程；未触发买点的股票继续保留为未成交预选记录。"""

    if flow.market_closed:
        raise ValueError("每日结果已经收盘")
    if not closed_at.startswith(flow.trade_date):
        raise ValueError("收盘时间必须属于当前交易日")
    preselection = tuple(
        replace(item, status="not_triggered")
        if item.status == "watching"
        else item
        for item in flow.preselection
    )
    return replace(
        flow,
        updated_at=closed_at,
        preselection=preselection,
        closed_at=closed_at,
        market_closed=True,
    )


def holding_document(item: HoldingItem, *, trade_date: str) -> dict[str, object]:
    """生成跨策略一致的单票持仓估值字段。"""

    document = asdict(item)
    market_value = money(item.mark_price * item.shares)
    entry_cost = money(item.entry_notional + item.buy_commission)
    gross_total_pnl = money(market_value - item.entry_notional)
    unrealized_pnl = money(market_value - entry_cost)
    previous_close = item.previous_close
    market_day_pnl = (
        money((item.mark_price - previous_close) * item.shares)
        if previous_close is not None and previous_close > 0
        else None
    )
    market_day_return = (
        item.mark_price / previous_close - 1.0
        if previous_close is not None and previous_close > 0
        else None
    )
    bought_today = item.entry_execution_at.startswith(trade_date)
    account_day_pnl = unrealized_pnl if bought_today else market_day_pnl
    account_day_base = entry_cost if bought_today else (
        money(previous_close * item.shares)
        if previous_close is not None and previous_close > 0
        else None
    )
    sellable_today = not item.entry_execution_at.startswith(trade_date)
    document.update(
        market_value=market_value,
        gross_total_pnl=gross_total_pnl,
        gross_total_return=gross_total_pnl / item.entry_notional,
        unrealized_pnl=unrealized_pnl,
        unrealized_return=unrealized_pnl / entry_cost,
        total_pnl=unrealized_pnl,
        total_return=unrealized_pnl / entry_cost,
        market_day_pnl=market_day_pnl,
        market_day_return=market_day_return,
        account_day_pnl=account_day_pnl,
        account_day_return=(
            account_day_pnl / account_day_base
            if account_day_pnl is not None and account_day_base
            else None
        ),
        sellable_today=sellable_today,
        t1_locked=not sellable_today,
    )
    return document


def holding_pnl_summary(
    holding_items: Sequence[Mapping[str, object]],
) -> dict[str, float]:
    """按资金基数加权汇总持仓池的金额和收益率。"""

    gross_unrealized_pnl = money(
        sum(float(item["gross_total_pnl"]) for item in holding_items)
    )
    gross_cost = money(
        sum(float(item["entry_notional"]) for item in holding_items)
    )
    unrealized_pnl = money(
        sum(float(item["unrealized_pnl"]) for item in holding_items)
    )
    entry_cost = money(
        sum(
            float(item["entry_notional"])
            + float(item["buy_commission"])
            for item in holding_items
        )
    )
    market_day_items = [
        item
        for item in holding_items
        if item.get("market_day_pnl") is not None
        and item.get("previous_close") is not None
    ]
    holding_market_day_pnl = money(
        sum(float(item["market_day_pnl"]) for item in market_day_items)
    )
    market_day_base = money(
        sum(
            float(item["previous_close"]) * int(item["shares"])
            for item in market_day_items
        )
    )
    account_day_items = [
        item
        for item in holding_items
        if item.get("account_day_pnl") is not None
    ]
    open_position_account_day_pnl = money(
        sum(float(item["account_day_pnl"]) for item in account_day_items)
    )
    account_day_base = money(
        sum(
            (
                float(item["entry_notional"])
                + float(item["buy_commission"])
                if bool(item.get("t1_locked"))
                else float(item["previous_close"]) * int(item["shares"])
            )
            for item in account_day_items
        )
    )
    return {
        "gross_unrealized_pnl": gross_unrealized_pnl,
        "gross_unrealized_return": (
            gross_unrealized_pnl / gross_cost if gross_cost else 0.0
        ),
        "unrealized_pnl": unrealized_pnl,
        "unrealized_return": (
            unrealized_pnl / entry_cost if entry_cost else 0.0
        ),
        "holding_market_day_pnl": holding_market_day_pnl,
        "holding_market_day_return": (
            holding_market_day_pnl / market_day_base
            if market_day_base
            else 0.0
        ),
        "open_position_account_day_pnl": open_position_account_day_pnl,
        "open_position_account_day_return": (
            open_position_account_day_pnl / account_day_base
            if account_day_base
            else 0.0
        ),
    }


def strategy_pnl_summary(
    *,
    trade_date: str,
    holding_items: Sequence[Mapping[str, object]],
    closed_trade_items: Sequence[Mapping[str, object]],
    execution_items: Sequence[Mapping[str, object]],
) -> dict[str, float]:
    """生成所有策略共用的加权盈亏金额及收益率汇总。"""

    holding_summary = holding_pnl_summary(holding_items)
    realized_pnl = money(
        sum(float(item["net_pnl"]) for item in closed_trade_items)
    )
    closed_entry_cost = money(
        sum(
            float(item["entry_notional"])
            + float(item["buy_commission"])
            for item in closed_trade_items
        )
    )
    exit_execution_by_id = {
        str(item["event_id"]): item
        for item in execution_items
        if item.get("action") == "sell"
    }
    closed_position_account_day_pnl = 0.0
    closed_account_day_base = 0.0
    for item in closed_trade_items:
        execution = exit_execution_by_id.get(str(item["exit_event_id"]))
        if execution is None:
            continue
        previous_close_value = float(execution["previous_close"])
        shares = int(item["shares"])
        closed_position_account_day_pnl += (
            float(item["exit_notional"])
            - float(item["sell_commission"])
            - float(item["stamp_duty"])
            - previous_close_value * shares
        )
        closed_account_day_base += previous_close_value * shares
    closed_position_account_day_pnl = money(
        closed_position_account_day_pnl
    )
    closed_account_day_base = money(closed_account_day_base)

    open_account_day_base = money(
        sum(
            (
                float(item["entry_notional"])
                + float(item["buy_commission"])
                if str(item["entry_execution_at"]).startswith(trade_date)
                else float(item["previous_close"]) * int(item["shares"])
            )
            for item in holding_items
            if item.get("account_day_pnl") is not None
        )
    )
    open_entry_cost = money(
        sum(
            float(item["entry_notional"])
            + float(item["buy_commission"])
            for item in holding_items
        )
    )
    account_day_pnl = money(
        holding_summary["open_position_account_day_pnl"]
        + closed_position_account_day_pnl
    )
    account_day_base = money(open_account_day_base + closed_account_day_base)
    total_pnl = money(realized_pnl + holding_summary["unrealized_pnl"])
    total_cost = money(open_entry_cost + closed_entry_cost)
    return {
        "realized_pnl": realized_pnl,
        "realized_return": (
            realized_pnl / closed_entry_cost if closed_entry_cost else 0.0
        ),
        **holding_summary,
        "closed_position_account_day_pnl": (
            closed_position_account_day_pnl
        ),
        "closed_position_account_day_return": (
            closed_position_account_day_pnl / closed_account_day_base
            if closed_account_day_base
            else 0.0
        ),
        "account_day_pnl": account_day_pnl,
        "account_day_return": (
            account_day_pnl / account_day_base if account_day_base else 0.0
        ),
        "total_pnl": total_pnl,
        "total_return": total_pnl / total_cost if total_cost else 0.0,
    }


def independent_account_summary(
    *, accounts: Sequence[IndependentAccount], holding_items: Sequence[Mapping[str, object]],
    opening_total_assets: float | None, trade_date: str,
) -> dict[str, object]:
    """首次买入才纳入资金汇总；清仓后保留账户，新纳入本金不计作盈利。"""
    if not accounts:
        return {}
    active = [item for item in accounts if item.has_traded]
    active_codes = {item.code for item in active}
    if any(item["code"] not in active_codes for item in holding_items):
        raise ValueError("持仓账户缺少首次买入记录，请迁移账户统计口径")
    if any(item.cash != item.initial_cash or item.realized_pnl != 0
           for item in accounts if not item.has_traded):
        raise ValueError("未激活账户已有资金变动，请迁移首次买入记录")
    if any(item.first_buy_at[:10] > trade_date for item in active):
        raise ValueError("首次买入日期不能晚于账户快照日期")
    newly_active = [item for item in active if item.first_buy_at[:10] == trade_date]
    capital = money(sum(item.initial_cash for item in active))
    capital_inflow = money(sum(item.initial_cash for item in newly_active))
    cash = money(sum(item.cash for item in active))
    market_value = money(sum(float(item["market_value"]) for item in holding_items))
    assets = money(cash + market_value)
    realized = money(sum(item.realized_pnl for item in active))
    unrealized = money(sum(float(item["unrealized_pnl"]) for item in holding_items))
    pnl = money(assets - capital)
    if money(realized + unrealized) != pnl:
        raise ValueError("独立账户现金、持仓与累计盈亏无法对账")
    opening = opening_total_assets if opening_total_assets is not None else money(capital - capital_inflow)
    day_base = money(opening + capital_inflow)
    day_pnl = money(assets - opening - capital_inflow)
    return {
        "account_count": len(active), "universe_account_count": len(accounts),
        "inactive_account_count": len(accounts) - len(active),
        "new_account_count": len(newly_active), "capital_inflow": capital_inflow,
        "opening_total_assets": opening, "account_day_return_base": day_base,
        "initial_capital": capital,
        "cash_balance": cash, "market_value": market_value, "total_assets": assets,
        "realized_pnl": realized, "unrealized_pnl": unrealized,
        "realized_return": realized / capital if capital else 0.,
        "unrealized_return": unrealized / capital if capital else 0.,
        "total_pnl": pnl, "total_return": pnl / capital if capital else 0.,
        "account_day_pnl": day_pnl, "account_day_return": day_pnl / day_base if day_base else 0.,
        "return_basis": "traded_accounts_initial_capital",
        "account_day_return_basis": "opening_assets_plus_capital_inflow",
    }


def daily_flow_document(flow: DailyFlow) -> dict[str, object]:
    """生成前端一次请求即可渲染完整每日量化流程的 MongoDB 文档。"""

    holding_items = [
        holding_document(item, trade_date=flow.trade_date)
        for item in flow.holdings
    ]
    closed_trade_items = [asdict(item) for item in flow.closed_trades]
    execution_items = [asdict(item) for item in flow.executions]
    pnl_summary = strategy_pnl_summary(
        trade_date=flow.trade_date,
        holding_items=holding_items,
        closed_trade_items=closed_trade_items,
        execution_items=execution_items,
    )
    pnl_summary.update(independent_account_summary(
        accounts=flow.accounts, holding_items=holding_items,
        opening_total_assets=flow.opening_total_assets, trade_date=flow.trade_date))
    buy_count = sum(item.action == "buy" for item in flow.executions)
    sell_count = sum(item.action == "sell" for item in flow.executions)
    if flow.market_closed:
        status = "closed"
    elif flow.monitoring_started_at:
        status = "monitoring"
    else:
        status = "waiting_open"
    return {
        "schema_version": "1.5",
        "strategy_id": STRATEGY_ID,
        "trade_date": flow.trade_date,
        "selection_date": flow.selection_date,
        "status": status,
        "strategy": {
            "id": STRATEGY_ID,
            "name": STRATEGY_LABEL,
            "version": STRATEGY_VERSION,
            "macd_parameters": [
                flow.config.fast_period,
                flow.config.slow_period,
                flow.config.signal_period,
            ],
            "intraday_interval": INTRADAY_INTERVAL,
            "minimum_shrink_ratio": MINIMUM_SHRINK_RATIO,
            "confirmation_bars": CONFIRMATION_BARS,
            "buy_filter": {"indicator": "ADX", "period": 14, "minimum": 20,
                           "comparison": "t-1 > t-4", "cutoff": "previous_completed_day"},
            "exit_policy": "E2", "recording_start_date": RECORDING_START_DATE,
        },
        "execution_rule": {
            "mode": "explicit_reference_price_with_bounded_slippage",
            "initial_cash_per_stock": flow.config.initial_cash,
            "slippage_rate": flow.config.slippage_rate,
            "commission_rate": flow.config.commission_rate,
            "stamp_duty_rate": flow.config.stamp_duty_rate,
            "lot_size": flow.config.lot_size,
            "settlement": "T+1",
            "price_limit": (
                "limit_up_buy_cancelled_and_limit_down_sell_deferred"
            ),
        },
        "summary": {
            "preselection_count": len(flow.preselection),
            "watching_count": sum(
                item.status == "watching" for item in flow.preselection
            ),
            "not_triggered_count": sum(
                item.status == "not_triggered" for item in flow.preselection
            ),
            "sell_candidate_count": len(flow.sell_candidates),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "holding_count": len(flow.holdings),
            "t1_locked_holding_count": sum(
                bool(item["t1_locked"]) for item in holding_items
            ),
            "closed_trade_count": len(flow.closed_trades),
            **pnl_summary,
        },
        "preselection_pool": {
            "count": len(flow.preselection),
            "items": [asdict(item) for item in flow.preselection],
        },
        "sell_candidate_pool": {
            "count": len(flow.sell_candidates),
            "items": [asdict(item) for item in flow.sell_candidates],
        },
        "intraday_trading": {
            "interval": INTRADAY_INTERVAL,
            "count": len(flow.executions),
            "items": execution_items,
        },
        "holding_pool": {
            "count": len(holding_items),
            "items": holding_items,
        },
        "closed_trades": {
            "count": len(flow.closed_trades),
            "items": closed_trade_items,
        },
        "timeline": [
            {
                "stage": "after_close_selection",
                "status": "completed",
                "at": flow.generated_at,
            },
            {
                "stage": "intraday_monitoring",
                "status": (
                    "completed"
                    if flow.market_closed
                    else "in_progress"
                    if flow.monitoring_started_at
                    else "waiting"
                ),
                "at": flow.monitoring_started_at,
            },
            {
                "stage": "daily_close",
                "status": "completed" if flow.market_closed else "waiting",
                "at": flow.closed_at,
            },
        ],
        "generated_at": flow.generated_at,
        "updated_at": flow.updated_at,
    }
