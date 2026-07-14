from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import ClassVar, List, Optional

from pydantic import BaseModel, Field


CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    """
    返回当前北京时间。

    用途：
    - 给 created_at / updated_at 提供默认值；
    - 避免系统时区不同导致入库时间混乱。
    """

    return datetime.now(CN_TZ)


class MAIndicators(BaseModel):
    """
    收盘价移动平均线指标。

    直接读取东方财富网页图表运行时；网页未提供的周期保持 None。
    """

    ma5: Optional[float] = Field(default=None, description="5 日收盘价均线")
    ma10: Optional[float] = Field(default=None, description="10 日收盘价均线")
    ma20: Optional[float] = Field(default=None, description="20 日收盘价均线")
    ma30: Optional[float] = Field(default=None, description="30 日收盘价均线")
    ma60: Optional[float] = Field(default=None, description="60 日收盘价均线")
    ma120: Optional[float] = Field(default=None, description="120 日收盘价均线")
    ma250: Optional[float] = Field(default=None, description="250 日收盘价均线，近似年线")


class VolumeMAIndicators(BaseModel):
    """
    成交量移动平均线指标。

    volume 的原始单位沿用东方财富行情页日 K 数据：手。
    """

    vol_ma5: Optional[float] = Field(default=None, description="5 日成交量均线，单位：手")
    vol_ma10: Optional[float] = Field(default=None, description="10 日成交量均线，单位：手")
    vol_ma20: Optional[float] = Field(default=None, description="20 日成交量均线，单位：手")
    vol_ma60: Optional[float] = Field(default=None, description="60 日成交量均线，单位：手")


class MACDIndicators(BaseModel):
    """
    MACD 指标。

    字段直接对应东方财富网页显示的 DIF、DEA 和 MACD 柱值。
    """

    dif: Optional[float] = Field(default=None, description="DIF 快线")
    dea: Optional[float] = Field(default=None, description="DEA 慢线")
    hist: Optional[float] = Field(default=None, description="MACD 柱值")


class KDJIndicators(BaseModel):
    """
    KDJ 随机指标。

    用于观察短线超买、超卖和拐点变化。
    """

    k: Optional[float] = Field(default=None, description="K 值")
    d: Optional[float] = Field(default=None, description="D 值")
    j: Optional[float] = Field(default=None, description="J 值")


class RSIIndicators(BaseModel):
    """
    RSI 相对强弱指标。

    三个周期用于区分短线、中线和更平滑的动量状态。
    """

    rsi6: Optional[float] = Field(default=None, description="6 日 RSI")
    rsi12: Optional[float] = Field(default=None, description="12 日 RSI")
    rsi24: Optional[float] = Field(default=None, description="24 日 RSI")


class BOLLIndicators(BaseModel):
    """
    BOLL 布林线指标。

    mid 为中轨，upper/lower 为上下轨。
    """

    mid: Optional[float] = Field(default=None, description="布林线中轨")
    upper: Optional[float] = Field(default=None, description="布林线上轨")
    lower: Optional[float] = Field(default=None, description="布林线下轨")


class CCIIndicators(BaseModel):
    """
    CCI 顺势指标。
    """

    cci14: Optional[float] = Field(default=None, description="14 日 CCI")


class WRIndicators(BaseModel):
    """
    WR 威廉指标。

    用于观察短线超买超卖区间。
    """

    wr6: Optional[float] = Field(default=None, description="6 日 WR")
    wr10: Optional[float] = Field(default=None, description="10 日 WR")
    wr14: Optional[float] = Field(default=None, description="14 日 WR")


class ATRIndicators(BaseModel):
    """
    ATR 真实波幅指标。东方财富当前页面未公开时保持 None。
    """

    atr14: Optional[float] = Field(default=None, description="14 日 ATR，衡量价格波动幅度")


class ChipCostRange(BaseModel):
    """
    筹码成本区间。

    用于描述某个比例的筹码集中在哪个价格区间，以及集中程度。
    """

    low: Optional[float] = Field(default=None, description="成本区间下沿")
    high: Optional[float] = Field(default=None, description="成本区间上沿")
    concentration: Optional[float] = Field(default=None, description="筹码集中度")


