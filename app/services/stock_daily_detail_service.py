from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional
from uuid import uuid4

from bson.son import SON
import pandas as pd
from pymongo import ASCENDING, MongoClient, ReplaceOne
from pymongo.collection import Collection
from pymongo.database import Database

from app.core.config import get_settings
from app.crawlers.proxy_provider import (
    AsyncRequestRateLimiter,
    AsyncShanchenProxyPool,
)
from app.crawlers.stock_daily_detail_crawler import (
    EastMoneyQuotePageFetcher,
    LocalQuoteCircuitBreaker,
    StockDailyDetailCrawler,
)
from app.models.stock_daily_detail import CN_TZ, StockDailyDetail, now_cn


logger = logging.getLogger(__name__)

STOCK_DAILY_DEFAULT_START_DATE = "20240101"
STOCK_DAILY_DEFAULT_END_DATE = ""
STOCK_DAILY_DEFAULT_ADJUST = "qfq"
STOCK_DAILY_DEFAULT_LIMIT: Optional[int] = None
STOCK_DAILY_DEFAULT_ONLY_CODE: Optional[str] = None
STOCK_DAILY_REQUEST_SLEEP_SECONDS = 0.5
STOCK_DAILY_MAX_RETRY = 2
STOCK_DAILY_DEFAULT_CONCURRENCY = 50
STOCK_DAILY_PAGE_CONCURRENCY = 12
STOCK_DAILY_PROXY_MINUTES = 3
STOCK_DAILY_PROXY_POOL_SIZE = 6
STOCK_DAILY_PROXY_CONCURRENCY_PER_IP = 2
STOCK_DAILY_TRADE_CALENDAR_LOOKBACK_DAYS = 90
STOCK_DAILY_STARTUP_MIN_TIME = "16:00"

SYNC_STATUS_RUNNING = "running"
SYNC_STATUS_SUCCESS = "success"
SYNC_STATUS_PARTIAL_FAILED = "partial_failed"
SYNC_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class StockTradeDateDecision:
    """
    A 股交易日历解析结果。

    reference_yyyymmdd 是用户或 scheduler 传入的参考日期；target_trade_date 是真正
    应该同步或检查的 A 股交易日。如果参考日期不是交易日，target 会回退到上一个
    A 股交易日。
    """

    reference_yyyymmdd: str
    reference_trade_date: str
    target_yyyymmdd: str
    target_trade_date: str
    is_reference_trade_day: bool


@dataclass
class StockDailyDetailSyncResult:
    """
    单次日线同步批次结果。

    这个对象既用于返回给调用方，也会被写入 stock_daily_detail_sync_runs 集合，
    作为后续跳过判断、失败补偿和同步完整性判断的依据。
    """

    run_id: str
    target_trade_date: str
    adjust: str
    run_mode: str
    scope_key: str
    expected_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    affected_total: int = 0
    status: str = SYNC_STATUS_RUNNING
    failed_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StockDailyDetailItemResult:
    """
    单只股票同步结果，用于协程 worker 和批次汇总。
    """

    index: int
    total: int
    code: str
    name: Optional[str]
    affected: int = 0
    error: Optional[str] = None


async def _load_a_stock_trade_dates(reference_yyyymmdd: str) -> tuple[str, ...]:
    """
    从东方财富上证指数日 K 推导参考日期附近的 A 股交易日，并缓存到当前进程内。

    Returns:
        升序排列的交易日元组，日期格式为 YYYY-MM-DD。

    Raises:
        RuntimeError:
            当东方财富返回空数据时抛出。scheduler 会记录异常，避免静默错误。
    """

    reference_date = datetime.strptime(reference_yyyymmdd, "%Y%m%d").date()
    start_yyyymmdd = (
        reference_date - timedelta(days=STOCK_DAILY_TRADE_CALENDAR_LOOKBACK_DAYS)
    ).strftime("%Y%m%d")

    crawler = StockDailyDetailCrawler(max_retry=1)
    try:
        try:
            trade_dates = await crawler.fetch_trade_dates(
                start_date=start_yyyymmdd,
                end_date=reference_yyyymmdd,
            )
        except Exception as calendar_error:
            logger.warning(
                "eastmoney_trade_calendar_unavailable error=%s "
                "fallback=stock_list_latest_trade_date",
                repr(calendar_error),
            )
            crawler.max_retry = 3
            stock_df = await crawler.fetch_stock_list()
            latest_trade_date = stock_df.attrs.get("latest_trade_date")
            reference_trade_date = reference_date.strftime("%Y-%m-%d")
            if (
                isinstance(latest_trade_date, str)
                and latest_trade_date <= reference_trade_date
            ):
                logger.warning(
                    "eastmoney_trade_calendar_fallback_success "
                    "reference_date=%s latest_trade_date=%s",
                    reference_trade_date,
                    latest_trade_date,
                )
                return (latest_trade_date,)
            raise RuntimeError(
                "EastMoney trade calendar and stock-list fallback failed, "
                f"latest_trade_date={latest_trade_date!r}"
            ) from calendar_error
    finally:
        await crawler.close()

    if not trade_dates:
        raise RuntimeError(
            "EastMoney trade calendar returned no dates, "
            f"start_date={start_yyyymmdd}, end_date={reference_yyyymmdd}"
        )

    return tuple(trade_dates)


