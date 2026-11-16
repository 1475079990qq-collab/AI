"""本机当天时间（Asia/Shanghai, UTC+8）。

日期是硬事实，不能让小模型自己估：prompt 注入、自检规则判定、判断缓存键
都从这里取同一个当天日期，避免各写一份、跨天对不上。
"""
from __future__ import annotations

import datetime as _dt

try:  # Python 3.9+ 标准库；极端环境缺 tzdata 时退回固定 UTC+8
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 兜底分支
    _TZ = _dt.timezone(_dt.timedelta(hours=8), "Asia/Shanghai")

_WEEK_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def now_shanghai() -> _dt.datetime:
    """当前上海时间（带时区）。"""
    return _dt.datetime.now(_TZ)


def date_key() -> str:
    """缓存键用的当天日期：YYYY-MM-DD。跨天自然变键，旧判断不复用。"""
    return now_shanghai().strftime("%Y-%m-%d")


def weekday_cn() -> str:
    return _WEEK_CN[now_shanghai().weekday()]


def today_label() -> str:
    """给模型看的当天日期 + 星期 + 时区。"""
    n = now_shanghai()
    return (f"{n.year}年{n.month}月{n.day}日 {weekday_cn()}"
            f"（时区 Asia/Shanghai，UTC+8）")
