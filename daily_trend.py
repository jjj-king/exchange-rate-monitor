"""Small, dependency-free daily price history and seven-calendar-day summary.

Only actual dated observations are stored. A missing day is never forward-filled.
The caller supplies an explicit series ID to prevent mixing different prices.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys


def valid_point(point: dict) -> dict:
    if not isinstance(point, dict):
        raise ValueError("Invalid history point")
    day = date.fromisoformat(point["date"])
    value = Decimal(point["value"])
    stamp = datetime.fromisoformat(point["timestamp"])
    if (not value.is_finite() or value <= 0 or stamp.utcoffset() is None
            or stamp.date() != day):
        raise ValueError("Invalid history value or date")
    return {"date": day.isoformat(), "value": str(value), "timestamp": stamp.isoformat()}


def load_history(path: Path, series: str) -> list[dict]:
    if not path.exists():
        return []
    if path.stat().st_size > 100_000:
        raise ValueError("History is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or data.get("series") != series
            or not isinstance(data.get("points"), list) or len(data["points"]) > 90):
        raise ValueError("Invalid history format or series")
    points = [valid_point(point) for point in data["points"]]
    if len({point["date"] for point in points}) != len(points):
        raise ValueError("Duplicate history dates")
    return points


def update_history(points: list[dict], today: date, point: dict | None = None) -> list[dict]:
    cutoff = today - timedelta(days=29)
    by_date = {p["date"]: p for p in points
               if cutoff <= date.fromisoformat(p["date"]) <= today}
    if point is not None:
        point = valid_point(point)
        old = by_date.get(point["date"])
        if (cutoff <= date.fromisoformat(point["date"]) <= today
                and (old is None or datetime.fromisoformat(point["timestamp"])
                     >= datetime.fromisoformat(old["timestamp"]))):
            by_date[point["date"]] = point
    return [by_date[key] for key in sorted(by_date)]


def save_history(path: Path, series: str, points: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "series": series, "points": points},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_summary(points: list[dict], today: date, *, digits: int, unit: str) -> str:
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    by_date = {p["date"]: Decimal(p["value"]) for p in points}
    values = [by_date.get(day.isoformat()) for day in days]
    valid = [(day, value) for day, value in zip(days, values) if value is not None]
    lines = [f"### 近7天趋势（{days[0]:%m/%d}–{days[-1]:%m/%d}）", "",
             f"| 日期 | 价格（{unit}） |", "| --- | ---: |"]
    for day, value in zip(days, values):
        shown = "暂无记录" if value is None else f"{value:.{digits}f}"
        lines.append(f"| {day:%Y-%m-%d} | {shown} |")
    lines += ["", "未记录的日期显示“暂无记录”，不沿用旧报价补齐。", ""]
    if not valid:
        return "\n".join(lines + ["暂没有有效历史记录；从首次有效报价开始积累。"])
    low = min(value for _, value in valid)
    high = max(value for _, value in valid)
    lines += [f"有效记录{len(valid)}/7天；最低{low:.{digits}f}，最高{high:.{digits}f}{unit}。"]
    if len(valid) >= 2:
        first_day, first = valid[0]
        last_day, last = valid[-1]
        change = last - first
        percent = change / first * 100
        lines += [f"首末有效记录变化（{first_day:%m/%d}→{last_day:%m/%d}）："
                  f"{change:+.{digits}f}{unit}（{percent:+.2f}%）。"]
    else:
        lines += ["目前仅1天有效记录，仍在积累，暂不计算区间变化。"]
    lines += ["", "按每日运行时的有效报价记录，非日均价或日收盘价；同一天保留时间较晚的一次。"]
    return "\n".join(lines)


def prepare_summary(path: Path, series: str, today: date, *, digits: int, unit: str,
                    point: dict | None = None, persist: bool = False) -> str:
    # A history problem must not turn a valid current quote into a fetch failure.
    try:
        points = update_history(load_history(path, series), today, point)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        print("WARNING: 历史记录无法读取或校验，未覆盖原文件；今日报价照常展示。", file=sys.stderr)
        return "### 近7天趋势\n\n历史记录暂不可用，请查看Actions日志；未补造缺失数据。"
    warning = ""
    if persist:
        try:
            save_history(path, series, points)
        except OSError:
            print("WARNING: 本次历史记录未能保存。", file=sys.stderr)
            warning = "\n\n本次历史记录未能保存，请查看Actions日志。"
    return build_summary(points, today, digits=digits, unit=unit) + warning
