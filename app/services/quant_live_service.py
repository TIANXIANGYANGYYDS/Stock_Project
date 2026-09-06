"""正式策略的只读影子盘编排服务。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from app.db.mongo import db as default_db
from app.quant.core.indicators import calculate_macd
from app.quant.core.models import Bar
from app.quant.runtime.daily_flow import (
    HoldingItem,
    IndependentAccount,
    independent_account_summary,
    PreselectionItem,
    SellCandidateItem,
    create_daily_flow,
    daily_flow_document,
    holding_document,
    strategy_pnl_summary,
)
from app.quant.runtime.daily_macd import calculate_daily_macd_states
from app.quant.runtime.live import (
    LIVE_RUNTIME_SCHEMA_VERSION,
    LiveObservationSpec,
    LiveThreeMinuteBar,
    aggregate_complete_three_minute_bars,
    expected_completed_bar_count,
    next_evaluation_at,
    observation_spec_document,
    observation_spec_from_document,
    opening_flow_document,
    opening_flow_from_document,
    replay_live_day,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    INTRADAY_INTERVAL,
    determine_observation_action,
    official_backtest_config,
)
from app.quant.strategies.provisional_daily_macd_3m.adx import (
    LIVE_RECORDING_START, buy_allowed, daily_adx_snapshot,
)
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_VERSION
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from app.services.stock_daily_detail_service import (
    resolve_a_stock_target_trade_date,
)
from app.services.trading_calendar_service import (
    MorningTradeDateDecision,
    resolve_morning_trade_dates,
)


CN_TZ = timezone(timedelta(hours=8))
DAILY_SOURCE_COLLECTION = "stock_daily_detail"
REALTIME_SOURCE_COLLECTION = "stock_realtime_minute_bars"
LIVE_RESULT_SCHEMA_VERSION = "3.0"
MARKET_CLOSE_FINALIZE_TIME = time(15, 5)
MARKET_CLOSE_HARD_FINALIZE_TIME = time(15, 10)
MAX_TRACKED_CODES = 2_000
MAX_DAILY_BARS_PER_CODE = 10_000
MAX_MINUTE_ROWS_PER_CODE = 300
MAX_THREE_MINUTE_BARS_TOTAL = (
    MAX_TRACKED_CODES * EXPECTED_INTRADAY_BARS_PER_DAY
)
MONGO_STREAM_BATCH_SIZE = 1_000
LATEST_MARK_QUERY_BATCH_SIZE = 50


def _holding_from_public_document(document: Mapping[str, Any]) -> HoldingItem:
    allowed = {item.name for item in fields(HoldingItem)}
    return HoldingItem(
        **{key: value for key, value in document.items() if key in allowed}
    )


def _initial_observation(spec: LiveObservationSpec) -> dict[str, Any]:
    return {
        "code": spec.code,
        "name": spec.name,
        "action": spec.action,
        "state": "holding" if spec.action == "hold" else "watching",
        "data_status": "waiting_data",
        "observation_before_date": spec.observation_before_date,
        "observation_date": spec.observation_date,
        "reference_histogram": spec.reference_histogram,
        "provisional_histogram": None,
        "shrink_ratio": None,
        "condition_met": False,
        "consecutive_confirmations": 0,
        "required_confirmations": CONFIRMATION_BARS,
        "last_complete_bar_at": None,
        "signal_id": None,
        "reason": "等待第一根完整三分钟K线",
        "adx_14": spec.adx, "adx_14_3_days_ago": spec.adx_3_days_ago,
        "factor_completed_date": spec.factor_completed_date,
        "factor_comparison_date": spec.factor_comparison_date,
    }


def _observation_state_counts(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in observations:
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _public_runtime(
    *,
    version: int,
    evaluated_at: str,
    expected_bar_count: int,
    last_complete_bar_at: str | None,
    observation_count: int,
    complete_observation_count: int,
    incomplete_codes: Sequence[str],
    tracked_code_count: int,
    market_closed: bool,
) -> dict[str, Any]:
    if market_closed:
        data_status = "closed" if not incomplete_codes else "closed_partial"
    elif expected_bar_count == 0:
        data_status = "waiting_open"
    elif not incomplete_codes:
        data_status = "fresh"
    else:
        data_status = "partial"
    return {
        "schema_version": LIVE_RUNTIME_SCHEMA_VERSION,
        "mode": "shadow",
        "version": version,
        "evaluated_at": evaluated_at,
        "last_complete_bar_at": last_complete_bar_at,
        "next_evaluation_at": (
            None
            if market_closed
            else next_evaluation_at(
                evaluated_at[:10],
                expected_bar_count,
            )
        ),
        "expected_complete_bar_count": expected_bar_count,
        "bars_per_complete_day": EXPECTED_INTRADAY_BARS_PER_DAY,
        "observation_count": observation_count,
        "complete_observation_count": complete_observation_count,
        "incomplete_observation_count": max(
            0, observation_count - complete_observation_count
        ),
        "tracked_code_count": tracked_code_count,
        "incomplete_code_count": len(incomplete_codes),
        "incomplete_codes": list(incomplete_codes),
        "data_status": data_status,
        "source": {
            "daily": DAILY_SOURCE_COLLECTION,
            "intraday": REALTIME_SOURCE_COLLECTION,
            "intraday_source_interval": "1m",
            "strategy_interval": INTRADAY_INTERVAL,
            "adjustment": "previous_close_ratio_to_qfq_state",
        },
    }


class QuantLiveService:
    """生成观察池，并把实时分钟行情确定性重放为影子交易快照。"""

    def __init__(self, database: AsyncIOMotorDatabase | None = None) -> None:
        self.database = default_db if database is None else database
        self.results = QuantDailyResultRepository(self.database)
        self.daily_collection = self.database[DAILY_SOURCE_COLLECTION]
        self.minute_collection = self.database[REALTIME_SOURCE_COLLECTION]

    async def ensure_indexes(self) -> None:
        await self.results.create_indexes()

    async def _resolve_trade_dates(
        self, target: date
    ) -> MorningTradeDateDecision:
        """解析交易日；内置日历过期时以实际沪指交易日接口为准。"""

        try:
            return resolve_morning_trade_dates(target)
        except RuntimeError as exc:
            if "交易日历不覆盖" not in str(exc):
                raise

        target_iso = target.isoformat()
        local_current = await self.daily_collection.find_one(
            {"adjust": "qfq", "trade_date": target_iso},
            {"_id": 0, "trade_date": 1},
        )
        if local_current is not None:
            analysis_date = target_iso
            is_current_trade_day = True
        else:
            external = await resolve_a_stock_target_trade_date(
                target.strftime("%Y%m%d")
            )
            analysis_date = external.target_trade_date
            is_current_trade_day = external.is_reference_trade_day

        previous = await self.daily_collection.find_one(
            {
                "adjust": "qfq",
                "trade_date": {"$lt": analysis_date},
            },
            {"_id": 0, "trade_date": 1},
            sort=[("trade_date", -1)],
        )
        if previous is None:
            raise RuntimeError(
                f"本地日线缺少{analysis_date}之前的交易日，无法冻结量化输入"
            )
        return MorningTradeDateDecision(
            reference_date=target_iso,
            analysis_date=analysis_date,
            prev_trade_date=str(previous["trade_date"]),
            is_current_trade_day=is_current_trade_day,
        )

    async def _load_three_minute_bars(
        self, *, trade_date: str, codes: Sequence[str]
    ) -> dict[str, tuple[LiveThreeMinuteBar, ...]]:
        """按股票顺序流式聚合，内存中不保留全观察池的一分钟原始行。"""

        if len(codes) > MAX_TRACKED_CODES:
            raise RuntimeError(
                f"量化观察代码超过内存安全上限: {len(codes)}/{MAX_TRACKED_CODES}"
            )
        output: dict[str, tuple[LiveThreeMinuteBar, ...]] = {
            code: () for code in codes
        }
        if not codes:
            return output
        start_at = f"{trade_date}T09:30:00+08:00"
        end_at = f"{trade_date}T14:59:00+08:00"
        cursor = self.minute_collection.find(
            {
                "trade_date": trade_date,
                "interval": "1m",
                "code": {"$in": list(codes)},
                "timestamp": {"$gte": start_at, "$lte": end_at},
            },
            {
                "_id": 0,
                "code": 1,
                "timestamp": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "previous_close": 1,
            },
        ).sort([("code", ASCENDING), ("timestamp", ASCENDING)])
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(MONGO_STREAM_BATCH_SIZE)

        current_code: str | None = None
        current_rows: list[dict[str, Any]] = []
        aggregated_bar_count = 0

        def finish_code() -> None:
            nonlocal aggregated_bar_count, current_rows
            if current_code is not None:
                bars = aggregate_complete_three_minute_bars(
                    current_rows,
                    trade_date=trade_date,
                )
                aggregated_bar_count += len(bars)
                if aggregated_bar_count > MAX_THREE_MINUTE_BARS_TOTAL:
                    raise RuntimeError(
                        "量化三分钟K线超过内存安全上限: "
                        f"{aggregated_bar_count}/{MAX_THREE_MINUTE_BARS_TOTAL}"
                    )
                output[current_code] = bars
            current_rows = []

        async for row in cursor:
            code = str(row.get("code") or "")
            if current_code is not None and code != current_code:
                finish_code()
            current_code = code
            current_rows.append(row)
            if len(current_rows) > MAX_MINUTE_ROWS_PER_CODE:
                raise RuntimeError(
                    f"股票{code}的一分钟数据超过内存安全上限: "
                    f"{len(current_rows)}/{MAX_MINUTE_ROWS_PER_CODE}"
                )
        finish_code()
        return output

    async def _load_latest_holding_marks(
        self,
        *,
        trade_date: str,
        codes: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """按现有代码时间索引分批读取各持仓最新一分钟价格。"""

        if len(codes) > MAX_TRACKED_CODES:
            raise RuntimeError(
                f"量化持仓代码超过内存安全上限: {len(codes)}/{MAX_TRACKED_CODES}"
            )
        if not codes:
            return {}
        latest: dict[str, dict[str, Any]] = {}
        start_at = f"{trade_date}T09:30:00+08:00"
        end_at = f"{trade_date}T14:59:00+08:00"

        async def load_one(code: str) -> dict[str, Any] | None:
            return await self.minute_collection.find_one(
                {
                    "code": code,
                    "interval": "1m",
                    "timestamp": {"$gte": start_at, "$lte": end_at},
                },
                {
                    "_id": 0,
                    "code": 1,
                    "timestamp": 1,
                    "close": 1,
                    "previous_close": 1,
                },
                sort=[("timestamp", DESCENDING)],
            )

        rows: list[dict[str, Any] | None] = []
        for offset in range(0, len(codes), LATEST_MARK_QUERY_BATCH_SIZE):
            rows.extend(
                await asyncio.gather(
                    *(load_one(code) for code in codes[
                        offset : offset + LATEST_MARK_QUERY_BATCH_SIZE
                    ])
                )
            )
        for row in rows:
            if row is None:
                continue
            code = str(row.get("code") or "")
            try:
                close = float(row["close"])
                previous_close = float(row["previous_close"])
                timestamp = str(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if close <= 0 or previous_close <= 0 or not timestamp:
                continue
            latest[code] = {
                "price": close,
                "previous_close": previous_close,
                "marked_at": (
                    datetime.fromisoformat(timestamp) + timedelta(minutes=1)
                ).isoformat(),
            }
        return latest

    @staticmethod
    def _apply_holding_marks(
        document: dict[str, Any],
        marks: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        """把最新价写入公开持仓和汇总，不触碰策略信号状态。"""

        current_items = list(
            document.get("holding_pool", {}).get("items", [])
        )
        changed = False
        updated_items: list[dict[str, object]] = []
        for item in current_items:
            holding = _holding_from_public_document(item)
            mark = marks.get(holding.code)
            if mark is not None:
                price = float(mark["price"])
                previous_close = float(mark["previous_close"])
                marked_at = str(mark["marked_at"])
                if (
                    price != holding.mark_price
                    or previous_close != holding.previous_close
                    or marked_at > holding.marked_at
                ):
                    holding = replace(
                        holding,
                        mark_price=price,
                        previous_close=previous_close,
                        marked_at=marked_at,
                    )
                    changed = True
            updated_items.append(
                holding_document(
                    holding,
                    trade_date=str(document["trade_date"]),
                )
            )
        document["holding_pool"] = {
            "count": len(updated_items),
            "items": updated_items,
        }
        pnl_summary = strategy_pnl_summary(
            trade_date=str(document["trade_date"]),
            holding_items=updated_items,
            closed_trade_items=document.get("closed_trades", {}).get(
                "items", []
            ),
            execution_items=document.get("intraday_trading", {}).get(
                "items", []
            ),
        )
        internal = document.get("_runtime_state", {})
        pnl_summary.update(independent_account_summary(
            accounts=[IndependentAccount(**item) for item in internal.get("accounts", [])],
            holding_items=updated_items,
            opening_total_assets=internal.get("opening_flow", {}).get("opening_total_assets"),
            trade_date=str(document["trade_date"])))
        summary = document.setdefault("summary", {})
        pnl_changed = any(
            summary.get(key) != value
            for key, value in pnl_summary.items()
        )
        if not changed and not pnl_changed:
            return False
        summary.update(pnl_summary)
        if updated_items:
            latest_marked_at = max(
                str(item["marked_at"]) for item in updated_items
            )
            document["updated_at"] = max(
                str(document.get("updated_at") or ""),
                latest_marked_at,
            )
        return True

    async def _refresh_holding_valuations(
        self,
        document: dict[str, Any],
        *,
        evaluated_at: str,
    ) -> dict[str, Any]:
        holdings = document.get("holding_pool", {}).get("items", [])
        codes = sorted({str(item.get("code") or "") for item in holdings})
        marks = await self._load_latest_holding_marks(
            trade_date=str(document["trade_date"]),
            codes=[code for code in codes if code],
        )
        if not self._apply_holding_marks(document, marks):
            return document
        runtime = document.setdefault("runtime", {})
        runtime["version"] = int(runtime.get("version", 0)) + 1
        runtime["evaluated_at"] = evaluated_at
        runtime["last_valuation_at"] = max(
            str(item["marked_at"])
            for item in document["holding_pool"]["items"]
        )
        await self.results.save_document(document)
        return document

    async def _load_previous_state(self, trade_date: str, *, expected_previous_date: str | None = None):
        previous = await self.results.latest_before(trade_date)
        if trade_date == LIVE_RECORDING_START:
            return [], [], [], {}
        if previous is None or previous.get("trade_date", "") < LIVE_RECORDING_START:
            raise RuntimeError(f"新版独立账户缺少前序记录，请先从{LIVE_RECORDING_START}顺序补录")
        if expected_previous_date and previous["trade_date"] != expected_previous_date:
            raise RuntimeError("独立账户记录有交易日缺口，请先按日补录，禁止跳日继续交易")
        if previous.get("strategy", {}).get("version") != STRATEGY_VERSION:
            raise RuntimeError("前序记录仍为旧策略，禁止混用账户；请先完成迁移")
        if previous.get("recording", {}).get("start_date") != LIVE_RECORDING_START:
            raise RuntimeError("前序账户起点不一致，请先完成连续补录，禁止拼接不同起点的收益")
        if previous.get("status") != "closed":
            raise RuntimeError("前一交易日尚未闭合，禁止从未完成账户继续交易")
        holdings = [replace(_holding_from_public_document(item), previous_close=float(item["mark_price"]))
                    for item in previous.get("holding_pool", {}).get("items", [])]
        internal = previous.get("_runtime_state", {})
        accounts = [IndependentAccount(**item) for item in internal.get("accounts", [])]
        if not accounts:
            raise RuntimeError("新版记录缺少独立账户账本")
        return holdings, [dict(item) for item in internal.get("pending_signals", [])], accounts, internal.get("exit_states", {})

    async def _build_observation_pool(
        self,
        *,
        trade_date: str,
        selection_date: str,
        holdings: Sequence[HoldingItem],
        opening_pending: Sequence[Mapping[str, Any]],
        accounts: Sequence[IndependentAccount] = (),
    ) -> tuple[
        list[PreselectionItem],
        list[SellCandidateItem],
        list[LiveObservationSpec],
        dict[str, int],
        list[IndependentAccount],
    ]:
        holding_codes = {item.code for item in holdings}
        pending_codes = {str(item["code"]) for item in opening_pending}
        candidates: dict[str, PreselectionItem] = {}
        sell_candidates: dict[str, SellCandidateItem] = {}
        specs: list[LiveObservationSpec] = []
        counters = {
            "stock_count": 0,
            "eligible_stock_count": 0,
            "stale_daily_count": 0,
            "insufficient_history_count": 0,
            "adx_weak_or_missing_buy_observation_count": 0,
        }
        market_dates = sorted(await self.daily_collection.distinct(
            "trade_date", {"adjust": "qfq", "trade_date": {"$lte": selection_date}}))
        if len(market_dates) < 4 or market_dates[-1] != selection_date:
            raise RuntimeError("ADX14缺少t-1/t-4市场交易日端点")
        comparison_date = market_dates[-4]
        fixed_accounts = {item.code: item for item in accounts}
        initial_accounts: list[IndependentAccount] = []

        cursor = self.daily_collection.find(
            {
                "adjust": "qfq",
                "trade_date": {"$lte": selection_date},
                "close": {"$gt": 0},
            },
            {
                "_id": 0,
                "code": 1,
                "name": 1,
                "trade_date": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
            },
        ).sort([("code", ASCENDING), ("trade_date", ASCENDING)])
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(MONGO_STREAM_BATCH_SIZE)

        current_code: str | None = None
        current_name = ""
        bars: list[Bar] = []
        last_prices: dict[str, float] = {}

        def finish_stock() -> None:
            nonlocal bars
            if current_code is None or not bars:
                return
            code = current_code
            counters["stock_count"] += 1
            last_prices[code] = bars[-1].close
            if not accounts and bars[-1].trade_date == selection_date:
                initial_accounts.append(IndependentAccount(code, current_name))
            stale = bars[-1].trade_date != selection_date
            config = official_backtest_config(code=code)
            insufficient = len(bars) < config.warmup_bars
            counters["stale_daily_count"] += int(stale)
            counters["insufficient_history_count"] += int(insufficient)
            if (stale or insufficient) and code not in holding_codes:
                return
            if accounts and code not in fixed_accounts:
                return
            indicators = calculate_macd(bars, config) if not insufficient else []
            states = calculate_daily_macd_states(bars, config) if not insufficient else []
            action = determine_observation_action(indicators[-2], indicators[-1]) if not stale and indicators else None
            snapshot = daily_adx_snapshot(bars=bars, trade_date=trade_date,
                completed_date=selection_date, comparison_date=comparison_date)
            if action is not None:
                counters["eligible_stock_count"] += 1
            if code in holding_codes:
                action = "sell" if action == "sell" and code not in pending_codes else "hold"
                if action == "sell":
                    sell_candidates[code] = SellCandidateItem(code, current_name,
                        "原MACD红柱继续变长，卖点出现后检查E2延期资格", bars[-1].close)
            elif action == "buy" and code not in pending_codes:
                counters["adx_weak_or_missing_buy_observation_count"] += int(not buy_allowed(snapshot))
                candidates[code] = PreselectionItem(code, current_name,
                    "原MACD绿柱继续变长；三柱确认后须通过ADX14门控", bars[-1].close)
            else:
                return
            specs.append(LiveObservationSpec(code=code, name=current_name, action=action,
                observation_before_date=bars[-2].trade_date if len(bars) > 1 else selection_date,
                observation_date=selection_date, previous_close=bars[-1].close,
                reference_histogram=indicators[-1].histogram if indicators else 0.,
                previous_state=states[-1] if states and not stale else None,
                adx=snapshot.value("adx_14"), adx_3_days_ago=snapshot.value("adx_14_3_days_ago"),
                factor_completed_date=selection_date, factor_comparison_date=comparison_date))

        async for row in cursor:
            code = str(row.get("code") or "").zfill(6)
            if current_code is not None and code != current_code:
                finish_stock()
                bars = []
            current_code = code
            current_name = str(row.get("name") or current_name)
            try:
                bar = Bar(
                    trade_date=str(row["trade_date"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                bar.open <= 0
                or bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
            ):
                continue
            bars.append(bar)
            if len(bars) > MAX_DAILY_BARS_PER_CODE:
                raise RuntimeError(
                    f"股票{code}的日线数据超过内存安全上限: "
                    f"{len(bars)}/{MAX_DAILY_BARS_PER_CODE}"
                )
        finish_stock()

        holding_by_code = {item.code: item for item in holdings}
        spec_codes = {item.code for item in specs}
        for holding in holdings:
            if holding.code not in spec_codes:
                specs.append(LiveObservationSpec(holding.code, holding.name, "hold", selection_date,
                    selection_date, holding.mark_price, 0., None,
                    factor_completed_date=selection_date, factor_comparison_date=comparison_date))
        for pending in opening_pending:
            code = str(pending["code"])
            action = str(pending["action"])
            name = str(pending.get("name") or "")
            reference_price = last_prices.get(
                code, float(pending.get("signal_price") or 0)
            )
            if reference_price <= 0:
                continue
            if action == "buy" and code not in holding_by_code:
                candidates.setdefault(
                    code,
                    PreselectionItem(
                        code=code,
                        name=name,
                        reason="上一交易日买入信号等待撮合",
                        reference_price=reference_price,
                    ),
                )
            elif action == "sell" and code in holding_by_code:
                sell_candidates.setdefault(
                    code,
                    SellCandidateItem(
                        code=code,
                        name=name,
                        reason="上一交易日卖出信号等待撮合",
                        reference_price=reference_price,
                    ),
                )

        return (
            list(candidates.values()),
            list(sell_candidates.values()),
            sorted(specs, key=lambda item: item.code),
            counters,
            list(accounts) if accounts else initial_accounts,
        )

    async def prepare(self, reference_date: date | None = None) -> dict[str, Any]:
        """在开盘前幂等生成当天观察池和不可变开盘状态。"""

        await self.ensure_indexes()
        target = reference_date or datetime.now(CN_TZ).date()
        target_trade_date = target.isoformat()
        if target_trade_date < LIVE_RECORDING_START:
            return {"status": "skipped", "reason": "before_recording_start", "reference_date": target_trade_date}
        if target > datetime.now(CN_TZ).date():
            raise ValueError("不能记录未来交易日")
        existing = await self.results.get(target_trade_date)
        if existing is not None and existing.get("_runtime_state", {}).get(
            "opening_flow"
        ):
            if existing.get("strategy", {}).get("version") != STRATEGY_VERSION:
                raise RuntimeError("该日仍为旧策略记录，请通过迁移入口保留旧账并重建")
            if existing.get("recording", {}).get("start_date") != LIVE_RECORDING_START:
                raise RuntimeError("现有账户起点不一致，请通过历史补录入口重新建账")
            return existing
        decision = await self._resolve_trade_dates(target)
        if not decision.is_current_trade_day:
            return {
                "status": "skipped",
                "reason": "non_trading_day",
                "reference_date": decision.reference_date,
            }
        trade_date = decision.analysis_date

        holdings, opening_pending, accounts, opening_exit_states = await self._load_previous_state(trade_date, expected_previous_date=decision.prev_trade_date)
        candidates, sell_candidates, specs, quality, accounts = (
            await self._build_observation_pool(
                trade_date=trade_date,
                selection_date=decision.prev_trade_date,
                holdings=holdings,
                opening_pending=opening_pending,
                accounts=accounts,
            )
        )
        tracked_codes = {
            *(item.code for item in specs),
            *(str(item["code"]) for item in opening_pending),
            *(item.code for item in holdings),
        }
        if len(tracked_codes) > MAX_TRACKED_CODES:
            raise RuntimeError(
                "量化观察代码超过内存安全上限: "
                f"{len(tracked_codes)}/{MAX_TRACKED_CODES}"
            )
        generated_at = datetime.now(CN_TZ).isoformat()
        flow = create_daily_flow(
            trade_date=trade_date,
            selection_date=decision.prev_trade_date,
            generated_at=generated_at,
            candidates=candidates,
            sell_candidates=sell_candidates,
            holdings=holdings,
            accounts=accounts,
        )
        document = daily_flow_document(flow)
        document["schema_version"] = LIVE_RESULT_SCHEMA_VERSION
        document["recording"] = {
            "start_date": LIVE_RECORDING_START,
            "mode": "historical_replay" if target < datetime.now(CN_TZ).date() else "live",
            "market_data_trade_date": trade_date,
            "computed_at": generated_at,
            "data_kind": "observed_market_data",
            "execution_kind": "shadow_simulation",
            "strategy_version": STRATEGY_VERSION,
        }
        observations = [_initial_observation(spec) for spec in specs]
        document["runtime"] = _public_runtime(
            version=0,
            evaluated_at=generated_at,
            expected_bar_count=0,
            last_complete_bar_at=None,
            observation_count=len(observations),
            complete_observation_count=0,
            incomplete_codes=sorted(tracked_codes),
            tracked_code_count=len(tracked_codes),
            market_closed=False,
        )
        document["runtime"]["preparation_quality"] = quality
        document["runtime"]["resource_limits"] = {
            "tracked_codes": len(tracked_codes),
            "max_tracked_codes": MAX_TRACKED_CODES,
            "max_daily_bars_per_code": MAX_DAILY_BARS_PER_CODE,
            "max_minute_rows_per_code": MAX_MINUTE_ROWS_PER_CODE,
            "max_three_minute_bars_total": MAX_THREE_MINUTE_BARS_TOTAL,
            "mongo_stream_batch_size": MONGO_STREAM_BATCH_SIZE,
            "latest_mark_query_batch_size": LATEST_MARK_QUERY_BATCH_SIZE,
        }
        document["runtime"]["observation_state_counts"] = (
            _observation_state_counts(observations)
        )
        document["runtime"]["recent_signals"] = [
            dict(item) for item in opening_pending[-20:]
        ]
        document["observation_pool"] = {
            "count": len(observations),
            "items": observations,
        }
        document["signals"] = {
            "count": len(opening_pending),
            "items": [dict(item) for item in opening_pending],
        }
        document["_runtime_state"] = {
            "schema_version": LIVE_RUNTIME_SCHEMA_VERSION,
            "opening_flow": opening_flow_document(flow),
            "observation_specs": [
                observation_spec_document(spec) for spec in specs
            ],
            "opening_pending_signals": [dict(item) for item in opening_pending],
            "pending_signals": [dict(item) for item in opening_pending],
            "snapshot_key": None,
            "accounts": [asdict(item) for item in flow.accounts],
            "opening_exit_states": opening_exit_states,
            "exit_states": opening_exit_states,
        }
        await self.results.save_document(document)
        return document

    async def catch_up_completed_days(self, *, before_date: date | None = None) -> list[str]:
        """重启后顺序补齐缺失的已结束交易日，真实行情不足时保留异常状态。"""
        cutoff = (before_date or datetime.now(CN_TZ).date()).isoformat()
        days = sorted(await self.daily_collection.distinct("trade_date", {
            "adjust": "qfq", "trade_date": {"$gte": LIVE_RECORDING_START, "$lt": cutoff}}))
        completed = []
        for day in days:
            document = await self.results.get(day)
            if document and document.get("strategy", {}).get("version") != STRATEGY_VERSION:
                raise RuntimeError(f"{day}仍为旧策略，请先迁移再恢复调度")
            if document and document.get("recording", {}).get("start_date") != LIVE_RECORDING_START:
                raise RuntimeError(f"{day}的账户起点不一致，请先完成连续补录")
            if document and document.get("status") == "closed":
                continue
            available = await self.minute_collection.find_one(
                {"trade_date": day, "interval": "1m"}, {"_id": 1})
            if not available:
                raise RuntimeError(f"{day}缺少真实分钟行情，不能跳过后继续建账")
            await self.process(now=datetime.fromisoformat(f"{day}T15:10:00+08:00"))
            completed.append(day)
        return completed

    async def process(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """读取完整分钟柱并刷新当前交易日影子盘快照。"""

        evaluated = now or datetime.now(CN_TZ)
        if evaluated.tzinfo is None:
            evaluated = evaluated.replace(tzinfo=CN_TZ)
        else:
            evaluated = evaluated.astimezone(CN_TZ)
        if evaluated.time() < time(9, 20):
            return {"status": "skipped", "reason": "before_prepare_window"}

        trade_date = evaluated.date().isoformat()
        document = await self.results.get(trade_date)
        if document is not None and document.get("_runtime_state", {}).get("opening_flow") and document.get("strategy", {}).get("version") != STRATEGY_VERSION:
            raise RuntimeError("旧策略快照不可在新版进程继续重放，请先迁移")
        if document is None or not document.get("_runtime_state", {}).get(
            "opening_flow"
        ):
            document = await self.prepare(evaluated.date())
        if document.get("status") == "skipped":
            return document
        expected_count = expected_completed_bar_count(evaluated, trade_date)
        past_finalize_window = (
            expected_count == EXPECTED_INTRADAY_BARS_PER_DAY
            and evaluated.time() >= MARKET_CLOSE_FINALIZE_TIME
        )
        if document.get("recording") and document["recording"].get("start_date") != LIVE_RECORDING_START:
            raise RuntimeError("现有账户起点不一致，请通过历史补录入口重新建账")
        runtime = document.get("runtime", {})
        already_final = document.get("status") == "closed"
        if already_final:
            return document
        if expected_count == 0:
            return document
        if (
            int(runtime.get("expected_complete_bar_count", -1))
            == expected_count
            and runtime.get("data_status") == "fresh"
            and not past_finalize_window
        ):
            return await self._refresh_holding_valuations(
                document,
                evaluated_at=evaluated.isoformat(),
            )
        internal = document["_runtime_state"]
        opening_flow = opening_flow_from_document(internal["opening_flow"])
        specs = [
            observation_spec_from_document(item)
            for item in internal.get("observation_specs", [])
        ]
        opening_pending = internal.get("opening_pending_signals", [])
        codes = {
            *(item.code for item in specs),
            *(str(item["code"]) for item in opening_pending),
            *(item.code for item in opening_flow.holdings),
        }
        bars_by_code = await self._load_three_minute_bars(
            trade_date=trade_date,
            codes=sorted(codes),
        )
        incomplete_tracked_codes = sorted(
            code
            for code in codes
            if len(bars_by_code.get(code, ())) < expected_count
            or (
                bars_by_code.get(code)
                and bars_by_code[code][0].previous_close is None
            )
        )
        close_market = past_finalize_window and (
            not incomplete_tracked_codes
            or evaluated.time() >= MARKET_CLOSE_HARD_FINALIZE_TIME
        )
        replayed = replay_live_day(
            opening_flow=opening_flow,
            observation_specs=specs,
            opening_pending_signals=opening_pending,
            bars_by_code=bars_by_code,
            expected_bar_count=expected_count,
            close_market=close_market,
            opening_exit_states=internal.get("opening_exit_states", {}),
        )
        if internal.get("snapshot_key") == replayed["snapshot_key"]:
            return await self._refresh_holding_valuations(
                document,
                evaluated_at=evaluated.isoformat(),
            )

        flow = replayed["flow"]
        refreshed = daily_flow_document(flow)
        refreshed["schema_version"] = LIVE_RESULT_SCHEMA_VERSION
        refreshed["recording"] = {**document.get("recording", {}), "computed_at": datetime.now(CN_TZ).isoformat()}
        refreshed["exit_decisions"] = {"count": len(replayed["exit_decisions"]), "items": list(replayed["exit_decisions"])}
        version = int(document.get("runtime", {}).get("version", 0)) + 1
        observations = list(replayed["observations"])
        signals = sorted(
            (dict(item) for item in replayed["signals"]),
            key=lambda item: (str(item.get("signal_at") or ""), item["code"]),
        )
        refreshed["runtime"] = _public_runtime(
            version=version,
            evaluated_at=evaluated.isoformat(),
            expected_bar_count=expected_count,
            last_complete_bar_at=replayed["last_complete_bar_at"],
            observation_count=len(observations),
            complete_observation_count=int(
                replayed["complete_observation_count"]
            ),
            incomplete_codes=incomplete_tracked_codes,
            tracked_code_count=len(codes),
            market_closed=flow.market_closed,
        )
        refreshed["runtime"]["preparation_quality"] = document.get(
            "runtime", {}
        ).get("preparation_quality", {})
        refreshed["runtime"]["resource_limits"] = document.get(
            "runtime", {}
        ).get("resource_limits", {})
        refreshed["runtime"]["observation_state_counts"] = (
            _observation_state_counts(observations)
        )
        refreshed["runtime"]["recent_signals"] = signals[-20:]
        refreshed["observation_pool"] = {
            "count": len(observations),
            "items": observations,
        }
        refreshed["signals"] = {"count": len(signals), "items": signals}
        refreshed["summary"].update(
            signal_count=len(signals),
            pending_signal_count=sum(
                item.get("status")
                in {"pending_execution", "deferred_limit_down", "deferred_t1"}
                for item in signals
            ),
            rejected_signal_count=sum(
                str(item.get("status", "")).startswith("rejected")
                for item in signals
            ),
        )
        refreshed["_runtime_state"] = {
            "schema_version": LIVE_RUNTIME_SCHEMA_VERSION,
            "opening_flow": internal["opening_flow"],
            "observation_specs": internal.get("observation_specs", []),
            "opening_pending_signals": opening_pending,
            "pending_signals": [
                dict(item) for item in replayed["pending_signals"]
            ],
            "snapshot_key": replayed["snapshot_key"],
            "accounts": [asdict(item) for item in flow.accounts],
            "opening_exit_states": internal.get("opening_exit_states", {}),
            "exit_states": replayed["exit_states"],
        }
        if not flow.market_closed:
            marks = await self._load_latest_holding_marks(
                trade_date=trade_date,
                codes=[item.code for item in flow.holdings],
            )
            if self._apply_holding_marks(refreshed, marks):
                refreshed["runtime"]["last_valuation_at"] = max(
                    str(item["marked_at"])
                    for item in refreshed["holding_pool"]["items"]
                )
        await self.results.save_document(refreshed)
        return refreshed


def public_quant_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """兼容旧调用方，统一使用公开数据字段白名单。"""
    from app.quant.public import public_quant_document as present_document

    return present_document(document)
