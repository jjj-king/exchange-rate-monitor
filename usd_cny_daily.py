#!/usr/bin/env python3
"""美元兑人民币每日通知：中行牌价 + 息知。

部署：由GitHub Actions每天运行一次；密钥只从XZKEY读取。
本文件是为daily通知新增的独立入口，原仓库邮件/阈值脚本无需修改。
运行python usd_cny_daily.py --dry-run可只取报价并预览，不读取密钥、不推送。
每次普通运行最多调用一次推送；手动运行也会额外推送一次。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from daily_trend import prepare_summary

BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
BEIJING = timezone(timedelta(hours=8))
RATE_LABELS = ("现汇买入价", "现钞买入价", "现汇卖出价", "现钞卖出价")


class TableRows(HTMLParser):
    """按HTML表格行收集单元格，保留空单元格和列的位置。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
            self.cell = None
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
            self.cell = None


def parse_usd(html):
    parser = TableRows()
    parser.feed(html)
    header = None
    for row in parser.rows:
        compact = [re.sub(r"\s+", "", cell) for cell in row]
        if "货币名称" in compact and all(label in compact for label in RATE_LABELS):
            header = compact
            continue
        if header is None:
            continue
        name_index = header.index("货币名称")
        if len(row) <= name_index or compact[name_index] != "美元":
            continue
        rates = {}
        for label in RATE_LABELS:
            index = header.index(label)
            if index >= len(row):
                raise ValueError("美元报价行缺少必要列")
            try:
                rate = Decimal(row[index].replace(",", ""))
            except InvalidOperation:
                raise ValueError("美元报价存在空值或非数字") from None
            if not rate.is_finite() or rate <= 0:
                raise ValueError("美元报价数值无效")
            rates[label] = rate
        date_index = header.index("发布日期") if "发布日期" in header else -1
        time_index = header.index("发布时间") if "发布时间" in header else -1
        raw_date = row[date_index] if 0 <= date_index < len(row) else ""
        raw_time = row[time_index] if 0 <= time_index < len(row) else ""
        date_match = re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", raw_date + " " + raw_time)
        time_match = re.search(r"\d{1,2}:\d{2}:\d{2}", raw_time or raw_date)
        if not date_match or not time_match:
            raise ValueError("无法识别报价发布日期或时间")
        stamp = date_match.group().replace("/", "-").replace(".", "-") + " " + time_match.group()
        # Reject malformed dates instead of labeling a stale/invalid result as current.
        published = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        return rates, published
    raise ValueError("未识别到完整美元牌价，请检查中行网页是否变化")


def fetch_usd():
    # Only GET requests are retried; an ambiguous notification failure is not retried.
    for attempt in range(3):
        try:
            req = Request(BOC_URL, headers={"User-Agent": "Mozilla/5.0 (DailyRateReminder/1.0)"})
            with urlopen(req, timeout=25) as response:
                body = response.read(2_000_001)
                encoding = response.headers.get_content_charset() or "utf-8"
            if len(body) > 2_000_000:
                raise ValueError("报价页面大小异常")
            return parse_usd(body.decode(encoding))
        except Exception:
            if attempt == 2:
                raise RuntimeError("连续3次未能获取或解析中国银行美元牌价") from None
            time.sleep(attempt + 1)


