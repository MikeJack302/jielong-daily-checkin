"""Browser-based daily check-in for Jielong Guanjia.

One-time setup opens a dedicated Edge profile for QR-code login and records
the target activity URL.  Daily runs reuse that local browser profile.  The
script does not export cookies/tokens or call undocumented private APIs.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Sequence


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "web_config.json"
PROFILE_DIR = BASE_DIR / ".browser-profile"
LOG_DIR = BASE_DIR / "logs"
EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

DEFAULT_CONFIG: dict[str, Any] = {
    "login_url": "https://i.jielong.com/",
    "target_url": "",
    "target_title": "",
    "active_from": "2000-01-01",
    "active_until": "2099-12-31",
    "success_markers": [
        "今日已打卡",
        "今天已打卡",
        "今日打卡完成",
        "已完成今日打卡",
        "打卡成功",
        "提交成功",
    ],
    "primary_buttons": ["立即打卡", "去打卡", "参与打卡", "我要打卡", "今日打卡"],
    "quick_fill_buttons": ["快速填充上一次内容", "填充上次内容"],
    "submit_buttons": ["立即打卡", "确认打卡", "提交打卡", "确认提交", "提交"],
    "confirmation_buttons": ["确认打卡", "确认提交"],
    "verify_timeout_seconds": 20,
}


class CheckinError(RuntimeError):
    """An expected, fail-closed automation error."""


def setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jielong-web-checkin")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        LOG_DIR / "web-checkin.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            saved = json.load(file)
        config.update(saved)
    # Parse up front so invalid configuration cannot reach browser actions.
    date.fromisoformat(config["active_from"])
    date.fromisoformat(config["active_until"])
    return config


def save_config(config: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


def date_is_active(today: date, config: dict[str, Any]) -> bool:
    return (
        date.fromisoformat(config["active_from"])
        <= today
        <= date.fromisoformat(config["active_until"])
    )


def find_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise CheckinError("没有找到 Microsoft Edge，无法启动独立网页登录会话。")


def import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CheckinError(
            "缺少 Playwright。请先运行 install_web_task.ps1，"
            "或执行：python -m pip install -r web_requirements.txt"
        ) from exc
    return sync_playwright


def body_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


def contains_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker and marker in text for marker in markers)


def visible_exact(page: Any, label: str) -> list[Any]:
    """Return visible exact-text matches; never fall back to partial text."""
    matches: list[Any] = []
    locator = page.get_by_text(label, exact=True)
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                matches.append(candidate)
        except Exception:
            continue
    return matches


def unique_exact(page: Any, labels: Sequence[str]) -> tuple[str, Any] | None:
    for label in labels:
        matches = visible_exact(page, label)
        if len(matches) == 1:
            return label, matches[0]
        if len(matches) > 1:
            raise CheckinError(f"页面存在多个“{label}”，为避免误点，已停止。")
    return None


def save_screenshot(page: Any, prefix: str, logger: logging.Logger) -> Path | None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info("页面截图：%s", path)
        return path
    except Exception as exc:
        logger.warning("保存截图失败：%s", exc)
        return None


def launch_context(playwright: Any, *, headless: bool) -> Any:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        executable_path=str(find_edge()),
        headless=headless,
        viewport=None,
        locale="zh-CN",
        args=["--start-maximized"],
    )


def current_page(context: Any) -> Any:
    pages = [page for page in context.pages if not page.is_closed()]
    return pages[-1] if pages else context.new_page()


def try_open_target_from_dashboard(page: Any, target_title: str) -> bool:
    matches = visible_exact(page, target_title)
    if len(matches) != 1:
        return False
    try:
        matches[0].click(timeout=5_000)
        page.wait_for_timeout(1_000)
        return is_target_activity_page(page, target_title)
    except Exception:
        return False


def is_target_activity_page(page: Any, target_title: str) -> bool:
    """Distinguish the activity detail from a dashboard card with the same title."""
    text = body_text(page)
    if target_title not in text:
        return False
    detail_route = bool(re.search(r"(?:/|#/)[cC]/[^/?#]+", page.url))
    detail_markers = "打卡规则" in text and "打卡频率" in text
    return detail_route or detail_markers


def setup_login(config: dict[str, Any], logger: logging.Logger, timeout: int) -> str:
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        try:
            page = current_page(context)
            page.goto(config["login_url"], wait_until="domcontentloaded", timeout=30_000)
            logger.info("请在打开的 Edge 窗口中扫码登录接龙管家。")
            logger.info(
                "登录后脚本会尝试自动打开“%s”；若未自动打开，请手动点进该活动。",
                config["target_title"],
            )

            deadline = time.monotonic() + timeout
            attempted_dashboard_click = False
            while time.monotonic() < deadline:
                page = current_page(context)
                text = body_text(page)
                if is_target_activity_page(page, config["target_title"]):
                    url = page.url
                    if not url.startswith("https://i.jielong.com/"):
                        raise CheckinError(f"目标活动网址不属于接龙管家：{url}")
                    config["target_url"] = url
                    save_config(config)
                    save_screenshot(page, "setup-success", logger)
                    logger.info("首次登录配置成功，活动网址已保存。")
                    return url

                # After login, the target card is commonly visible on the home page.
                # Try once per loop only when there is one unambiguous exact match.
                if not attempted_dashboard_click and try_open_target_from_dashboard(
                    page, config["target_title"]
                ):
                    attempted_dashboard_click = True
                    continue
                time.sleep(2)

            save_screenshot(current_page(context), "setup-timeout", logger)
            raise CheckinError(
                "等待首次登录/目标活动超时。请重新运行 --setup，并在扫码后打开“"
                f"{config['target_title']}”。"
            )
        finally:
            context.close()


def visible_required_fields(page: Any) -> list[dict[str, str]]:
    return page.locator("input, textarea, select, [contenteditable='true']").evaluate_all(
        """elements => elements.filter(el => {
          const style = window.getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          const visible = style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
          const required = el.required || el.getAttribute('aria-required') === 'true';
          const value = (el.value ?? el.textContent ?? '').trim();
          return visible && required && !value;
        }).map(el => ({
          tag: el.tagName,
          name: el.name || el.getAttribute('aria-label') || el.placeholder || '',
        }))"""
    )


def wait_for_success(page: Any, config: dict[str, Any], deadline: float) -> bool:
    while time.monotonic() < deadline:
        text = body_text(page)
        if contains_any(text, config["success_markers"]):
            return True
        if "/submit-success" in page.url:
            return True
        time.sleep(1)
    return False


def perform_daily(config: dict[str, Any], logger: logging.Logger, diagnose: bool) -> str:
    if not date_is_active(date.today(), config):
        logger.info(
            "今天不在活动日期 %s 至 %s 内，跳过。",
            config["active_from"],
            config["active_until"],
        )
        return "outside-date-range"
    if not config.get("target_url"):
        raise CheckinError("尚未完成首次扫码配置。请先运行 web_checkin.py --setup。")

    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=not diagnose)
        try:
            page = current_page(context)
            page.goto(config["target_url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            text = body_text(page)

            if config["target_title"] not in text:
                save_screenshot(page, "login-or-page-failure", logger)
                raise CheckinError(
                    "登录态已失效，或目标活动网址已变化。请重新运行 "
                    "web_checkin.py --setup 扫码。"
                )
            if contains_any(text, config["success_markers"]):
                logger.info("今日已经打卡，无需重复提交。")
                return "already-checked-in"

            primary = unique_exact(page, config["primary_buttons"])
            if primary is None:
                save_screenshot(page, "button-not-found", logger)
                raise CheckinError("没有找到受信任的打卡按钮，页面可能已经变化。")
            if diagnose:
                logger.info("诊断成功：未打卡时将点击“%s”；本次未点击。", primary[0])
                return "diagnose-ready"

            logger.info("点击“%s”。", primary[0])
            primary[1].click(timeout=10_000)
            page.wait_for_timeout(1_500)
            if wait_for_success(page, config, time.monotonic() + 2):
                logger.info("打卡成功。")
                return "checked-in"

            quick_fill = unique_exact(page, config["quick_fill_buttons"])
            if quick_fill is not None:
                logger.info("使用“%s”。", quick_fill[0])
                quick_fill[1].click(timeout=10_000)
                page.wait_for_timeout(1_000)

            empty_required = visible_required_fields(page)
            if empty_required:
                save_screenshot(page, "required-fields-empty", logger)
                labels = "、".join(field["name"] or field["tag"] for field in empty_required)
                raise CheckinError(f"仍有必填项为空（{labels}），没有提交。")

            submit = unique_exact(page, config["submit_buttons"])
            if submit is None:
                save_screenshot(page, "submit-not-found", logger)
                raise CheckinError("进入打卡表单后没有找到受信任的提交按钮。")
            logger.info("提交打卡：%s。", submit[0])
            submit[1].click(timeout=10_000)
            page.wait_for_timeout(1_000)

            confirmation = unique_exact(page, config["confirmation_buttons"])
            if confirmation is not None:
                logger.info("二次确认：%s。", confirmation[0])
                confirmation[1].click(timeout=10_000)

            deadline = time.monotonic() + float(config["verify_timeout_seconds"])
            if wait_for_success(page, config, deadline):
                logger.info("打卡成功，已看到完成状态。")
                return "checked-in"

            save_screenshot(page, "unverified", logger)
            raise CheckinError("提交后未能确认打卡成功；脚本不会继续重复点击。")
        finally:
            context.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="接龙管家网页端每日自动打卡")
    parser.add_argument("--setup", action="store_true", help="首次扫码并保存网页登录态")
    parser.add_argument("--diagnose", action="store_true", help="只检查，不执行打卡")
    parser.add_argument("--target-title", help="目标打卡活动的完整标题")
    parser.add_argument("--active-from", help="活动开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--active-until", help="活动结束日期，格式 YYYY-MM-DD")
    parser.add_argument("--timeout", type=int, default=600, help="首次扫码等待秒数")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.verbose)
    try:
        config = load_config()
        if args.setup:
            if args.target_title:
                config["target_title"] = args.target_title.strip()
            if args.active_from:
                date.fromisoformat(args.active_from)
                config["active_from"] = args.active_from
            if args.active_until:
                date.fromisoformat(args.active_until)
                config["active_until"] = args.active_until
            if not config["target_title"]:
                raise CheckinError(
                    "首次配置必须通过 --target-title 指定目标活动的完整标题。"
                )
        result = (
            setup_login(config, logger, max(30, args.timeout))
            if args.setup
            else perform_daily(config, logger, args.diagnose)
        )
        logger.info("本次结果：%s", result)
        return 0
    except (CheckinError, ValueError, OSError, json.JSONDecodeError) as exc:
        logger.error("本次未完成：%s", exc)
        return 2
    except Exception:
        logger.exception("发生未预期错误；脚本已停止。")
        return 3


if __name__ == "__main__":
    sys.exit(main())