class ChipChart(BaseModel):
    """
    筹码图曲线。

    x 为网页 Canvas 实际绘制宽度，y 为页面坐标对应的价格。
    """

    x: List[float] = Field(default_factory=list, description="网页筹码图横向绘制数据")
    y: List[float] = Field(default_factory=list, description="筹码图纵轴数据")


class ChipDistribution(BaseModel):
    """
    筹码分布信息。

    数据由东方财富概念页自己的筹码运行时生成，项目不实现筹码公式。每次抓取
    最多读取页面最近 90 个交易日，更早日线的 chip 字段可能为空。
    """

    profit_ratio: Optional[float] = Field(default=None, description="获利比例")
    avg_cost: Optional[float] = Field(default=None, description="平均成本")
    cost_90: ChipCostRange = Field(default_factory=ChipCostRange, description="90% 筹码成本区间")
    cost_70: ChipCostRange = Field(default_factory=ChipCostRange, description="70% 筹码成本区间")
    chart: Optional[ChipChart] = Field(default=None, description="筹码图曲线，可能为空")


class StockDailyDetailSource(BaseModel):
    """
    数据来源记录。

    用于以后排查字段来源、接口切换和结果复核。
    """

    daily: str = Field(default="eastmoney.quote_page", description="原始日线数据来源")
    page_url: Optional[str] = Field(default=None, description="东方财富行情页地址")
    network: Optional[str] = Field(
        default=None,
        description="行情页实际网络出口：local 或 proxy",
    )
    indicator: str = Field(
        default="eastmoney.quote_page.runtime",
        description="技术指标计算来源",
    )
    chip: str = Field(
        default="eastmoney.quote_page.runtime",
        description="筹码分布数据来源",
    )


class StockDailyDetail(BaseModel):
    """
    股票详细日线数据。

    唯一键建议：code + trade_date + adjust。
    """

    __tablename__: ClassVar[str] = "stock_daily_detail"  # MongoDB 集合名

    trade_date: str = Field(..., description="交易日期，YYYY-MM-DD")
    trade_date_int: int = Field(..., description="交易日期整数，YYYYMMDD")
    code: str = Field(..., description="股票代码，6 位")
    name: Optional[str] = Field(default=None, description="股票名称")
    adjust: str = Field(default="qfq", description="复权口径：'' / qfq / hfq")

    open: Optional[float] = Field(default=None, description="开盘价")
    close: Optional[float] = Field(default=None, description="收盘价")
    high: Optional[float] = Field(default=None, description="最高价")
    low: Optional[float] = Field(default=None, description="最低价")

    volume: Optional[int] = Field(default=None, description="成交量，单位：手")
    amount: Optional[float] = Field(default=None, description="成交额，单位：元")

    amplitude_pct: Optional[float] = Field(default=None, description="振幅，单位：%")
    pct_chg: Optional[float] = Field(default=None, description="涨跌幅，单位：%")
    change_amount: Optional[float] = Field(default=None, description="涨跌额，单位：元")
    turnover_pct: Optional[float] = Field(default=None, description="换手率，单位：%")

    ma: MAIndicators = Field(default_factory=MAIndicators, description="收盘价均线指标组")
    volume_ma: VolumeMAIndicators = Field(default_factory=VolumeMAIndicators, description="成交量均线指标组")
    macd: MACDIndicators = Field(default_factory=MACDIndicators, description="MACD 指标组")
    kdj: KDJIndicators = Field(default_factory=KDJIndicators, description="KDJ 指标组")
    rsi: RSIIndicators = Field(default_factory=RSIIndicators, description="RSI 指标组")
    boll: BOLLIndicators = Field(default_factory=BOLLIndicators, description="BOLL 布林线指标组")
    cci: CCIIndicators = Field(default_factory=CCIIndicators, description="CCI 指标组")
    wr: WRIndicators = Field(default_factory=WRIndicators, description="WR 威廉指标组")
    atr: ATRIndicators = Field(default_factory=ATRIndicators, description="ATR 真实波幅指标组")

    chip: Optional[ChipDistribution] = Field(default=None, description="筹码分布，可能为空")

    source: StockDailyDetailSource = Field(default_factory=StockDailyDetailSource, description="本条记录各类数据来源")
    created_at: datetime = Field(default_factory=now_cn, description="首次入库时间，北京时间")
    updated_at: datetime = Field(default_factory=now_cn, description="最近更新时间，北京时间")