def build_message(rates, published, now=None):
    now = now or datetime.now(BEIJING)
    sell = rates["现汇卖出价"] / Decimal(100)
    title = f"美元购汇日报｜现汇卖出价 {sell:.4f}元"
    lines = [
        "## 买入美元现汇 · 中国银行牌价",
        "",
        f"**购汇参考价：1美元 = {sell:.4f}元人民币（现汇卖出价）**",
        "",
        f"买入1000美元约需{sell * 1000:.2f}元人民币。",
        "",
        "你用人民币买入账户美元，应看银行的现汇卖出价；数值越低，同额美元所需人民币越少。",
        "",
        "| 项目 | 1美元折合人民币 |",
        "| --- | --- |",
    ]
    # Prioritize the user's purchase price; retain the reverse direction only.
    for label in ("现汇卖出价", "现汇买入价"):
        lines.append(f"| {label} | {rates[label] / Decimal(100):.4f} 元 |")
    lines += [
        "",
        f"报价时间（北京时间）：{published:%Y-%m-%d %H:%M:%S}",
        "",
        f"获取时间（北京时间）：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "人民币购买账户美元：关注现汇卖出价；账户美元换回人民币：关注现汇买入价。",
        "",
        "中行原表以100美元为单位，本消息已除以100。它是银行牌价，不是市场中间价。",
        "",
        f"数据来源：[中国银行外汇牌价]({BOC_URL})。实际兑换以银行成交报价为准。",
    ]
    if now - published > timedelta(hours=24):
        lines += ["", "本次网页报价距获取时间超过24小时，请留意上方报价时间。"]
    return title, "\n".join(lines)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def xizhi_url(key):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("请在当前仓库的Actions Secrets中配置XZKEY（只填息知Key，不填完整网址）。")
    return f"https://xizhi.qqoq.net/{key}.send"


def send_once(key, title, text):
    # One POST only; exceptions can contain the key-bearing URL and are sanitized.
    url = xizhi_url(key)
    data = json.dumps({"title": title, "content": text}, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "DailyRateReminder/1.0",
    })
    try:
        with build_opener(_NoRedirect()).open(req, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError("息知未返回正常状态，本次不自动重发")
            data = response.read(100_001)
            if len(data) > 100_000:
                raise RuntimeError("息知响应异常，本次不自动重发")
            result = json.loads(data.decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"息知请求失败（HTTP {error.code}），本次不自动重发") from None
    except Exception:
        raise RuntimeError("息知网络请求失败或响应无法解析，本次不自动重发；请先检查是否已经收到通知") from None
    if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 200:
        raise RuntimeError("息知未确认接收成功；请检查XZKEY和息知配置，本次不自动重发")


def main(argv=None):
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument("--dry-run", action="store_true", help="预览报价，不推送")
    argparser.add_argument("--history-file", type=Path,
                           default=Path(__file__).resolve().parent / ".state/usd-cny-history.json",
                           help="历史购汇价文件；预览只读，不写入")
    args = argparser.parse_args(argv)
    key = ""
    if not args.dry_run:
        key = os.environ.get("XZKEY", "").strip()
        try:
            xizhi_url(key)
        except ValueError as error:
            print("ERROR: " + str(error), file=sys.stderr)
            return 1
    fetch_failed = False
    now = datetime.now(BEIJING)
    point = None
    try:
        rates, published = fetch_usd()
        title, text = build_message(rates, published, now)
        if published.astimezone(BEIJING) <= now:
            point = {"date": published.astimezone(BEIJING).date().isoformat(),
                     "value": str(rates["现汇卖出价"] / Decimal(100)),
                     "timestamp": published.astimezone(BEIJING).isoformat()}
    except Exception:
        fetch_failed = True
        title = "美元兑人民币日报：获取失败"
        text = ("今天未能获取或解析中国银行美元牌价，未使用缓存或虚构数据。\n\n"
                "请打开GitHub仓库的Actions检查运行结果，或稍后手动重试。\n\n"
                f"数据源：{BOC_URL}")
    text += "\n\n" + prepare_summary(args.history_file, "boc-usd-cny-fx-sell-per-usd", now.date(),
                                      digits=4, unit="元/美元", point=point,
                                      persist=not args.dry_run)
    text += "\n\n趋势仅统计现汇卖出价；价格下降表示购买同额美元更省人民币。"
    if args.dry_run:
        print(title + "\n\n" + text)
        return int(fetch_failed)
    try:
        send_once(key, title, text)
    except Exception as exc:
        # send_once emits a sanitized message without the key or response body.
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    print("报价获取失败，息知已接受失败提醒请求。" if fetch_failed else "息知已接受本次日报推送请求，请以微信实际收到通知为准。")
    return int(fetch_failed)


if __name__ == "__main__":
    raise SystemExit(main())