async def resolve_a_stock_target_trade_date(
    reference_yyyymmdd: str,
) -> StockTradeDateDecision:
    """
    基于 A 股交易日历解析当前应该同步的目标交易日。

    规则：
    - 如果 reference_yyyymmdd 是 A 股交易日，目标日期就是它自己；
    - 如果不是交易日，目标日期回退到小于 reference 的最近一个交易日。

    Args:
        reference_yyyymmdd:
            参考日期，格式 YYYYMMDD，通常是今天或 STOCK_DAILY_DEFAULT_END_DATE。

    Returns:
        StockTradeDateDecision，包含参考日期、目标交易日和是否交易日。
    """

    reference_trade_date = datetime.strptime(reference_yyyymmdd, "%Y%m%d").strftime(
        "%Y-%m-%d"
    )
    trade_dates = await _load_a_stock_trade_dates(reference_yyyymmdd)
    is_reference_trade_day = reference_trade_date in trade_dates

    if is_reference_trade_day:
        target_trade_date = reference_trade_date
    else:
        previous_trade_dates = [
            trade_date
            for trade_date in trade_dates
            if trade_date < reference_trade_date
        ]

        if not previous_trade_dates:
            raise RuntimeError(
                "EastMoney trade calendar has no previous trade date before "
                f"{reference_trade_date}"
            )

        target_trade_date = previous_trade_dates[-1]

    target_yyyymmdd = datetime.strptime(target_trade_date, "%Y-%m-%d").strftime(
        "%Y%m%d"
    )

    return StockTradeDateDecision(
        reference_yyyymmdd=reference_yyyymmdd,
        reference_trade_date=reference_trade_date,
        target_yyyymmdd=target_yyyymmdd,
        target_trade_date=target_trade_date,
        is_reference_trade_day=is_reference_trade_day,
    )


