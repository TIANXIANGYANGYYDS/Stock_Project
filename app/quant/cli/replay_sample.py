"""随机抽样股票并批量回放盘中临时日线 MACD 策略。"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from pymongo import MongoClient

from app.core.config import PROJECT_ROOT, get_settings
from app.quant.cli.replay_stock import (
    _load_daily_documents,
    _load_minute_bars,
    replay_official,
)
from app.quant.core.execution import money
from app.quant.core.models import Bar
from app.quant.data.market_data import (
    DAILY_HISTORY_COLLECTION,
    DEFAULT_ADJUST,
    THREE_MINUTE_HISTORY_COLLECTION,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    INTRADAY_INTERVAL,
    MINIMUM_SHRINK_RATIO,
    STRATEGY_ID,
    STRATEGY_LABEL,
    STRATEGY_VERSION,
    official_backtest_config,
)


DEFAULT_SAMPLE_SIZE = 300
DEFAULT_RANDOM_SEED = 20260903


@dataclass(frozen=True)
class SampleStock:
    sample_rank: int
    code: str
    name_at_start: str


class CsvSink:
    """在第一批记录出现时建立表头，结束时保证文件一定存在。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Any | None = None
        self.writer: csv.DictWriter[str] | None = None

    def write(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            if self.writer is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.file = self.path.open("w", encoding="utf-8-sig", newline="")
                self.writer = csv.DictWriter(self.file, fieldnames=list(row))
                self.writer.writeheader()
            self.writer.writerow(row)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
        elif not self.path.exists():
            self.path.write_text("", encoding="utf-8")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="随机抽取股票，以独立账户批量回放盘中临时日线MACD。"
    )
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            PROJECT_ROOT / ".local" / "quant" / STRATEGY_ID / "samples"
        ),
    )
    return parser


def sample_stocks(
    universe: Sequence[tuple[str, str]], *, sample_size: int, seed: int
) -> list[SampleStock]:
    """从按代码去重后的期初股票池中进行可复现的等概率抽样。"""

    unique = sorted(dict(universe).items())
    if sample_size <= 0:
        raise ValueError("sample-size必须大于0")
    if sample_size > len(unique):
        raise ValueError(
            f"sample-size={sample_size}超过期初股票池数量{len(unique)}"
        )
    selected = random.Random(seed).sample(unique, sample_size)
    return [
        SampleStock(rank, code, name)
        for rank, (code, name) in enumerate(selected, start=1)
    ]


