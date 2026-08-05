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

    按东方财富前端图表算法逆向还原；算法未提供的周期保持 None。
    """

    # 最近 5 个交易日收盘价的算术平均值。
    ma5: Optional[float] = Field(default=None, description="5 日收盘价均线")
    # 最近 10 个交易日收盘价的算术平均值。
    ma10: Optional[float] = Field(default=None, description="10 日收盘价均线")
    # 最近 20 个交易日收盘价的算术平均值。
    ma20: Optional[float] = Field(default=None, description="20 日收盘价均线")
    # 最近 30 个交易日收盘价的算术平均值。
    ma30: Optional[float] = Field(default=None, description="30 日收盘价均线")
    # 最近 60 个交易日收盘价的算术平均值。
    ma60: Optional[float] = Field(default=None, description="60 日收盘价均线")
    # 最近 120 个交易日收盘价的算术平均值。
    ma120: Optional[float] = Field(default=None, description="120 日收盘价均线")
    # 最近 250 个交易日收盘价均值，通常作为年线参考。
    ma250: Optional[float] = Field(default=None, description="250 日收盘价均线，近似年线")


class VolumeMAIndicators(BaseModel):
    """
    成交量移动平均线指标。

    volume 的原始单位沿用东方财富日 K 接口数据：手。
    """

    # 最近 5 个交易日成交量均值，单位沿用日 K 接口的“手”。
    vol_ma5: Optional[float] = Field(default=None, description="5 日成交量均线，单位：手")
    # 最近 10 个交易日成交量均值，单位为手。
    vol_ma10: Optional[float] = Field(default=None, description="10 日成交量均线，单位：手")
    # 最近 20 个交易日成交量均值，单位为手。
    vol_ma20: Optional[float] = Field(default=None, description="20 日成交量均线，单位：手")
    # 最近 60 个交易日成交量均值，单位为手。
    vol_ma60: Optional[float] = Field(default=None, description="60 日成交量均线，单位：手")


class MACDIndicators(BaseModel):
    """
    MACD 指标。

    字段对应东方财富前端算法生成的 DIF、DEA 和 MACD 柱值。
    """

    # MACD 指标的 DIF 快线值。
    dif: Optional[float] = Field(default=None, description="DIF 快线")
    # MACD 指标的 DEA 慢线值。
    dea: Optional[float] = Field(default=None, description="DEA 慢线")
    # DIF 与 DEA 关系对应的 MACD 柱体值。
    hist: Optional[float] = Field(default=None, description="MACD 柱值")


class KDJIndicators(BaseModel):
    """
    KDJ 随机指标。

    用于观察短线超买、超卖和拐点变化。
    """

    # KDJ 随机指标的快速 K 值。
    k: Optional[float] = Field(default=None, description="K 值")
    # KDJ 随机指标的慢速 D 值。
    d: Optional[float] = Field(default=None, description="D 值")
    # 由 K 与 D 派生的 J 值，用于表征较强的超买超卖变化。
    j: Optional[float] = Field(default=None, description="J 值")


class RSIIndicators(BaseModel):
    """
    RSI 相对强弱指标。

    三个周期用于区分短线、中线和更平滑的动量状态。
    """

    # 6 日周期的相对强弱指标，侧重短线动量。
    rsi6: Optional[float] = Field(default=None, description="6 日 RSI")
    # 12 日周期的相对强弱指标。
    rsi12: Optional[float] = Field(default=None, description="12 日 RSI")
    # 24 日周期的相对强弱指标，提供更平滑的动量观察。
    rsi24: Optional[float] = Field(default=None, description="24 日 RSI")


class BOLLIndicators(BaseModel):
    """
    BOLL 布林线指标。

    mid 为中轨，upper/lower 为上下轨。
    """

    # 布林线的中轨值，作为价格趋势的中心参考。
    mid: Optional[float] = Field(default=None, description="布林线中轨")
    # 布林线的上轨值，用于表征波动区间上界。
    upper: Optional[float] = Field(default=None, description="布林线上轨")
    # 布林线的下轨值，用于表征波动区间下界。
    lower: Optional[float] = Field(default=None, description="布林线下轨")


class CCIIndicators(BaseModel):
    """
    CCI 顺势指标。
    """

    # 14 日周期的顺势指标值，用于观察价格偏离常态区间的程度。
    cci14: Optional[float] = Field(default=None, description="14 日 CCI")


class WRIndicators(BaseModel):
    """
    WR 威廉指标。

    用于观察短线超买超卖区间。
    """

    # 6 日周期的威廉指标，侧重较短周期的超买超卖状态。
    wr6: Optional[float] = Field(default=None, description="6 日 WR")
    # 10 日周期的威廉指标值。
    wr10: Optional[float] = Field(default=None, description="10 日 WR")
    # 14 日周期的威廉指标值。
    wr14: Optional[float] = Field(default=None, description="14 日 WR")


class ATRIndicators(BaseModel):
    """
    ATR 真实波幅指标。东方财富当前页面未公开时保持 None。
    """

    # 14 日周期的平均真实波幅，用于衡量价格波动程度。
    atr14: Optional[float] = Field(default=None, description="14 日 ATR，衡量价格波动幅度")


class ChipCostRange(BaseModel):
    """
    筹码成本区间。

    用于描述某个比例的筹码集中在哪个价格区间，以及集中程度。
    """

    # 指定筹码比例所覆盖成本区间的最低价格。
    low: Optional[float] = Field(default=None, description="成本区间下沿")
    # 指定筹码比例所覆盖成本区间的最高价格。
    high: Optional[float] = Field(default=None, description="成本区间上沿")
    # 该成本区间内筹码的集中程度，数值口径沿用来源页面。
    concentration: Optional[float] = Field(default=None, description="筹码集中度")


class ChipChart(BaseModel):
    """
    筹码图曲线。

    x 为网页 Canvas 实际绘制宽度，y 为页面坐标对应的价格。
    """

    # 来源页面 Canvas 中各价格层级对应的筹码柱宽数组。
    x: List[float] = Field(default_factory=list, description="网页筹码图横向绘制数据")
    # 与 x 按索引对应的筹码图价格或纵轴坐标数组。
    y: List[float] = Field(default_factory=list, description="筹码图纵轴数据")


class ChipDistribution(BaseModel):
    """
    筹码分布信息。

    数据由逆向还原的东方财富筹码算法生成。每次抓取最多计算最近 90 个交易日，
    更早日线的 chip 字段可能为空。
    """

    # 按当日收盘价计算的获利筹码比例，口径沿用东方财富页面。
    profit_ratio: Optional[float] = Field(default=None, description="获利比例")
    # 逆向算法估算的全部筹码平均持仓成本。
    avg_cost: Optional[float] = Field(default=None, description="平均成本")
    # 覆盖 90% 筹码的成本上下沿与集中度。
    cost_90: ChipCostRange = Field(default_factory=ChipCostRange, description="90% 筹码成本区间")
    # 覆盖 70% 筹码的成本上下沿与集中度。
    cost_70: ChipCostRange = Field(default_factory=ChipCostRange, description="70% 筹码成本区间")
    # 逆向算法生成的筹码分布曲线；数据不足时为 None。
    chart: Optional[ChipChart] = Field(default=None, description="筹码图曲线，可能为空")


class StockDailyDetailSource(BaseModel):
    """
    数据来源记录。

    用于以后排查字段来源、接口切换和结果复核。
    """

    # 开高低收、成交量和成交额等基础日线的来源标识。
    daily: str = Field(
        default="eastmoney.quote_api.reverse",
        description="逆向日线接口数据来源",
    )
    # 与本条数据对应的东方财富股票行情参考地址，协议请求不加载该页面。
    page_url: Optional[str] = Field(default=None, description="东方财富行情参考地址")
    # 抓取时实际使用的网络出口；生产逆向链路使用 proxy。
    network: Optional[str] = Field(
        default=None,
        description="逆向接口实际网络出口",
    )
    # 移动均线、MACD 等技术指标的计算或提取来源。
    indicator: str = Field(
        default="eastmoney.quote_page.algorithm_reverse",
        description="逆向还原的技术指标算法来源",
    )
    # 筹码分布数据的计算或提取来源。
    chip: str = Field(
        default="eastmoney.quote_page.algorithm_reverse",
        description="逆向还原的筹码算法来源",
    )


class StockDailyDetail(BaseModel):
    """
    股票详细日线数据。

    唯一键建议：code + trade_date + adjust。
    """

    __tablename__: ClassVar[str] = "stock_daily_detail"  # MongoDB 集合名

    # 该行情记录所属交易日，使用 YYYY-MM-DD 格式。
    trade_date: str = Field(..., description="交易日期，YYYY-MM-DD")
    # 交易日期的 YYYYMMDD 整数形式，便于数值比较和索引查询。
    trade_date_int: int = Field(..., description="交易日期整数，YYYYMMDD")
    # A 股六位证券代码，与交易日和复权口径共同唯一标识记录。
    code: str = Field(..., description="股票代码，6 位")
    # 股票简称，来源页面无法解析时可为 None。
    name: Optional[str] = Field(default=None, description="股票名称")
    # 价格复权口径：空字符串为不复权，qfq 为前复权，hfq 为后复权。
    adjust: str = Field(default="qfq", description="复权口径：'' / qfq / hfq")

    # 指定交易日的开盘价。
    open: Optional[float] = Field(default=None, description="开盘价")
    # 指定交易日的收盘价。
    close: Optional[float] = Field(default=None, description="收盘价")
    # 指定交易日的最高成交价。
    high: Optional[float] = Field(default=None, description="最高价")
    # 指定交易日的最低成交价。
    low: Optional[float] = Field(default=None, description="最低价")

    # 当日成交量，单位为手。
    volume: Optional[int] = Field(default=None, description="成交量，单位：手")
    # 当日成交金额，单位为人民币元。
    amount: Optional[float] = Field(default=None, description="成交额，单位：元")

    # 当日最高价与最低价相对前收盘价的振幅百分比。
    amplitude_pct: Optional[float] = Field(default=None, description="振幅，单位：%")
    # 当日收盘价相对前收盘价的涨跌幅百分比。
    pct_chg: Optional[float] = Field(default=None, description="涨跌幅，单位：%")
    # 当日收盘价相对前收盘价的绝对变动金额。
    change_amount: Optional[float] = Field(default=None, description="涨跌额，单位：元")
    # 当日成交股数占流通股本的比例，单位为百分比。
    turnover_pct: Optional[float] = Field(default=None, description="换手率，单位：%")

    # 各周期收盘价移动平均线指标组。
    ma: MAIndicators = Field(default_factory=MAIndicators, description="收盘价均线指标组")
    # 各周期成交量移动平均线指标组。
    volume_ma: VolumeMAIndicators = Field(default_factory=VolumeMAIndicators, description="成交量均线指标组")
    # 趋势与动量分析使用的 MACD 指标组。
    macd: MACDIndicators = Field(default_factory=MACDIndicators, description="MACD 指标组")
    # 短线超买超卖分析使用的 KDJ 指标组。
    kdj: KDJIndicators = Field(default_factory=KDJIndicators, description="KDJ 指标组")
    # 不同周期相对强弱指标组。
    rsi: RSIIndicators = Field(default_factory=RSIIndicators, description="RSI 指标组")
    # 表征价格中心和波动边界的布林线指标组。
    boll: BOLLIndicators = Field(default_factory=BOLLIndicators, description="BOLL 布林线指标组")
    # 用于观察价格异常偏离的 CCI 指标组。
    cci: CCIIndicators = Field(default_factory=CCIIndicators, description="CCI 指标组")
    # 用于观察超买超卖区间的威廉指标组。
    wr: WRIndicators = Field(default_factory=WRIndicators, description="WR 威廉指标组")
    # 用于衡量真实价格波动幅度的 ATR 指标组。
    atr: ATRIndicators = Field(default_factory=ATRIndicators, description="ATR 真实波幅指标组")

    # 当日筹码成本与分布数据；来源页未公开时为 None。
    chip: Optional[ChipDistribution] = Field(default=None, description="筹码分布，可能为空")

    # 基础行情、技术指标和筹码数据的来源溯源信息。
    source: StockDailyDetailSource = Field(default_factory=StockDailyDetailSource, description="本条记录各类数据来源")
    # 记录首次写入 MongoDB 的北京时间。
    created_at: datetime = Field(default_factory=now_cn, description="首次入库时间，北京时间")
    # 记录最近一次更新 MongoDB 文档的北京时间。
    updated_at: datetime = Field(default_factory=now_cn, description="最近更新时间，北京时间")
