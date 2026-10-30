"""Run a daily notification only in its Beijing-time window and claim the day first.

GitHub schedule events may arrive hours late. Check the wall clock at execution,
not the nominal cron time. A separate Git branch stores attempted delivery days;
Actions caches are deliberately not used as the duplicate-prevention authority.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


BEIJING = timezone(timedelta(hours=8))
STATE_BRANCH = "daily-delivery-state"
STATE_PATH = "delivery.json"


class StateError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def minute_of_day(value: str) -> int:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间必须为HH:MM")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def in_window(now: datetime, start: str, end: str) -> bool:
    begin, stop = minute_of_day(start), minute_of_day(end)
    if stop <= begin:
        raise ValueError("发送窗口必须在同一自然日内，结束时间晚于开始时间")
    local = now.astimezone(BEIJING)
    return begin <= local.hour * 60 + local.minute < stop


class GitHubLedger:
    def __init__(self, repository: str, token: str, source_sha: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise StateError("仓库名称无效，停止推送。")
        if not token or not re.fullmatch(r"[0-9a-f]{40,64}", source_sha):
            raise StateError("缺少GITHUB_TOKEN或有效提交号，停止推送。")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token
        self.source_sha = source_sha

    def api(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.root + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DailyDeliveryWindow/1.0",
        })
        try:
            with build_opener(NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(300_001)
                if len(raw) > 300_000:
                    raise StateError("去重记录响应过大，停止推送。")
                return response.status, json.loads(raw)
        except HTTPError as error:
            if error.code in (404, 409, 422):
                return error.code, None
            raise StateError(f"去重记录请求失败（HTTP {error.code}），停止推送。") from None
        except StateError:
            raise
        except Exception:
            # Do not include URLs, request headers, response bodies or tokens.
            raise StateError("去重记录网络异常或响应无效，停止推送且不自动重发。") from None

    def read(self):
        status, blob = self.api("GET", f"/contents/{STATE_PATH}?ref={STATE_BRANCH}")
        if status == 404:
            return None, {}
        if status != 200:
            raise StateError("无法读取去重记录，停止推送。")
        try:
            if blob["encoding"] != "base64" or not isinstance(blob["sha"], str):
                raise ValueError
            data = json.loads(base64.b64decode(blob["content"]).decode("utf-8"))
            if (type(data.get("version")) is not int or data["version"] != 1
                    or not isinstance(data.get("attempts"), dict)
                    or len(data["attempts"]) > 60):
                raise ValueError
            for day, stamp in data["attempts"].items():
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError
                parsed = datetime.fromisoformat(stamp)
                if parsed.utcoffset() is None or parsed.astimezone(BEIJING).date().isoformat() != day:
                    raise ValueError
            return blob["sha"], data["attempts"]
        except Exception:
            raise StateError("去重记录格式异常，保留原记录并停止推送。") from None

    def claim(self, now: datetime) -> bool:
        local = now.astimezone(BEIJING)
        day = local.date().isoformat()
        # Retry a write conflict only: never retry a notification or an ambiguous PUT.
        for _ in range(2):
            sha, attempts = self.read()
            if day in attempts:
                return False
            if sha is None:
                status, _ = self.api("POST", "/git/refs", {
                    "ref": "refs/heads/" + STATE_BRANCH, "sha": self.source_sha,
                })
                if status not in (201, 422):
                    raise StateError("无法初始化去重记录分支，停止推送。")
            cutoff = local.date() - timedelta(days=29)
            attempts = {d: stamp for d, stamp in attempts.items()
                        if cutoff <= date.fromisoformat(d) <= local.date()}
            attempts[day] = local.isoformat()
            payload = {
                "message": "Claim daily notification attempt " + day,
                "branch": STATE_BRANCH,
                "content": base64.b64encode(json.dumps({
                    "version": 1, "attempts": attempts,
                }, indent=2).encode()).decode(),
            }
            if sha is not None:
                payload["sha"] = sha
            status, _ = self.api("PUT", "/contents/" + STATE_PATH, payload)
            if status in (200, 201):
                return True
            if status not in (409, 422):
                raise StateError("无法保存本日推送标记，停止推送。")
        raise StateError("去重记录连续发生写入冲突，本轮停止推送。")


def guarded_run(command, now, start, end, event, force, ledger_factory, runner):
    allowed = in_window(now, start, end)
    if event == "schedule" and not allowed:
        print(f"SKIP：当前北京时间{now.astimezone(BEIJING):%Y-%m-%d %H:%M}不在{start}–{end}发送窗口内；不补发、不读取报价。")
        return 0
    if not (force and event != "schedule"):
        ledger = ledger_factory()
        if not ledger.claim(now):
            print("SKIP：北京时间今天已记录一次推送尝试，本轮不再推送。")
            return 0
    else:
        print("MANUAL：按显式手动要求额外运行一次，不修改日常去重记录。")
    print("开始本次日报任务；推送前已记录尝试，不因超时自动重复发送。")
    return runner(command, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--check-window", action="store_true", help="仅检查时间窗口，写入Actions输出；不读取密钥或占用今日发送标记")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command and not args.check_window:
        parser.error("缺少要运行的命令")
    try:
        if args.check_window:
            now = datetime.now(BEIJING)
            allowed = (os.environ.get("GITHUB_EVENT_NAME", "local") != "schedule"
                       or in_window(now, args.start, args.end))
            output = "allowed=" + str(allowed).lower() + "\n"
            print(output.strip())
            print(f"当前北京时间{now:%Y-%m-%d %H:%M}；日常发送窗口{args.start}–{args.end}。")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
                    file.write(output)
            return 0
        return guarded_run(command, datetime.now(BEIJING), args.start, args.end,
                           os.environ.get("GITHUB_EVENT_NAME", "local"),
                           os.environ.get("FORCE_SEND", "false").lower() == "true",
                           lambda: GitHubLedger(os.environ.get("GITHUB_REPOSITORY", ""),
                                                os.environ.get("GITHUB_TOKEN", ""),
                                                os.environ.get("GITHUB_SHA", "")),
                           subprocess.run)
    except (StateError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