def percentile(values: Sequence[float], probability: float) -> float:
    """用线性插值计算一个非空序列的百分位。"""

    if not values:
        raise ValueError("百分位序列不能为空")
    if not 0 <= probability <= 1:
        raise ValueError("百分位概率必须位于[0,1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def maximum_drawdown(values: Sequence[float]) -> float:
    """计算资产序列从历史峰值到后续谷值的最大回撤。"""

    peak = 0.0
    drawdown = 0.0
    for value in values:
        if value <= 0:
            raise ValueError("资产必须大于0")
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    sink = CsvSink(path)
    try:
        sink.write(rows)
    finally:
        sink.close()


def _market_dates(collection: Any, *, start_date: str, end_date: str) -> list[str]:
    return sorted(
        str(item)
        for item in collection.distinct(
            "trade_date",
            {
                "adjust": DEFAULT_ADJUST,
                "trade_date": {"$gte": start_date, "$lte": end_date},
            },
        )
    )


def _sample_universe(collection: Any, *, start_date: str) -> list[tuple[str, str]]:
    documents = collection.find(
        {"trade_date": start_date, "adjust": DEFAULT_ADJUST},
        {"_id": 0, "code": 1, "name": 1},
    )
    return [
        (str(item["code"]), str(item.get("name") or item["code"]))
        for item in documents
    ]


def _portfolio_rows(
    *,
    market_dates: Sequence[str],
    stock_daily_rows: Sequence[Sequence[dict[str, Any]]],
    initial_cash_per_stock: float,
) -> list[dict[str, Any]]:
    totals = {
        trade_date: {
            "cash": 0.0,
            "market_value": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "holding_count": 0,
        }
        for trade_date in market_dates
    }
    for rows in stock_daily_rows:
        by_date = {str(item["trade_date"]): item for item in rows}
        state = {
            "cash_at_close": initial_cash_per_stock,
            "market_value": 0.0,
            "realized_pnl_cumulative": 0.0,
            "unrealized_pnl": 0.0,
            "account_state_at_close": "空仓",
        }
        for trade_date in market_dates:
            if trade_date in by_date:
                state = by_date[trade_date]
            target = totals[trade_date]
            target["cash"] += float(state["cash_at_close"])
            target["market_value"] += float(state["market_value"])
            target["realized_pnl"] += float(state["realized_pnl_cumulative"])
            target["unrealized_pnl"] += float(state["unrealized_pnl"])
            target["holding_count"] += (
                str(state["account_state_at_close"]) == "持仓"
            )
    capital_base = initial_cash_per_stock * len(stock_daily_rows)
    output: list[dict[str, Any]] = []
    for trade_date in market_dates:
        item = totals[trade_date]
        cash = money(item["cash"])
        market_value = money(item["market_value"])
        assets = money(cash + market_value)
        output.append(
            {
                "trade_date": trade_date,
                "cash": cash,
                "market_value": market_value,
                "total_assets": assets,
                "total_pnl": money(assets - capital_base),
                "total_return": assets / capital_base - 1.0,
                "realized_pnl_cumulative": money(item["realized_pnl"]),
                "unrealized_pnl": money(item["unrealized_pnl"]),
                "holding_account_count": int(item["holding_count"]),
            }
        )
    return output


def _stock_result_row(
    stock: SampleStock, summary: dict[str, Any]
) -> dict[str, Any]:
    return {
        "sample_rank": stock.sample_rank,
        "code": stock.code,
        "name_at_start": stock.name_at_start,
        "name_at_end": summary["name"],
        "strategy_id": summary["strategy_id"],
        "official_strategy_configuration": summary[
            "official_strategy_configuration"
        ],
        "daily_count": summary["daily_count"],
        "complete_minute_day_count": summary["complete_minute_day_count"],
        "monitored_day_count": summary["monitored_day_count"],
        "buy_signal_count": summary["buy_signal_count"],
        "sell_signal_count": summary["sell_signal_count"],
        "false_intraday_signal_count": summary["false_intraday_signal_count"],
        "filled_buy_count": summary["filled_buy_count"],
        "filled_sell_count": summary["filled_sell_count"],
        "rejected_limit_up_buy_count": summary[
            "rejected_limit_up_buy_count"
        ],
        "deferred_limit_down_attempt_count": summary[
            "deferred_limit_down_attempt_count"
        ],
        "closed_trade_count": summary["closed_trade_count"],
        "winning_closed_trade_count": summary["winning_closed_trade_count"],
        "realized_pnl": summary["realized_pnl"],
        "end_holding": summary["end_holding"],
        "end_shares": summary["end_shares"],
        "end_cash": summary["end_cash"],
        "end_market_value": summary["end_market_value"],
        "final_assets": summary["final_assets"],
        "total_pnl": summary["total_pnl"],
        "total_return": summary["total_return"],
        "pending_action_at_end": summary["pending_action_at_end"] or "",
    }


def render_report(
    *,
    summary: dict[str, Any],
    portfolio_rows: Sequence[dict[str, Any]],
    stock_rows: Sequence[dict[str, Any]],
) -> str:
    best = sorted(stock_rows, key=lambda item: float(item["total_return"]), reverse=True)[:10]
    worst = sorted(stock_rows, key=lambda item: float(item["total_return"]))[:10]
    lines = [
        f"# {summary['sample_size']}只随机股票：盘中临时日线MACD真实回放",
        "",
        f"- 策略：`{summary['strategy_id']}` v{summary['strategy_version']}（正式配置）",
        "",
        "## 回放口径",
        "",
        f"- 窗口：`{summary['start_date']}` 至 `{summary['end_date']}`，共 `{summary['market_trade_day_count']}` 个市场交易日。",
        f"- 抽样：从期初 `{summary['universe_size']}` 只有日线记录的股票中等概率抽取 `{summary['sample_size']}` 只，固定随机种子 `{summary['random_seed']}`。",
        f"- 每只股票独立{summary['initial_cash_per_stock']:.2f}元，窗口开始为空仓；MACD参数{tuple(summary['daily_macd_parameters'])}，至少{summary['daily_warmup_bars']}根日线预热。",
        "- 前一完整日线绿柱继续变长时进入买入观察，红柱继续变长且已有可卖持仓时进入卖出观察。",
        f"- 每{summary['intraday_interval']}使用当前价格重算一根临时日线；柱体缩短至少{summary['minimum_shrink_ratio']:.2%}并连续{summary['confirmation_bars']}根成立后发出信号，下一根{summary['intraday_interval']}K线开盘撮合。",
        f"- 双边滑点{summary['slippage_rate']:.4%}、佣金{summary['commission_rate']:.4%}、卖出印花税{summary['stamp_duty_rate']:.4%}；涨停不买、跌停卖出顺延、T+1。",
        "- 本结果只执行提前盘中路径；提前买入未成交后，没有叠加收盘确认后的次日兜底路径。",
        "",
        "## 总体结果",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 初始总资金 | {summary['capital_base']:.2f}元 |",
        f"| 期末总资产 | {summary['final_assets']:.2f}元 |",
        f"| 总盈亏 | {summary['total_pnl']:.2f}元 |",
        f"| 总收益率/账户平均收益率 | {summary['total_return']:.4%} |",
        f"| 组合最大回撤 | {summary['maximum_drawdown']:.4%} |",
        f"| 单账户收益率中位数 | {summary['median_stock_return']:.4%} |",
        f"| 单账户收益率25%/75%分位 | {summary['stock_return_p25']:.4%} / {summary['stock_return_p75']:.4%} |",
        f"| 盈利/亏损/持平账户 | {summary['profitable_account_count']} / {summary['losing_account_count']} / {summary['flat_account_count']} |",
        f"| 有成交/从未成交账户 | {summary['traded_account_count']} / {summary['never_traded_account_count']} |",
        f"| 期末仍持仓账户 | {summary['end_holding_account_count']} |",
        f"| 闭合交易及胜率 | {summary['closed_trade_count']} / {summary['closed_trade_win_rate_text']} |",
        "",
        "## 信号与执行",
        "",
        f"- 买入信号 `{summary['buy_signal_count']}` 次，实际买入 `{summary['filled_buy_count']}` 次，涨停拒绝 `{summary['rejected_limit_up_buy_count']}` 次。",
        f"- 卖出信号 `{summary['sell_signal_count']}` 次，实际卖出 `{summary['filled_sell_count']}` 次，跌停顺延尝试 `{summary['deferred_limit_down_attempt_count']}` 次。",
        f"- 盘中触发但收盘不再确认的信号 `{summary['false_intraday_signal_count']}` 次，占全部盘中信号 `{summary['false_intraday_signal_rate']:.2%}`。",
        f"- 已实现盈亏 `{summary['realized_pnl']:.2f}` 元，期末未实现盈亏 `{summary['unrealized_pnl']:.2f}` 元。",
        "",
        "## 数据覆盖",
        "",
        f"- 抽样账户实际有日线的股票交易日 `{summary['stock_daily_day_count']}` 个，其中{summary['intraday_interval']}数据完整 `{summary['complete_minute_stock_day_count']}` 个，完整率 `{summary['minute_data_coverage_rate']:.3%}`。",
        f"- 因{summary['intraday_interval']}数据不完整而跳过的本应监控日期 `{summary['skipped_monitoring_data_gap_count']}` 个；完整{summary['intraday_interval']}日与日线收盘价不一致 `{summary['minute_daily_close_mismatch_count']}` 个。",
        "",
        "## 月末组合资产",
        "",
        "| 日期 | 总资产 | 累计盈亏 | 累计收益率 | 持仓账户 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    month_end: dict[str, dict[str, Any]] = {}
    for row in portfolio_rows:
        month_end[str(row["trade_date"])[:7]] = row
    for row in month_end.values():
        lines.append(
            f"| {row['trade_date']} | {float(row['total_assets']):.2f} | "
            f"{float(row['total_pnl']):.2f} | {float(row['total_return']):.4%} | "
            f"{row['holding_account_count']} |"
        )

    def add_rank(title: str, rows: Sequence[dict[str, Any]]) -> None:
        lines.extend(
            [
                "",
                title,
                "",
                "| 代码 | 名称 | 收益率 | 盈亏 | 买入/卖出 | 期末状态 |",
                "| --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for row in rows:
            lines.append(
                f"| {row['code']} | {row['name_at_end']} | "
                f"{float(row['total_return']):.4%} | {float(row['total_pnl']):.2f} | "
                f"{row['filled_buy_count']}/{row['filled_sell_count']} | "
                f"{'持仓' if row['end_holding'] else '现金'} |"
            )

    add_rank("## 收益最高的10个账户", best)
    add_rank("## 收益最低的10个账户", worst)
    lines.extend(
        [
            "",
            "## 结果文件",
            "",
            f"- `sampled_stocks.csv`：固定种子的{summary['sample_size']}只样本。",
            "- `stock_results.csv`：每只股票独立账户结果。",
            f"- `daily_portfolio.csv`：{summary['sample_size']}个账户逐日合并资产。",
            "- `daily_judgements.csv`：每只股票每天的完整判断。",
            f"- `intraday_{summary['intraday_interval']}_checks.csv`：所有实际观察日的逐{summary['intraday_interval']}临时日线判断。",
            "- `signals.csv`、`execution_attempts.csv`、`trade_events.csv`、`closed_trades.csv`：信号、撮合与交易明细。",
            "",
            f"单次{summary['sample_size']}股随机样本可以观察策略行为，但仍不等同于全市场验证；排名还会受到回放窗口行情风格影响。",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> Path:
    if args.start_date > args.end_date:
        raise ValueError("start-date不能晚于end-date")
    start_date = args.start_date.isoformat()
    end_date = args.end_date.isoformat()
    output_directory = (
        args.output_root
        / f"{start_date}_{end_date}"
        / f"n{args.sample_size}_seed{args.seed}"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    intraday_checks_name = "intraday_3m_checks"
    sinks = {
        name: CsvSink(output_directory / f"{name}.csv")
        for name in (
            "daily_judgements",
            intraday_checks_name,
            "signals",
            "execution_attempts",
            "trade_events",
            "closed_trades",
        )
    }
    result_rows_by_sink = {
        "daily_judgements": "daily_rows",
        intraday_checks_name: "intraday_rows",
        "signals": "signal_rows",
        "execution_attempts": "attempt_rows",
        "trade_events": "event_rows",
        "closed_trades": "closed_trade_rows",
    }
    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5_000)
    stock_rows: list[dict[str, Any]] = []
    all_stock_daily_rows: list[Sequence[dict[str, Any]]] = []
    failure_rows: list[dict[str, Any]] = []
    try:
        database = client[settings.mongo_db_name]
        daily_collection = database[DAILY_HISTORY_COLLECTION]
        factor_market_dates = daily_collection.distinct("trade_date", {
            "adjust": DEFAULT_ADJUST, "trade_date": {"$lte": end_date}})
        universe = _sample_universe(daily_collection, start_date=start_date)
        sampled = sample_stocks(
            universe, sample_size=args.sample_size, seed=args.seed
        )
        market_dates = _market_dates(
            daily_collection, start_date=start_date, end_date=end_date
        )
        if not market_dates or market_dates[0] != start_date:
            raise ValueError("start-date必须是期初股票池存在的市场交易日")
        _write_csv(
            output_directory / "sampled_stocks.csv",
            [
                {
                    "sample_rank": item.sample_rank,
                    "code": item.code,
                    "name_at_start": item.name_at_start,
                    "random_seed": args.seed,
                    "universe_date": start_date,
                }
                for item in sampled
            ],
        )

        for position, stock in enumerate(sampled, start=1):
            try:
                daily_documents = _load_daily_documents(
                    daily_collection, code=stock.code, through_date=end_date
                )
                daily_bars = [
                    Bar(
                        trade_date=str(item["trade_date"]),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                    )
                    for item in daily_documents
                ]
                minute_bars = _load_minute_bars(
                    database[THREE_MINUTE_HISTORY_COLLECTION],
                    code=stock.code,
                    start_date=start_date,
                    end_date=end_date,
                )
                result = replay_official(
                    market_dates=factor_market_dates,
                    code=stock.code,
                    name=str(daily_documents[-1].get("name") or stock.name_at_start),
                    daily_bars=daily_bars,
                    minute_bars_by_date=minute_bars,
                    start_date=start_date,
                    end_date=end_date,
                    config=official_backtest_config(code=stock.code),
                )
            except (KeyError, TypeError, ValueError) as exc:
                failure_rows.append(
                    {
                        "sample_rank": stock.sample_rank,
                        "code": stock.code,
                        "name_at_start": stock.name_at_start,
                        "reason": str(exc),
                    }
                )
                raise RuntimeError(
                    f"样本{stock.code}无法完成回放，停止以避免悄悄替换随机样本"
                ) from exc

            stock_rows.append(_stock_result_row(stock, result["summary"]))
            all_stock_daily_rows.append(result["daily_rows"])
            for key, sink in sinks.items():
                sink.write(result[result_rows_by_sink[key]])
            if position % 25 == 0 or position == len(sampled):
                print(
                    f"sample_replay_progress completed={position}/{len(sampled)} "
                    f"buys={sum(int(item['filled_buy_count']) for item in stock_rows)} "
                    f"sells={sum(int(item['filled_sell_count']) for item in stock_rows)}",
                    flush=True,
                )
    finally:
        client.close()
        for sink in sinks.values():
            sink.close()

    _write_csv(output_directory / "stock_results.csv", stock_rows)
    _write_csv(output_directory / "failures.csv", failure_rows)
    official_account = official_backtest_config(code="000000")
    portfolio_rows = _portfolio_rows(
        market_dates=market_dates,
        stock_daily_rows=all_stock_daily_rows,
        initial_cash_per_stock=official_account.initial_cash,
    )
    _write_csv(output_directory / "daily_portfolio.csv", portfolio_rows)
    capital_base = money(args.sample_size * official_account.initial_cash)
    final_assets = money(sum(float(item["final_assets"]) for item in stock_rows))
    returns = [float(item["total_return"]) for item in stock_rows]
    signal_count = sum(
        int(item["buy_signal_count"]) + int(item["sell_signal_count"])
        for item in stock_rows
    )
    closed_count = sum(int(item["closed_trade_count"]) for item in stock_rows)
    winning_closed_count = sum(
        int(item["winning_closed_trade_count"]) for item in stock_rows
    )
    stock_daily_day_count = sum(int(item["daily_count"]) for item in stock_rows)
    complete_minute_days = sum(
        int(item["complete_minute_day_count"]) for item in stock_rows
    )
    skipped_gaps = sum(
        "停止交易判断" in str(row["gate_reason"])
        for rows in all_stock_daily_rows
        for row in rows
    )
    close_mismatches = sum(
        int(row["minute_bar_count"]) == EXPECTED_INTRADAY_BARS_PER_DAY
        and not bool(row["minute_close_matches_daily"])
        for rows in all_stock_daily_rows
        for row in rows
    )
    total_pnl = money(final_assets - capital_base)
    realized_pnl = money(sum(float(item["realized_pnl"]) for item in stock_rows))
    unrealized_pnl = money(total_pnl - realized_pnl)
    if portfolio_rows[-1]["total_assets"] != final_assets:
        raise RuntimeError("期末组合资产不等于所有独立账户资产之和")
    if money(realized_pnl + unrealized_pnl) != total_pnl:
        raise RuntimeError("组合总盈亏无法由已实现和未实现盈亏回算")
    summary = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "strategy_name": STRATEGY_LABEL,
        "official_strategy_configuration": True,
        "start_date": start_date,
        "end_date": end_date,
        "market_trade_day_count": len(market_dates),
        "universe_size": len(dict(universe)),
        "sample_size": args.sample_size,
        "random_seed": args.seed,
        "initial_cash_per_stock": official_account.initial_cash,
        "capital_base": capital_base,
        "daily_macd_parameters": [
            official_account.fast_period,
            official_account.slow_period,
            official_account.signal_period,
        ],
        "daily_warmup_bars": official_account.warmup_bars,
        "intraday_interval": INTRADAY_INTERVAL,
        "expected_intraday_bars_per_day": EXPECTED_INTRADAY_BARS_PER_DAY,
        "minimum_shrink_ratio": MINIMUM_SHRINK_RATIO,
        "confirmation_bars": CONFIRMATION_BARS,
        "slippage_rate": official_account.slippage_rate,
        "commission_rate": official_account.commission_rate,
        "stamp_duty_rate": official_account.stamp_duty_rate,
        "successful_account_count": len(stock_rows),
        "failure_count": len(failure_rows),
        "final_assets": final_assets,
        "total_pnl": total_pnl,
        "total_return": final_assets / capital_base - 1.0,
        "maximum_drawdown": maximum_drawdown(
            [capital_base]
            + [float(item["total_assets"]) for item in portfolio_rows]
        ),
        "median_stock_return": median(returns),
        "stock_return_p10": percentile(returns, 0.10),
        "stock_return_p25": percentile(returns, 0.25),
        "stock_return_p75": percentile(returns, 0.75),
        "stock_return_p90": percentile(returns, 0.90),
        "profitable_account_count": sum(float(item["total_pnl"]) > 0 for item in stock_rows),
        "losing_account_count": sum(float(item["total_pnl"]) < 0 for item in stock_rows),
        "flat_account_count": sum(float(item["total_pnl"]) == 0 for item in stock_rows),
        "traded_account_count": sum(int(item["filled_buy_count"]) > 0 for item in stock_rows),
        "never_traded_account_count": sum(int(item["filled_buy_count"]) == 0 for item in stock_rows),
        "end_holding_account_count": sum(bool(item["end_holding"]) for item in stock_rows),
        "buy_signal_count": sum(int(item["buy_signal_count"]) for item in stock_rows),
        "sell_signal_count": sum(int(item["sell_signal_count"]) for item in stock_rows),
        "false_intraday_signal_count": sum(int(item["false_intraday_signal_count"]) for item in stock_rows),
        "false_intraday_signal_rate": (
            sum(int(item["false_intraday_signal_count"]) for item in stock_rows)
            / signal_count
            if signal_count
            else 0.0
        ),
        "filled_buy_count": sum(int(item["filled_buy_count"]) for item in stock_rows),
        "filled_sell_count": sum(int(item["filled_sell_count"]) for item in stock_rows),
        "rejected_limit_up_buy_count": sum(int(item["rejected_limit_up_buy_count"]) for item in stock_rows),
        "deferred_limit_down_attempt_count": sum(int(item["deferred_limit_down_attempt_count"]) for item in stock_rows),
        "closed_trade_count": closed_count,
        "winning_closed_trade_count": winning_closed_count,
        "closed_trade_win_rate": winning_closed_count / closed_count if closed_count else None,
        "closed_trade_win_rate_text": f"{winning_closed_count / closed_count:.2%}" if closed_count else "-",
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "stock_daily_day_count": stock_daily_day_count,
        "complete_minute_stock_day_count": complete_minute_days,
        "minute_data_coverage_rate": complete_minute_days / stock_daily_day_count if stock_daily_day_count else None,
        "skipped_monitoring_data_gap_count": skipped_gaps,
        "minute_daily_close_mismatch_count": close_mismatches,
    }
    (output_directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path = output_directory / "report.md"
    report_path.write_text(
        render_report(
            summary=summary,
            portfolio_rows=portfolio_rows,
            stock_rows=stock_rows,
        ),
        encoding="utf-8",
    )
    return report_path


def main() -> None:
    report_path = run(build_argument_parser().parse_args())
    print(f"quant_sample_replay_finished report={report_path}", flush=True)


if __name__ == "__main__":
    main()
