"""Interpretation of a price table (报价单) and recomputation of averages.

A table is a JSON object keyed by quotation number ("报价单号").  Each entry
has a market (市场), commodity (商品), quotation date (报价日) and an integer
price in fen (分).  Prices are never converted to floats: averages are
computed in fen and then rendered with exact integer arithmetic.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

FIELD_MARKET = ("market", "市场", "market_name", "市场名称")
FIELD_PRODUCT = ("product", "commodity", "item", "商品", "商品名称", "品种")
FIELD_DATE = ("date", "quotation_date", "quote_date", "报价日", "报价日期")
FIELD_PRICE = ("price", "price_fen", "unit_price", "价格", "价格分", "单价")
FIELD_UNIT = ("unit", "单位")

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class TableError(ValueError):
    """The table cannot be interpreted as a quotation table."""


@dataclass(frozen=True)
class Quote:
    quotation_no: str
    market: str
    product: str
    quoted_on: date
    price_fen: int
    unit: str | None = None


@dataclass
class GroupSummary:
    label: str
    count: int
    total_fen: int
    average_yuan: str
    min_fen: int
    max_fen: int


@dataclass
class PriceReport:
    quote_count: int
    markets: list[str]
    products: list[str]
    first_date: date | None
    last_date: date | None
    total_fen: int
    average_yuan: str
    by_market: list[GroupSummary]
    by_product: list[GroupSummary]
    by_date: list[GroupSummary]
    warnings: list[str] = field(default_factory=list)


def _pick(entry: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in entry:
            return entry[name]
    return None


def _as_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TableError(f"{label} 必须是非空字符串")
    return value.strip()


def _as_date(value: Any, label: str) -> date:
    if isinstance(value, str):
        match = DATE_RE.match(value.strip())
        if not match:
            raise TableError(f"{label} 必须是 YYYY-MM-DD 格式，实际为 {value!r}")
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise TableError(f"{label} 不是有效日期: {value!r}") from exc
    raise TableError(f"{label} 必须是 YYYY-MM-DD 字符串")


def _as_fen(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TableError(f"{label} 必须是整数分，实际为布尔值")
    if isinstance(value, int):
        return value
    raise TableError(f"{label} 必须是整数分，实际为 {value!r}")


def parse_quotes(document: Any) -> list[Quote]:
    if not isinstance(document, dict):
        raise TableError("底表必须是按报价单号索引的 JSON 对象")
    quotes: list[Quote] = []
    for quotation_no, entry in document.items():
        if not isinstance(entry, dict):
            raise TableError(f"报价单 {quotation_no} 的内容必须是对象")
        market = _as_text(_pick(entry, FIELD_MARKET), f"报价单 {quotation_no} 的 market")
        product = _as_text(
            _pick(entry, FIELD_PRODUCT), f"报价单 {quotation_no} 的 product"
        )
        quoted_on = _as_date(_pick(entry, FIELD_DATE), f"报价单 {quotation_no} 的 date")
        price = _as_fen(_pick(entry, FIELD_PRICE), f"报价单 {quotation_no} 的 price")
        unit_value = _pick(entry, FIELD_UNIT)
        unit = unit_value.strip() if isinstance(unit_value, str) and unit_value.strip() else None
        quotes.append(
            Quote(
                quotation_no=str(quotation_no),
                market=market,
                product=product,
                quoted_on=quoted_on,
                price_fen=price,
                unit=unit,
            )
        )
    quotes.sort(key=lambda q: (q.quoted_on, q.market, q.product, q.quotation_no))
    return quotes


def format_fen(value_fen: int) -> str:
    """Render an amount in fen as ``元`` with two decimals (exact)."""

    sign = "-" if value_fen < 0 else ""
    value_fen = abs(value_fen)
    return f"{sign}{value_fen // 100}.{value_fen % 100:02d}"


def average_yuan(total_fen: int, count: int) -> str:
    """Render ``total / count`` fen as yuan with four decimals, half up.

    All arithmetic stays in integers, so no binary floating point error can
    creep into a published average.
    """

    if count <= 0:
        raise ValueError("count must be positive")
    # 1 yuan = 100 fen; four decimal places means a scale of 10_000.
    # average_yuan_scaled = (total_fen / count / 100) * 10_000
    scaled = total_fen * 100
    sign = -1 if scaled < 0 else 1
    scaled = abs(scaled)
    quotient, remainder = divmod(scaled, count)
    if remainder * 2 >= count:
        quotient += 1
    value = sign * quotient
    integer, fraction = divmod(abs(value), 10_000)
    prefix = "-" if value < 0 else ""
    return f"{prefix}{integer}.{fraction:04d}"


def _summaries(
    quotes: list[Quote], key: Any, label: str | None = None
) -> list[GroupSummary]:
    buckets: dict[Any, list[Quote]] = defaultdict(list)
    for quote in quotes:
        buckets[key(quote)].append(quote)
    result: list[GroupSummary] = []
    for bucket_key, items in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        prices = [q.price_fen for q in items]
        total = sum(prices)
        result.append(
            GroupSummary(
                label=str(bucket_key),
                count=len(prices),
                total_fen=total,
                average_yuan=average_yuan(total, len(prices)),
                min_fen=min(prices),
                max_fen=max(prices),
            )
        )
    return result


def build_report(quotes: list[Quote]) -> PriceReport:
    warnings: list[str] = []
    if not quotes:
        warnings.append("该版本没有任何报价单")
    prices = [q.price_fen for q in quotes]
    total = sum(prices)
    report = PriceReport(
        quote_count=len(quotes),
        markets=sorted({q.market for q in quotes}),
        products=sorted({q.product for q in quotes}),
        first_date=min((q.quoted_on for q in quotes), default=None),
        last_date=max((q.quoted_on for q in quotes), default=None),
        total_fen=total,
        average_yuan=average_yuan(total, len(quotes)) if quotes else "0.0000",
        by_market=_summaries(quotes, lambda q: q.market),
        by_product=_summaries(quotes, lambda q: q.product),
        by_date=_summaries(quotes, lambda q: q.quoted_on.isoformat()),
        warnings=warnings,
    )
    months: dict[str, int] = defaultdict(int)
    for quote in quotes:
        months[f"{quote.quoted_on.year:04d}-{quote.quoted_on.month:02d}"] += 1
    if len(months) > 1:
        listed = "、".join(f"{month}（{count} 条）" for month, count in sorted(months.items()))
        warnings.append(f"该版本报价跨多个自然月：{listed}")
    duplicates: dict[tuple[str, str, date], int] = defaultdict(int)
    for quote in quotes:
        duplicates[(quote.market, quote.product, quote.quoted_on)] += 1
    repeated = [key for key, count in duplicates.items() if count > 1]
    if repeated:
        warnings.append(
            f"有 {len(repeated)} 组“市场+商品+报价日”重复报价，均价按全部报价单计算"
        )
    return report


def month_bounds(value: str) -> tuple[date, date]:
    match = re.match(r"^(\d{4})-(\d{2})$", value.strip())
    if not match:
        raise ValueError(f"月份必须是 YYYY-MM，实际为 {value!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"月份无效: {value!r}")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def filter_by_month(quotes: list[Quote], month: str | None) -> list[Quote]:
    if not month:
        return quotes
    start, end = month_bounds(month)
    return [q for q in quotes if start <= q.quoted_on <= end]