class StockDailyDetailService:
    """
    股票详细日线同步服务。

    职责：
    1. 建立 Mongo 连接和索引；
    2. 调 crawler 获取日线、技术指标和筹码；
    3. 批量 upsert 到 stock_daily_detail 集合；
    4. 给 scheduler 提供全市场同步入口。
    """

    def __init__(
        self,
        mongo_uri: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_name: str = "stock_daily_detail",
        sync_run_collection_name: str = "stock_daily_detail_sync_runs",
        request_sleep_seconds: Optional[float] = None,
        max_retry: Optional[int] = None,
        concurrency: Optional[int] = None,
        page_concurrency: Optional[int] = None,
        proxy_minutes: Optional[int] = None,
        proxy_pool_size: Optional[int] = None,
        proxy_concurrency_per_ip: Optional[int] = None,
    ) -> None:
        """
        初始化日线同步服务。

        Args:
            mongo_uri:
                MongoDB 连接地址；不传时读取 Settings.mongo_uri。
            db_name:
                MongoDB 数据库名；不传时读取 Settings.mongo_db_name。
            collection_name:
                日线详情集合名，默认 stock_daily_detail。
            sync_run_collection_name:
                同步批次状态集合名，默认 stock_daily_detail_sync_runs。
            request_sleep_seconds:
                每只股票处理完成后的基础休眠秒数；不传时使用模块常量
                STOCK_DAILY_REQUEST_SLEEP_SECONDS。
            max_retry:
                crawler 请求东方财富页面或接口的最大重试次数；不传时使用模块常量
                STOCK_DAILY_MAX_RETRY。
            concurrency:
                全市场同步时的协程 worker 数；不传时使用
                STOCK_DAILY_DEFAULT_CONCURRENCY。
            page_concurrency:
                同时打开东方财富网页的上限。其余 worker 在异步队列中等待。
            proxy_minutes:
                代理 IP 的购买时长，单位为分钟。
            proxy_pool_size:
                单次最多维护的代理 IP 数。小批次会按页面并发自动缩小。
            proxy_concurrency_per_ip:
                每个代理 IP 同时承载的页面数。
        Side effects:
            会创建一个同步 MongoClient，并初始化 StockDailyDetailCrawler。
        """

        settings = get_settings()

        self.mongo_uri = mongo_uri or settings.mongo_uri
        self.db_name = db_name or settings.mongo_db_name
        self.collection_name = collection_name
        self.sync_run_collection_name = sync_run_collection_name

        self.client: MongoClient = MongoClient(self.mongo_uri)
        self.db: Database = self.client[self.db_name]
        self.collection: Collection = self.db[self.collection_name]
        self.sync_run_collection: Collection = self.db[self.sync_run_collection_name]

        self.request_sleep_seconds = (
            request_sleep_seconds
            if request_sleep_seconds is not None
            else STOCK_DAILY_REQUEST_SLEEP_SECONDS
        )
        self.max_retry = max_retry if max_retry is not None else STOCK_DAILY_MAX_RETRY
        self.concurrency = (
            max(1, concurrency)
            if concurrency is not None
            else STOCK_DAILY_DEFAULT_CONCURRENCY
        )
        self.page_concurrency = (
            max(1, page_concurrency)
            if page_concurrency is not None
            else STOCK_DAILY_PAGE_CONCURRENCY
        )
        self.proxy_minutes = (
            max(1, proxy_minutes)
            if proxy_minutes is not None
            else STOCK_DAILY_PROXY_MINUTES
        )
        self.proxy_pool_size = (
            max(1, proxy_pool_size)
            if proxy_pool_size is not None
            else STOCK_DAILY_PROXY_POOL_SIZE
        )
        self.proxy_concurrency_per_ip = (
            max(1, proxy_concurrency_per_ip)
            if proxy_concurrency_per_ip is not None
            else STOCK_DAILY_PROXY_CONCURRENCY_PER_IP
        )
        self.proxy_rate_limiter = AsyncRequestRateLimiter(max_calls_per_second=3.0)
        self.proxy_pool_stats_history: list[dict[str, int]] = []

        self.crawler = StockDailyDetailCrawler(
            request_sleep_seconds=self.request_sleep_seconds,
            max_retry=self.max_retry,
            proxy_rate_limiter=self.proxy_rate_limiter,
        )

    def ensure_indexes(self) -> None:
        """
        确保 stock_daily_detail 集合索引存在。

        创建的索引：
        - uniq_code_trade_date_adjust：唯一索引，保证同一股票、同一交易日、同一
          复权口径只保留一条记录；
        - idx_trade_date_code：支持按交易日查看全市场；
        - idx_code_trade_date_int：支持按单只股票时间序列查询。

        MongoDB create_index 是幂等操作，重复调用不会重复创建同名索引。
        """

        self.collection.create_index(
            [
                ("code", ASCENDING),
                ("trade_date", ASCENDING),
                ("adjust", ASCENDING),
            ],
            unique=True,
            name="uniq_code_trade_date_adjust",
        )
        self.collection.create_index(
            [
                ("trade_date", ASCENDING),
                ("code", ASCENDING),
            ],
            name="idx_trade_date_code",
        )
        self.collection.create_index(
            [
                ("code", ASCENDING),
                ("trade_date_int", ASCENDING),
            ],
            name="idx_code_trade_date_int",
        )
        self.sync_run_collection.create_index("run_id", unique=True, name="uniq_run_id")
        self.sync_run_collection.create_index(
            [
                ("target_trade_date", ASCENDING),
                ("adjust", ASCENDING),
                ("scope_key", ASCENDING),
                ("status", ASCENDING),
            ],
            name="idx_target_adjust_scope_status",
        )
        self.sync_run_collection.create_index(
            [
                ("started_at", ASCENDING),
            ],
            name="idx_started_at",
        )

    def _build_scope_key(
        self,
        *,
        only_code: Optional[str],
        limit: Optional[int],
        offset: int = 0,
    ) -> str:
        """
        生成同步范围标识。

        scope_key 会写入 sync run，用于判断某个范围是否已经成功同步：
        - code:000001 表示只同步单只股票；
        - offset:100:limit:100 表示跳过前 100 只后同步 100 只；
        - limit:10 表示只同步股票列表前 10 只；
        - all 表示全市场。
        """

        if only_code:
            return f"code:{self._normalize_code(only_code)}"

        if limit is not None and limit > 0:
            if offset > 0:
                return f"offset:{offset}:limit:{limit}"

            return f"limit:{limit}"

        if offset > 0:
            return f"offset:{offset}"

        return "all"

    @staticmethod
    def _single_stock_dataframe(
        code: str,
        name: Optional[str],
    ) -> pd.DataFrame:
        return pd.DataFrame([{"代码": code, "名称": name}])

    def _load_existing_stock_list(self) -> pd.DataFrame:
        """Use the latest stored name for each code when the stock-list API fails."""

        pipeline = [
            {"$sort": {"trade_date_int": -1}},
            {"$group": {"_id": "$code", "name": {"$first": "$name"}}},
            {"$project": {"_id": 0, "代码": "$_id", "名称": "$name"}},
            {"$sort": {"代码": 1}},
        ]
        return pd.DataFrame(list(self.collection.aggregate(pipeline)))

    def _build_run_id(
        self,
        *,
        target_trade_date: str,
        adjust: str,
        run_mode: str,
        scope_key: str,
    ) -> str:
        """
        生成本轮同步批次 ID。

        同一个目标交易日、复权口径、运行模式和同步范围会得到稳定前缀，再拼接短
        uuid，避免重复启动时 Mongo 唯一索引冲突。
        """

        safe_scope_key = scope_key.replace(":", "_")
        return f"{target_trade_date}_{adjust}_{run_mode}_{safe_scope_key}_{uuid4().hex[:8]}"

    def _start_sync_run(
        self,
        *,
        run_id: str,
        target_trade_date: str,
        adjust: str,
        run_mode: str,
        scope_key: str,
        start_date: str,
        end_date: str,
        expected_count: int,
        only_code: Optional[str],
        limit: Optional[int],
        parent_run_id: Optional[str],
        offset: int = 0,
    ) -> None:
        """
        写入 running 状态的同步批次记录。

        这条记录用于启动后判断是否已有完整同步、同步过程中观察进度，以及失败后
        定位需要补偿的股票。
        """

        now = now_cn()
        self.sync_run_collection.update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "run_id": run_id,
                    "target_trade_date": target_trade_date,
                    "adjust": adjust,
                    "run_mode": run_mode,
                    "scope_key": scope_key,
                    "scope": {
                        "only_code": self._normalize_code(only_code)
                        if only_code
                        else None,
                        "limit": limit,
                        "offset": offset,
                    },
                    "start_date": start_date,
                    "end_date": end_date,
                    "status": SYNC_STATUS_RUNNING,
                    "expected_count": expected_count,
                    "success_count": 0,
                    "failed_count": 0,
                    "affected_total": 0,
                    "failed_items": [],
                    "parent_run_id": parent_run_id,
                    "started_at": now,
                    "updated_at": now,
                    "finished_at": None,
                }
            },
            upsert=True,
        )

    def _finish_sync_run(self, result: StockDailyDetailSyncResult) -> None:
        """
        把同步批次从 running 更新为最终状态。
        """

        now = now_cn()
        self.sync_run_collection.update_one(
            {"run_id": result.run_id},
            {
                "$set": {
                    "status": result.status,
                    "expected_count": result.expected_count,
                    "success_count": result.success_count,
                    "failed_count": result.failed_count,
                    "affected_total": result.affected_total,
                    "failed_items": result.failed_items,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
        )

    @staticmethod
    def _decide_run_status(
        *,
        expected_count: int,
        success_count: int,
        failed_count: int,
    ) -> str:
        """
        根据成功/失败数量决定同步批次最终状态。
        """

        if expected_count == 0:
            return SYNC_STATUS_FAILED

        if failed_count == 0:
            return SYNC_STATUS_SUCCESS

        if success_count == 0:
            return SYNC_STATUS_FAILED

        return SYNC_STATUS_PARTIAL_FAILED

    def has_successful_sync_run(
        self,
        *,
        target_trade_date: str,
        adjust: str,
        only_code: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> bool:
        """
        判断某个目标交易日、复权口径和同步范围是否已经完整成功。

        这是 scheduler 启动跳过和非交易日回看上一个交易日时使用的精确判断。
        """

        scope_key = self._build_scope_key(
            only_code=only_code,
            limit=limit,
            offset=offset,
        )
        doc = self.sync_run_collection.find_one(
            {
                "target_trade_date": target_trade_date,
                "adjust": adjust,
                "scope_key": scope_key,
                "status": SYNC_STATUS_SUCCESS,
            },
            projection={"_id": 1},
        )
        return doc is not None

    async def sync_all(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: Optional[str] = None,
        limit: Optional[int] = None,
        only_code: Optional[str] = None,
        run_mode: str = "manual",
        target_trade_date: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        offset: int = 0,
    ) -> StockDailyDetailSyncResult:
        """
        同步全市场股票详细日线数据。

        start_date/end_date 格式为 YYYYMMDD。增量同步会从写入起点向前读取预热
        窗口，用于计算长周期指标，但只写入目标日期范围。

        Args:
            start_date:
                首次同步时的默认起始日期；为空时使用模块常量
                STOCK_DAILY_DEFAULT_START_DATE。
            end_date:
                同步结束日期；为空时优先使用模块常量
                STOCK_DAILY_DEFAULT_END_DATE，再默认今天。
            adjust:
                复权口径；为空时使用模块常量 STOCK_DAILY_DEFAULT_ADJUST。
            limit:
                只处理股票列表前 N 只，常用于小范围测试。
            only_code:
                只同步某一只股票，常用于本地验证或问题补偿。
            offset:
                跳过股票列表前 N 只，配合 limit 做历史分批补齐。
            run_mode:
                本轮同步来源，例如 startup、scheduled、manual、retry。
            target_trade_date:
                本轮同步对应的目标交易日，格式 YYYY-MM-DD。不传时由 end_date 转换。
            parent_run_id:
                失败补偿时记录原始失败 run_id。

        Returns:
            StockDailyDetailSyncResult，包含 expected/success/failed/affected 等统计。

        Side effects:
            会访问东方财富上游接口，并对 MongoDB 执行批量 upsert。
            同时会写入 stock_daily_detail_sync_runs 批次状态集合。
        """

        start_date = start_date or STOCK_DAILY_DEFAULT_START_DATE
        end_date = (
            end_date
            or STOCK_DAILY_DEFAULT_END_DATE
            or datetime.now(CN_TZ).strftime("%Y%m%d")
        )
        adjust = adjust if adjust is not None else STOCK_DAILY_DEFAULT_ADJUST
        target_trade_date = target_trade_date or self._yyyymmdd_to_trade_date(end_date)

        self.ensure_indexes()
        if only_code:
            only_code = self._normalize_code(only_code)
            existing = self.collection.find_one(
                {"code": only_code},
                projection={"_id": 0, "name": 1},
                sort=[("trade_date", -1)],
            )
            stock_df = self._single_stock_dataframe(
                only_code,
                existing.get("name") if existing else None,
            )
        else:
            try:
                active_trade_date = (
                    target_trade_date if run_mode in {"startup", "scheduled"} else None
                )
                stock_df = await self.crawler.fetch_stock_list(
                    target_trade_date=active_trade_date,
                )
            except Exception:
                if run_mode in {"startup", "scheduled"}:
                    logger.exception(
                        "active_stock_list_fetch_failed; daily sync aborted"
                    )
                    raise
                logger.exception(
                    "stock_list_fetch_failed; falling back to existing MongoDB universe"
                )
                stock_df = self._load_existing_stock_list()
                if stock_df.empty:
                    raise

        if not only_code and offset > 0:
            stock_df = stock_df.iloc[offset:]

        if limit is not None and limit > 0:
            stock_df = stock_df.head(limit)

        total = len(stock_df)
        scope_key = self._build_scope_key(
            only_code=only_code,
            limit=limit,
            offset=offset,
        )
        run_id = self._build_run_id(
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode=run_mode,
            scope_key=scope_key,
        )
        self._start_sync_run(
            run_id=run_id,
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode=run_mode,
            scope_key=scope_key,
            start_date=start_date,
            end_date=end_date,
            expected_count=total,
            only_code=only_code,
            limit=limit,
            parent_run_id=parent_run_id,
            offset=offset,
        )

        logger.info(
            (
                "stock_daily_detail_sync_start run_id=%s total=%s start_date=%s "
                "end_date=%s target_trade_date=%s adjust=%s workers=%s "
                "page_concurrency=%s"
            ),
            run_id,
            total,
            start_date,
            end_date,
            target_trade_date,
            adjust,
            self.concurrency,
            self.page_concurrency,
        )

        success_count = 0
        failed_count = 0
        affected_total = 0
        failed_items: list[dict[str, Any]] = []

        stock_rows = [
            (index, self._normalize_code(row["代码"]), row.get("名称"))
            for index, row in enumerate(stock_df.to_dict("records"), start=1)
        ]
        item_results = await self._sync_stock_rows(
            stock_rows=stock_rows,
            total=total,
            default_start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            target_trade_date=target_trade_date,
        )
        for item_result in item_results:
            if item_result.error is None:
                success_count += 1
                affected_total += item_result.affected
                continue

            failed_count += 1
            failed_items.append(
                {
                    "code": item_result.code,
                    "name": item_result.name,
                    "error": item_result.error[:1000],
                }
            )

        status = self._decide_run_status(
            expected_count=total,
            success_count=success_count,
            failed_count=failed_count,
        )
        result = StockDailyDetailSyncResult(
            run_id=run_id,
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode=run_mode,
            scope_key=scope_key,
            expected_count=total,
            success_count=success_count,
            failed_count=failed_count,
            affected_total=affected_total,
            status=status,
            failed_items=failed_items,
        )
        self._finish_sync_run(result)

        logger.info(
            (
                "stock_daily_detail_sync_finished run_id=%s status=%s total=%s "
                "success=%s failed=%s affected_total=%s"
            ),
            run_id,
            status,
            total,
            success_count,
            failed_count,
            affected_total,
        )
        return result

    async def _sync_stock_rows(
        self,
        *,
        stock_rows: list[tuple[int, str, Optional[str]]],
        total: int,
        default_start_date: str,
        end_date: str,
        adjust: str,
        target_trade_date: str,
        retry_failed_once: bool = True,
    ) -> list[StockDailyDetailItemResult]:
        """Process the stock queue with coroutines sharing one browser."""

        if not stock_rows:
            return []

        queue: asyncio.Queue[tuple[int, str, Optional[str]]] = asyncio.Queue()
        for stock_row in stock_rows:
            queue.put_nowait(stock_row)

        shared_fetcher = EastMoneyQuotePageFetcher()
        active_page_capacity = min(self.page_concurrency, len(stock_rows))
        required_proxy_count = (
            active_page_capacity + self.proxy_concurrency_per_ip - 1
        ) // self.proxy_concurrency_per_ip
        proxy_pool_size = min(self.proxy_pool_size, max(1, required_proxy_count))
        shared_proxy_provider = AsyncShanchenProxyPool(
            minutes=self.proxy_minutes,
            pool_size=proxy_pool_size,
            max_concurrency_per_proxy=self.proxy_concurrency_per_ip,
            rate_limiter=self.proxy_rate_limiter,
        )
        page_semaphore = asyncio.Semaphore(self.page_concurrency)
        local_circuit_breaker = LocalQuoteCircuitBreaker()
        crawler_count = min(self.page_concurrency * 2, len(stock_rows))
        crawlers = [
            StockDailyDetailCrawler(
                request_sleep_seconds=self.request_sleep_seconds,
                max_retry=self.max_retry,
                proxy_provider=shared_proxy_provider,
                quote_page_fetcher=shared_fetcher,
                page_semaphore=page_semaphore,
                local_circuit_breaker=local_circuit_breaker,
                proxy_rate_limiter=self.proxy_rate_limiter,
            )
            for _ in range(crawler_count)
        ]
        crawler_pool: asyncio.Queue[StockDailyDetailCrawler] = asyncio.Queue()
        for crawler in crawlers:
            crawler_pool.put_nowait(crawler)
        results: list[StockDailyDetailItemResult] = []

        async def worker() -> None:
            while True:
                try:
                    index, code, name = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                worker_crawler = await crawler_pool.get()
                try:
                    try:
                        affected = await self._sync_one_with_crawler(
                            crawler=worker_crawler,
                            code=code,
                            name=name,
                            default_start_date=default_start_date,
                            end_date=end_date,
                            adjust=adjust,
                            target_trade_date=target_trade_date,
                        )
                        item_result = StockDailyDetailItemResult(
                            index=index,
                            total=total,
                            code=code,
                            name=name,
                            affected=affected,
                        )
                        logger.info(
                            "stock_daily_detail_sync_one_success index=%s/%s "
                            "code=%s name=%s affected=%s",
                            index,
                            total,
                            code,
                            name,
                            affected,
                        )
                    except Exception as exc:
                        item_result = StockDailyDetailItemResult(
                            index=index,
                            total=total,
                            code=code,
                            name=name,
                            error=str(exc) or exc.__class__.__name__,
                        )
                        logger.error(
                            "stock_daily_detail_sync_one_failed index=%s/%s "
                            "code=%s name=%s error=%s",
                            index,
                            total,
                            code,
                            name,
                            item_result.error,
                        )
                    results.append(item_result)
                    await worker_crawler.sleep_after_request()
                finally:
                    crawler_pool.put_nowait(worker_crawler)
                    queue.task_done()

        worker_count = min(self.concurrency, len(stock_rows))
        try:
            await asyncio.gather(*(worker() for _ in range(worker_count)))
        finally:
            for crawler in crawlers:
                try:
                    await crawler.close()
                except Exception:
                    logger.exception("stock_daily_detail_worker_crawler_close_failed")
            proxy_pool_stats = dict(vars(shared_proxy_provider.stats))
            self.proxy_pool_stats_history.append(proxy_pool_stats)
            logger.info(
                "stock_daily_proxy_pool_stats minutes=%s pool_size=%s "
                "concurrency_per_ip=%s stats=%s",
                self.proxy_minutes,
                proxy_pool_size,
                self.proxy_concurrency_per_ip,
                proxy_pool_stats,
            )
            await shared_proxy_provider.close()
            await shared_fetcher.close()

        sorted_results = sorted(results, key=lambda item: item.index)
        if not retry_failed_once:
            return sorted_results

        retry_rows = [
            (item.index, item.code, item.name)
            for item in sorted_results
            if item.error and self._is_retryable_item_error(item.error)
        ]
        if not retry_rows:
            return sorted_results

        logger.info(
            "stock_daily_detail_retry_pass_start retry_count=%s total=%s",
            len(retry_rows),
            total,
        )
        retry_results = await self._sync_stock_rows(
            stock_rows=retry_rows,
            total=total,
            default_start_date=default_start_date,
            end_date=end_date,
            adjust=adjust,
            target_trade_date=target_trade_date,
            retry_failed_once=False,
        )
        retry_by_index = {item.index: item for item in retry_results}
        merged_results = [
            retry_by_index.get(item.index, item) for item in sorted_results
        ]
        logger.info(
            "stock_daily_detail_retry_pass_finished retried=%s recovered=%s",
            len(retry_results),
            sum(item.error is None for item in retry_results),
        )
        return merged_results

    @staticmethod
    def _is_retryable_item_error(error: str) -> bool:
        normalized_error = error.lower()
        retryable_tokens = (
            "timeout",
            "proxyunavailableerror",
            "net::err_",
            "connectionerror",
            "server disconnected",
        )
        return any(token in normalized_error for token in retryable_tokens)

    async def sync_one(
        self,
        code: str,
        name: Optional[str],
        default_start_date: str,
        end_date: str,
        adjust: str = "qfq",
        target_trade_date: Optional[str] = None,
    ) -> int:
        """
        同步单只股票的日线详情。

        增量同步只读取并写入最新已入库日期之后的目标区间。

        Args:
            code:
                股票代码，会标准化为 6 位。
            name:
                股票名称，写入模型的 name 字段。
            default_start_date:
                首次同步时的起始日期，格式 YYYYMMDD。
            end_date:
                同步结束日期，格式 YYYYMMDD。
            adjust:
                复权口径。

        Returns:
            本次 bulk_upsert 影响的文档数量，包含新增和修改。
        """

        return await self._sync_one_with_crawler(
            crawler=self.crawler,
            code=code,
            name=name,
            default_start_date=default_start_date,
            end_date=end_date,
            adjust=adjust,
            target_trade_date=target_trade_date,
        )

    async def _sync_one_with_crawler(
        self,
        *,
        crawler: StockDailyDetailCrawler,
        code: str,
        name: Optional[str],
        default_start_date: str,
        end_date: str,
        adjust: str,
        target_trade_date: Optional[str],
    ) -> int:
        code = self._normalize_code(code)
        latest_trade_date = self.get_latest_trade_date(code=code, adjust=adjust)

        if latest_trade_date:
            write_start_date = latest_trade_date
        else:
            write_start_date = self._yyyymmdd_to_trade_date(default_start_date)

        read_start_date = self._date_str_to_yyyymmdd(write_start_date)
        items = await crawler.build_stock_daily_details(
            code=code,
            name=name,
            start_date=read_start_date,
            end_date=end_date,
            adjust=adjust,
            write_start_date=write_start_date,
        )

        if not items:
            raise RuntimeError(
                f"eastmoney returned no stock daily detail items, code={code}"
            )

        if target_trade_date:
            self._validate_sync_items_for_target_date(
                items,
                target_trade_date=target_trade_date,
                code=code,
            )

        return self.bulk_upsert(items)

    def _validate_sync_items_for_target_date(
        self,
        items: Iterable[StockDailyDetail],
        *,
        target_trade_date: str,
        code: str,
    ) -> None:
        target_item = next(
            (item for item in items if item.trade_date == target_trade_date),
            None,
        )
        if target_item is None:
            raise RuntimeError(
                "target trade date missing from eastmoney K-line data, "
                f"code={code}, target_trade_date={target_trade_date}"
            )

        missing_fields = self._missing_required_fields(target_item)
        if missing_fields:
            raise RuntimeError(
                "target trade date stock daily detail incomplete, "
                f"code={code}, target_trade_date={target_trade_date}, "
                f"missing_fields={missing_fields}"
            )

    def _missing_required_fields(
        self,
        item: StockDailyDetail,
    ) -> list[str]:
        required_values: list[tuple[str, Any]] = [
            ("open", item.open),
            ("close", item.close),
            ("high", item.high),
            ("low", item.low),
            ("volume", item.volume),
            ("amount", item.amount),
            ("pct_chg", item.pct_chg),
            ("change_amount", item.change_amount),
            ("turnover_pct", item.turnover_pct),
            ("chip", item.chip),
        ]

        indicator_values = (
            item.ma.ma5,
            item.ma.ma10,
            item.ma.ma20,
            item.ma.ma30,
            item.ma.ma60,
            item.volume_ma.vol_ma5,
            item.volume_ma.vol_ma10,
            item.macd.dif,
            item.kdj.k,
            item.rsi.rsi6,
            item.boll.mid,
            item.cci.cci14,
            item.wr.wr6,
            item.wr.wr10,
        )
        if not any(value is not None for value in indicator_values):
            required_values.append(("page_indicators", None))
        if item.source.daily != "eastmoney.quote_page":
            required_values.append(("source.daily", None))
        if item.source.indicator != "eastmoney.quote_page.runtime":
            required_values.append(("source.indicator", None))

        if item.chip is not None:
            if item.source.chip != "eastmoney.quote_page.runtime":
                required_values.append(("source.chip", None))
            required_values.extend(
                [
                    ("chip.profit_ratio", item.chip.profit_ratio),
                    ("chip.avg_cost", item.chip.avg_cost),
                    ("chip.cost_90.low", item.chip.cost_90.low),
                    ("chip.cost_90.high", item.chip.cost_90.high),
                    ("chip.cost_90.concentration", item.chip.cost_90.concentration),
                    ("chip.cost_70.low", item.chip.cost_70.low),
                    ("chip.cost_70.high", item.chip.cost_70.high),
                    ("chip.cost_70.concentration", item.chip.cost_70.concentration),
                ]
            )

            if item.chip.chart is None:
                required_values.append(("chip.chart", None))
            else:
                if not item.chip.chart.x:
                    required_values.append(("chip.chart.x", None))
                if not item.chip.chart.y:
                    required_values.append(("chip.chart.y", None))

        return [name for name, value in required_values if value is None]

    def bulk_upsert(
        self,
        items: Iterable[StockDailyDetail],
        batch_size: int = 1000,
    ) -> int:
        """
        批量 upsert 股票日线详情模型。

        Args:
            items:
                待写入的 StockDailyDetail 模型序列。
            batch_size:
                每批 MongoDB bulk_write 的操作数量，默认 1000。

        Returns:
            MongoDB 报告的 upserted_count + modified_count。

        Side effects:
            会写入 stock_daily_detail 集合。
        """

        batch: List[StockDailyDetail] = []
        total_affected = 0

        for item in items:
            batch.append(item)

            if len(batch) >= batch_size:
                total_affected += self._flush_items(batch)
                batch.clear()

        if batch:
            total_affected += self._flush_items(batch)

        return total_affected

    def get_latest_trade_date(
        self,
        code: str,
        adjust: str = "qfq",
    ) -> Optional[str]:
        """
        查询某只股票在指定复权口径下已入库的最新交易日。

        Args:
            code:
                股票代码，会标准化为 6 位。
            adjust:
                复权口径。

        Returns:
            最新 trade_date，格式 YYYY-MM-DD；没有数据时返回 None。
        """

        doc = self.collection.find_one(
            {
                "code": self._normalize_code(code),
                "adjust": adjust,
            },
            projection={
                "_id": 0,
                "trade_date": 1,
            },
            sort=[
                ("trade_date", -1),
            ],
        )

        if not doc:
            return None

        return doc.get("trade_date")

    async def retry_failed_run(self, run_id: str) -> StockDailyDetailSyncResult:
        """
        只重试某个同步批次中失败的股票。

        Args:
            run_id:
                原始同步批次 ID。

        Returns:
            新的 retry 批次结果。retry 会创建一条新的 sync run，并用 parent_run_id
            关联原始失败批次。

        Raises:
            ValueError:
                找不到 run，或者该 run 没有 failed_items 时抛出。
        """

        source_run = self.sync_run_collection.find_one(
            {"run_id": run_id}, projection={"_id": 0}
        )

        if not source_run:
            raise ValueError(f"未找到同步批次: {run_id}")

        failed_items = source_run.get("failed_items") or []
        if not failed_items:
            raise ValueError(f"同步批次没有失败股票，无需补偿: {run_id}")

        target_trade_date = str(source_run["target_trade_date"])
        end_date = str(
            source_run.get("end_date") or self._date_str_to_yyyymmdd(target_trade_date)
        )
        start_date = str(source_run.get("start_date") or end_date)
        adjust = str(source_run.get("adjust") or "qfq")

        self.ensure_indexes()
        total = len(failed_items)
        scope_key = f"retry:{run_id}"
        retry_run_id = self._build_run_id(
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode="retry",
            scope_key=scope_key,
        )
        self._start_sync_run(
            run_id=retry_run_id,
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode="retry",
            scope_key=scope_key,
            start_date=start_date,
            end_date=end_date,
            expected_count=total,
            only_code=None,
            limit=None,
            parent_run_id=run_id,
        )

        success_count = 0
        failed_count = 0
        affected_total = 0
        retry_failed_items: list[dict[str, Any]] = []

        for index, item in enumerate(failed_items, start=1):
            code = self._normalize_code(item.get("code"))
            name = item.get("name")

            try:
                affected = await self.sync_one(
                    code=code,
                    name=name,
                    default_start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                    target_trade_date=target_trade_date,
                )
                success_count += 1
                affected_total += affected
                logger.info(
                    "stock_daily_detail_retry_one_success index=%s/%s code=%s name=%s affected=%s",
                    index,
                    total,
                    code,
                    name,
                    affected,
                )
            except Exception as exc:
                failed_count += 1
                retry_failed_items.append(
                    {
                        "code": code,
                        "name": name,
                        "error": (str(exc) or exc.__class__.__name__)[:1000],
                    }
                )
                logger.exception(
                    "stock_daily_detail_retry_one_failed index=%s/%s code=%s name=%s error=%s",
                    index,
                    total,
                    code,
                    name,
                    repr(exc),
                )

            await self.crawler.sleep_after_request()

        status = self._decide_run_status(
            expected_count=total,
            success_count=success_count,
            failed_count=failed_count,
        )
        result = StockDailyDetailSyncResult(
            run_id=retry_run_id,
            target_trade_date=target_trade_date,
            adjust=adjust,
            run_mode="retry",
            scope_key=scope_key,
            expected_count=total,
            success_count=success_count,
            failed_count=failed_count,
            affected_total=affected_total,
            status=status,
            failed_items=retry_failed_items,
        )
        self._finish_sync_run(result)
        return result

    def _stock_daily_key(
        self,
        item: StockDailyDetail,
    ) -> tuple[str, str, str]:
        """
        返回 stock_daily_detail 唯一键。
        """

        return (item.code, item.trade_date, item.adjust)

    def _load_existing_created_at_map(
        self,
        items: Iterable[StockDailyDetail],
    ) -> dict[tuple[str, str, str], datetime]:
        """
        批量查询已有记录的 created_at，整文档替换时保留首次入库时间。
        """

        filters = [
            {
                "code": code,
                "trade_date": trade_date,
                "adjust": adjust,
            }
            for code, trade_date, adjust in {
                self._stock_daily_key(item) for item in items
            }
        ]

        if not filters:
            return {}

        docs = self.collection.find(
            {"$or": filters},
            projection={
                "_id": 0,
                "code": 1,
                "trade_date": 1,
                "adjust": 1,
                "created_at": 1,
            },
        )

        result: dict[tuple[str, str, str], datetime] = {}
        for doc in docs:
            created_at = doc.get("created_at")

            if not isinstance(created_at, datetime):
                continue

            result[
                (
                    str(doc.get("code")),
                    str(doc.get("trade_date")),
                    str(doc.get("adjust")),
                )
            ] = created_at

        return result

    def _dump_stock_daily_detail_doc(
        self,
        item: StockDailyDetail,
        *,
        created_at: datetime,
        updated_at: datetime,
    ) -> SON:
        """
        按 StockDailyDetail 模型字段声明顺序生成 MongoDB 文档。
        """

        data = item.model_dump(mode="python", exclude_none=False)
        data["created_at"] = created_at
        data["updated_at"] = updated_at

        return SON(
            (field_name, data[field_name])
            for field_name in StockDailyDetail.model_fields
            if field_name in data
        )

    def _build_replace_one(
        self,
        item: StockDailyDetail,
        *,
        created_at: datetime,
        updated_at: datetime,
    ) -> ReplaceOne:
        """
        把单条模型转换成 MongoDB ReplaceOne 操作。

        写入策略：
        - filter 使用 code + trade_date + adjust；
        - replacement 按 StockDailyDetail 模型字段顺序写入完整文档；
        - 已有记录保留 created_at，新记录使用本次模型的 created_at。

        Args:
            item:
                待写入的日线详情模型。

        Returns:
            可交给 bulk_write 的 ReplaceOne 操作。
        """

        doc = self._dump_stock_daily_detail_doc(
            item,
            created_at=created_at,
            updated_at=updated_at,
        )

        return ReplaceOne(
            {
                "code": doc["code"],
                "trade_date": doc["trade_date"],
                "adjust": doc["adjust"],
            },
            doc,
            upsert=True,
        )

    def _flush_items(
        self,
        items: List[StockDailyDetail],
    ) -> int:
        """
        按模型字段顺序执行一批 MongoDB 整文档替换。

        Args:
            items:
                待写入的 StockDailyDetail 模型列表。

        Returns:
            upserted_count + modified_count；空列表返回 0。
        """

        if not items:
            return 0

        now = now_cn()
        existing_created_at_map = self._load_existing_created_at_map(items)
        operations: List[ReplaceOne] = []

        for item in items:
            item_key = self._stock_daily_key(item)
            created_at = existing_created_at_map.get(item_key) or item.created_at
            operations.append(
                self._build_replace_one(
                    item,
                    created_at=created_at,
                    updated_at=now,
                )
            )

        result = self.collection.bulk_write(operations, ordered=False)
        return int(result.upserted_count or 0) + int(result.modified_count or 0)

    async def close(self) -> None:
        """
        关闭当前 service 持有的 MongoDB client。

        run_stock_daily_detail_sync 这类入口会在 finally 中调用，避免连接泄漏。
        """

        await self.crawler.close()
        self.client.close()

    @staticmethod
    def _normalize_code(value: object) -> str:
        """
        把股票代码标准化为 6 位字符串。
        """

        return str(value).strip().zfill(6)

    @staticmethod
    def _minus_days(
        trade_date: str,
        days: int,
    ) -> str:
        """
        从 YYYY-MM-DD 日期向前减去指定自然日。

        用于计算增量同步的指标预热起点。
        """

        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        return (dt - timedelta(days=days)).strftime("%Y-%m-%d")

    @staticmethod
    def _date_str_to_yyyymmdd(
        trade_date: str,
    ) -> str:
        """
        把 YYYY-MM-DD 日期转换为东方财富需要的 YYYYMMDD。
        """

        return datetime.strptime(trade_date, "%Y-%m-%d").strftime("%Y%m%d")

    @staticmethod
    def _yyyymmdd_to_trade_date(
        trade_date: str,
    ) -> str:
        """
        把东方财富使用的 YYYYMMDD 日期转换为 MongoDB 文档中的 YYYY-MM-DD。
        """

        return datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d")


async def run_stock_daily_detail_sync(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    adjust: Optional[str] = None,
    limit: Optional[int] = None,
    only_code: Optional[str] = None,
    run_mode: str = "manual",
    target_trade_date: Optional[str] = None,
    offset: int = 0,
    concurrency: Optional[int] = None,
) -> StockDailyDetailSyncResult:
    """
    给脚本和 scheduler 调用的统一同步入口。

    这个函数负责创建 service、执行同步，并在 finally 中关闭 MongoDB 连接。
    外部调用者不需要关心 service 生命周期。
    """

    service = StockDailyDetailService(concurrency=concurrency)

    try:
        return await service.sync_all(
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            limit=limit,
            only_code=only_code,
            run_mode=run_mode,
            target_trade_date=target_trade_date,
            offset=offset,
        )
    finally:
        await service.close()


async def stock_daily_detail_has_successful_sync_run(
    target_trade_date: str,
    adjust: str = "qfq",
    only_code: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> bool:
    """
    给 scheduler 调用的成功批次检查入口。

    只有对应范围的同步批次状态为 success 时才返回 True。
    """

    service = StockDailyDetailService()

    try:
        service.ensure_indexes()
        return service.has_successful_sync_run(
            target_trade_date=target_trade_date,
            adjust=adjust,
            only_code=only_code,
            limit=limit,
            offset=offset,
        )
    finally:
        await service.close()


async def retry_failed_stock_daily_detail_run(
    run_id: str,
) -> StockDailyDetailSyncResult:
    """
    给脚本或手动命令调用的失败批次补偿入口。
    """

    service = StockDailyDetailService()

    try:
        return await service.retry_failed_run(run_id)
    finally:
        await service.close()
