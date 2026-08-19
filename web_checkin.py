"""Browser-based daily check-in for Jielong Guanjia.

One-time setup opens a dedicated Edge profile for QR-code login and records
the target activity URL.  Daily runs reuse that local browser profile.  The
script does not export cookies/tokens or call undocumented private APIs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Sequence
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "web_config.json"
PROFILE_DIR = BASE_DIR / ".browser-profile"
LOG_DIR = BASE_DIR / "logs"
NOTIFICATION_STATE_PATH = LOG_DIR / "notification-state.json"
LOCK_PATH = BASE_DIR / ".web-checkin.lock"
LOCK_STALE_SECONDS = 20 * 60
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
    "radio_option": "",
    "other_text": "",
    "daily_quotes": [],
    "headless": False,
    "navigation_attempts": 3,
    "navigation_retry_seconds": 4,
    "reauth_wait_seconds": 300,
    "verify_timeout_seconds": 20,
}


class CheckinError(RuntimeError):
    """An expected, fail-closed automation error."""


class ReauthenticationRequired(CheckinError):
    """The website requires a person to scan a new WeChat login QR code."""


def serverchan_endpoint(send_key: str) -> str:
    """Return the official ServerChan endpoint without exposing the key in logs."""
    send_key = send_key.strip()
    if send_key.startswith("SCT"):
        return f"https://sctapi.ftqq.com/{quote(send_key, safe='')}.send"
    match = re.match(r"^sctp(\d+)t", send_key)
    if match:
        uid = match.group(1)
        return f"https://{uid}.push.ft07.com/send/{quote(send_key, safe='')}.send"
    raise CheckinError("SERVERCHAN_SENDKEY 格式无效，应以 SCT 或 sctp 开头。")


def load_serverchan_send_key() -> str:
    """Read the key from this process or directly from the Windows user registry."""
    send_key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if send_key:
        return send_key
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "SERVERCHAN_SENDKEY")
        return str(value).strip()
    except (FileNotFoundError, OSError):
        return ""


def send_wechat_notification(
    title: str, description: str, logger: logging.Logger
) -> bool:
    """Send one ServerChan notification; notification failure never blocks check-in."""
    send_key = load_serverchan_send_key()
    if not send_key:
        logger.debug("未配置 SERVERCHAN_SENDKEY，跳过微信通知。")
        return False
    try:
        payload = urlencode({"title": title[:32], "desp": description}).encode("utf-8")
        request = Request(
            serverchan_endpoint(send_key),
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if str(result.get("code")) != "0":
            logger.warning("微信通知服务返回失败状态，SendKey 未写入日志。")
            return False
        logger.info("微信通知已发送。")
        return True
    except Exception as exc:
        # HTTP exceptions can include the credential-bearing URL, so log only
        # the exception type and never its message.
        logger.warning("微信通知发送失败（%s），不影响打卡结果。", type(exc).__name__)
        return False


def load_notification_state() -> dict[str, Any]:
    try:
        with NOTIFICATION_STATE_PATH.open("r", encoding="utf-8") as file:
            state = json.load(file)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_notification_state(state: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = NOTIFICATION_STATE_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, NOTIFICATION_STATE_PATH)


def notify_once(
    category: str, title: str, description: str, logger: logging.Logger
) -> bool:
    """Send at most one success and one failure notification per local date."""
    if not load_serverchan_send_key():
        return False
    today_text = date.today().isoformat()
    state = load_notification_state()
    if state.get("date") != today_text:
        state = {"date": today_text}
    if state.get(category):
        logger.debug("今日 %s 通知已发送，跳过重复推送。", category)
        return False
    if not send_wechat_notification(title, description, logger):
        return False
    state[category] = datetime.now().isoformat(timespec="seconds")
    try:
        save_notification_state(state)
    except OSError as exc:
        logger.warning(
            "微信通知已发送，但去重状态保存失败（%s）；不影响打卡结果。",
            type(exc).__name__,
        )
    return True


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
    validate_quote_coverage(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


@contextmanager
def execution_lock() -> Iterable[None]:
    """Avoid concurrent runs corrupting the persistent browser profile."""
    token = f"pid={os.getpid()} started={datetime.now().isoformat()}"
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, token.encode("utf-8"))
            break
        except FileExistsError:
            try:
                is_stale = time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_SECONDS
            except FileNotFoundError:
                continue
            if is_stale and attempt == 0:
                try:
                    LOCK_PATH.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise CheckinError(
                "已有另一个打卡进程正在运行；为保护登录会话，本次不并发执行。"
            )
    if descriptor is None:
        raise CheckinError("无法获取打卡运行锁。")
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            if LOCK_PATH.read_text(encoding="utf-8") == token:
                LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


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


def normalize_button_label(value: str) -> str:
    return "".join(value.split())


def validate_quote_coverage(config: dict[str, Any]) -> None:
    quotes = [
        str(quote).strip()
        for quote in (config.get("daily_quotes") or [])
        if str(quote).strip()
    ]
    if not quotes:
        return
    if len(set(quotes)) != len(quotes):
        raise CheckinError("daily_quotes 中存在重复短句。")
    start = date.fromisoformat(config["active_from"])
    end = date.fromisoformat(config["active_until"])
    required_days = (end - start).days + 1
    if required_days > len(quotes):
        raise CheckinError(
            f"活动共 {required_days} 天，但只有 {len(quotes)} 条不同短句，"
            "无法保证每天不重复。"
        )


def quote_for_day(today: date, quotes: Sequence[str], active_from: str) -> str:
    cleaned = [str(quote).strip() for quote in quotes if str(quote).strip()]
    if not cleaned:
        return ""
    offset = (today - date.fromisoformat(active_from)).days
    if offset < 0 or offset >= len(cleaned):
        raise CheckinError("当前日期超出每日短句的不重复覆盖范围。")
    return cleaned[offset]


def compose_other_text(base_text: str, quote: str) -> str:
    base = base_text.strip()
    quote = quote.strip()
    if base and base[-1] not in "。！？；":
        base += "。"
    return f"{base}{quote}"


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


def wechat_login_prompt_visible(page: Any) -> bool:
    """Detect the visible QR-login dialog, including when it overlays an activity."""
    try:
        return bool(visible_exact(page, "微信登录"))
    except Exception:
        return False


def unique_exact(page: Any, labels: Sequence[str]) -> tuple[str, Any] | None:
    for label in labels:
        matches = visible_exact(page, label)
        if len(matches) == 1:
            return label, matches[0]
        if len(matches) > 1:
            raise CheckinError(f"页面存在多个“{label}”，为避免误点，已停止。")
    return None


def visible_exact_buttons(
    page: Any, label: str, scope_selector: str | None = None
) -> list[Any]:
    """Return real, visible button elements instead of their nested text spans."""
    root = page.locator(scope_selector) if scope_selector else page
    locator = root.get_by_role("button", name=label, exact=True)
    matches: list[Any] = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                matches.append(candidate)
        except Exception:
            continue
    return matches


def unique_exact_button(
    page: Any,
    labels: Sequence[str],
    scope_selector: str | None = None,
) -> tuple[str, Any] | None:
    for label in labels:
        matches = visible_exact_buttons(page, label, scope_selector)
        if len(matches) == 1:
            return label, matches[0]
        if len(matches) > 1:
            scope = f"（范围：{scope_selector}）" if scope_selector else ""
            raise CheckinError(f"页面存在多个“{label}”按钮{scope}，为避免误点，已停止。")
    return None


def unique_whitelisted_css_button(
    page: Any, selector: str, allowed_labels: Sequence[str]
) -> tuple[str, Any] | None:
    """Select one visible CSS-targeted button only after validating its exact text."""
    locator = page.locator(selector)
    matches: list[tuple[str, Any]] = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            label = normalize_button_label(candidate.inner_text())
            if label in allowed_labels:
                matches.append((label, candidate))
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CheckinError(f"选择器 {selector} 匹配多个白名单按钮，已停止。")
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


def visible_checkin_form(page: Any) -> Any | None:
    """Return one visible form across the site's old and new modal layouts."""
    for selector in (
        "#attend-checkin-modal",
        "[role='dialog']",
        ".ant-modal-content",
        "form",
    ):
        locator = page.locator(selector)
        matches: list[Any] = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    matches.append(candidate)
            except Exception:
                continue
        if len(matches) > 1:
            raise CheckinError(
                f"选择器 {selector} 匹配多个可见表单，已停止以避免误提交。"
            )
        if matches:
            return matches[0]
    return None


def checkin_form_scope(page: Any) -> Any:
    """Use the modal when present; support a future standalone check-in form."""
    return visible_checkin_form(page) or page


def open_target_with_retry(page: Any, config: dict[str, Any], logger: logging.Logger) -> None:
    attempts = max(1, int(config.get("navigation_attempts", 3)))
    pause_ms = max(0, int(float(config.get("navigation_retry_seconds", 4)) * 1_000))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(config["target_url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)
            return
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            logger.warning("打开活动页失败（第 %s/%s 次），将重试：%s", attempt, attempts, exc)
            page.wait_for_timeout(pause_ms)
    raise CheckinError(f"连续 {attempts} 次无法打开活动页：{last_error}")


def wait_for_checkin_form(
    page: Any, config: dict[str, Any], deadline: float
) -> str:
    """Wait for the form to hydrate instead of assuming it is ready after 1.5 s."""
    while time.monotonic() < deadline:
        if wechat_login_prompt_visible(page):
            raise ReauthenticationRequired("检测到微信登录二维码。")
        text = body_text(page)
        if contains_any(text, config["success_markers"]) or "/submit-success" in page.url:
            return "success"
        if visible_checkin_form(page) is not None:
            return "form"
        page.wait_for_timeout(500)
    return "timeout"


def request_login_or_verify_session(page: Any, config: dict[str, Any]) -> str:
    """Open the QR prompt when needed, or prove the page can open a check-in form.

    The activity itself is public, so merely loading its title does not prove that
    the browser is logged in.  Opening the trusted primary button is the first
    action that reliably yields either the QR prompt or a real check-in form.
    """
    if wechat_login_prompt_visible(page):
        return "login-required"
    if visible_checkin_form(page) is not None:
        return "authenticated"
    text = body_text(page)
    if contains_any(text, config["success_markers"]) or "/submit-success" in page.url:
        return "authenticated"
    primary = unique_exact_button(page, config["primary_buttons"])
    if primary is None:
        return "unknown"
    try:
        primary[1].click(timeout=10_000)
    except Exception:
        return "login-required" if wechat_login_prompt_visible(page) else "unknown"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if wechat_login_prompt_visible(page):
            return "login-required"
        if visible_checkin_form(page) is not None:
            return "authenticated"
        text = body_text(page)
        if contains_any(text, config["success_markers"]) or "/submit-success" in page.url:
            return "authenticated"
        page.wait_for_timeout(500)
    return "unknown"


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


def fill_configured_answers(
    page: Any, config: dict[str, Any], logger: logging.Logger
) -> None:
    option = str(config.get("radio_option") or "").strip()
    if not option:
        return

    form = checkin_form_scope(page)
    radios = form.get_by_role("radio", name=option, exact=True)
    visible_radios = []
    for index in range(radios.count()):
        candidate = radios.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                visible_radios.append(candidate)
        except Exception:
            continue
    if len(visible_radios) != 1:
        raise CheckinError(
            f"无法唯一识别单选项“{option}”（匹配 {len(visible_radios)} 个）。"
        )
    visible_radios[0].check(timeout=5_000)
    logger.info("已选择单选项“%s”。", option)

    if option != "其他":
        return
    other_text = str(config.get("other_text") or "").strip()
    if not other_text:
        raise CheckinError("选择“其他”时，配置中的 other_text 不能为空。")
    quote = quote_for_day(
        date.today(),
        config.get("daily_quotes") or [],
        config["active_from"],
    )
    other_text = compose_other_text(other_text, quote)

    textboxes = form.get_by_role("textbox")
    visible_textboxes = []
    for index in range(textboxes.count()):
        candidate = textboxes.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                visible_textboxes.append(candidate)
        except Exception:
            continue
    if len(visible_textboxes) != 1:
        raise CheckinError(
            "选择“其他”后无法唯一识别说明文本框"
            f"（匹配 {len(visible_textboxes)} 个）。"
        )
    visible_textboxes[0].fill(other_text, timeout=5_000)
    logger.info("已填写“其他”说明及当日短句。")


def wait_for_success(page: Any, config: dict[str, Any], deadline: float) -> bool:
    while time.monotonic() < deadline:
        text = body_text(page)
        if contains_any(text, config["success_markers"]):
            return True
        if "/submit-success" in page.url:
            return True
        time.sleep(1)
    return False


def scheduled_time_reached(now: datetime, not_before: str | None) -> bool:
    if not not_before:
        return True
    scheduled = datetime.strptime(not_before, "%H:%M").time()
    return now.time().replace(second=0, microsecond=0) >= scheduled


def perform_daily_once(
    config: dict[str, Any],
    logger: logging.Logger,
    diagnose: bool,
) -> str:
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        # The site has shown a QR-login dialog during headless scheduled runs
        # while the same persistent profile works in a normal Edge window.
        # Prefer a short-lived visible window for reliable daily operation.
        headless = bool(config.get("headless", False)) and not diagnose
        context = launch_context(playwright, headless=headless)
        try:
            # A new tab avoids restored dashboard/popup tabs from prior Edge sessions.
            page = context.new_page()
            open_target_with_retry(page, config, logger)
            if wechat_login_prompt_visible(page):
                save_screenshot(page, "login-required", logger)
                raise ReauthenticationRequired("检测到微信登录二维码。")
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

            primary = unique_exact_button(page, config["primary_buttons"])
            if primary is None:
                save_screenshot(page, "button-not-found", logger)
                raise CheckinError("没有找到受信任的打卡按钮，页面可能已经变化。")
            if diagnose:
                logger.info("诊断成功：未打卡时将点击“%s”；本次未点击。", primary[0])
                return "diagnose-ready"

            logger.info("点击“%s”。", primary[0])
            try:
                primary[1].click(timeout=10_000)
            except Exception as exc:
                if wechat_login_prompt_visible(page):
                    save_screenshot(page, "login-required", logger)
                    raise ReauthenticationRequired("点击打卡时出现微信登录二维码。") from exc
                raise CheckinError(f"点击打卡按钮失败：{exc}") from exc
            form_state = wait_for_checkin_form(page, config, time.monotonic() + 12)
            if form_state == "success":
                logger.info("打卡成功。")
                return "checked-in"
            if form_state == "timeout":
                save_screenshot(page, "form-not-ready", logger)
                raise CheckinError("打卡表单加载超时；本次未提交，稍后会由计划任务重试。")

            quick_fill = unique_exact(page, config["quick_fill_buttons"])
            if quick_fill is not None:
                logger.info("使用“%s”。", quick_fill[0])
                quick_fill[1].click(timeout=10_000)
                page.wait_for_timeout(1_500)

            if wechat_login_prompt_visible(page):
                save_screenshot(page, "login-required", logger)
                raise ReauthenticationRequired("填写前出现微信登录二维码。")

            try:
                fill_configured_answers(page, config, logger)
            except CheckinError:
                save_screenshot(page, "configured-answer-failed", logger)
                raise

            empty_required = visible_required_fields(checkin_form_scope(page))
            if empty_required:
                save_screenshot(page, "required-fields-empty", logger)
                labels = "、".join(field["name"] or field["tag"] for field in empty_required)
                raise CheckinError(f"仍有必填项为空（{labels}），没有提交。")

            submit = unique_whitelisted_css_button(
                page,
                "#attend-checkin-modal button.btn-submit",
                config["submit_buttons"],
            )
            if submit is None:
                modal_buttons = page.locator(
                    "#attend-checkin-modal button.btn-submit"
                ).evaluate_all(
                    """buttons => buttons.map(button => ({
                      text: (button.innerText || '').trim(),
                      disabled: Boolean(button.disabled),
                      ariaDisabled: button.getAttribute('aria-disabled'),
                      className: button.className,
                    }))"""
                )
                if modal_buttons:
                    save_screenshot(page, "modal-submit-unavailable", logger)
                    raise CheckinError(
                        f"弹窗提交按钮当前不可用或文案不在白名单：{modal_buttons}"
                    )
                # Some page versions navigate to a standalone form instead of a modal.
                submit = unique_exact_button(page, config["submit_buttons"])
            if submit is None:
                save_screenshot(page, "submit-not-found", logger)
                raise CheckinError("进入打卡表单后没有找到受信任的提交按钮。")
            logger.info("提交打卡：%s。", submit[0])
            try:
                submit[1].click(timeout=10_000)
            except Exception as exc:
                save_screenshot(page, "submit-click-failed", logger)
                raise CheckinError(f"提交按钮点击失败：{exc}") from exc
            page.wait_for_timeout(1_000)

            if wechat_login_prompt_visible(page):
                save_screenshot(page, "login-required", logger)
                raise ReauthenticationRequired("提交后出现微信登录二维码。")

            confirmation = unique_exact_button(page, config["confirmation_buttons"])
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


def reauthenticate_interactively(
    config: dict[str, Any], logger: logging.Logger
) -> None:
    """Open a visible QR code and wait briefly for the user to scan it."""
    wait_seconds = max(30, int(config.get("reauth_wait_seconds", 300)))
    sync_playwright = import_playwright()
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        try:
            page = context.new_page()
            open_target_with_retry(page, config, logger)
            try:
                page.bring_to_front()
            except Exception:
                pass
            deadline = time.monotonic() + wait_seconds
            login_prompt_announced = False
            while time.monotonic() < deadline:
                try:
                    session_state = request_login_or_verify_session(page, config)
                except Exception:
                    if page.is_closed():
                        logger.info("登录窗口已关闭，将使用已保存的登录状态继续验证。")
                        return
                    raise
                if session_state == "authenticated":
                    logger.info("已确认微信登录完成。")
                    return
                if session_state == "login-required" and not login_prompt_announced:
                    logger.warning(
                        "登录已过期：已打开 Edge 微信二维码。请扫码确认，脚本将等待 %s 秒后自动继续。",
                        wait_seconds,
                    )
                    notify_once(
                        "failure",
                        "接龙管家：需要扫码登录",
                        f"{date.today():%Y-%m-%d} 打卡尚未完成。"
                        "Edge 已打开微信二维码，请在 5 分钟内扫码；脚本会在登录后自动继续。",
                        logger,
                    )
                    login_prompt_announced = True
                try:
                    page.wait_for_timeout(1_000)
                except Exception:
                    if page.is_closed():
                        logger.info("登录窗口已关闭，将使用已保存的登录状态继续验证。")
                        return
                    raise
            save_screenshot(page, "reauth-timeout", logger)
            raise ReauthenticationRequired(
                f"微信登录二维码已显示，但在 {wait_seconds} 秒内未完成扫码。"
            )
        finally:
            context.close()


def perform_daily(
    config: dict[str, Any],
    logger: logging.Logger,
    diagnose: bool,
    not_before: str | None = None,
) -> str:
    if not date_is_active(date.today(), config):
        logger.info(
            "今天不在活动日期 %s 至 %s 内，跳过。",
            config["active_from"],
            config["active_until"],
        )
        return "outside-date-range"
    if not scheduled_time_reached(datetime.now(), not_before):
        logger.info("当前时间早于 %s，登录补跑触发器不提前打卡。", not_before)
        return "before-scheduled-time"
    if not config.get("target_url"):
        raise CheckinError("尚未完成首次扫码配置。请先运行 web_checkin.py --setup。")

    try:
        return perform_daily_once(config, logger, diagnose)
    except ReauthenticationRequired:
        if diagnose:
            raise
        reauthenticate_interactively(config, logger)
        logger.info("重新登录成功，开始重新执行本次打卡。")
        return perform_daily_once(config, logger, diagnose=False)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="接龙管家网页端每日自动打卡")
    parser.add_argument("--setup", action="store_true", help="首次扫码并保存网页登录态")
    parser.add_argument("--diagnose", action="store_true", help="只检查，不执行打卡")
    parser.add_argument(
        "--test-notification", action="store_true", help="发送一条微信通知测试"
    )
    parser.add_argument("--target-title", help="目标打卡活动的完整标题")
    parser.add_argument("--active-from", help="活动开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--active-until", help="活动结束日期，格式 YYYY-MM-DD")
    parser.add_argument(
        "--not-before",
        help="当天早于此时间时跳过，格式 HH:MM；用于登录后的补跑任务",
    )
    parser.add_argument("--timeout", type=int, default=600, help="首次扫码等待秒数")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.verbose)
    try:
        if args.test_notification:
            if not load_serverchan_send_key():
                raise CheckinError("尚未配置 SERVERCHAN_SENDKEY。")
            if not send_wechat_notification(
                "接龙管家通知测试",
                f"{datetime.now():%Y-%m-%d %H:%M:%S} 微信通知配置成功。",
                logger,
            ):
                raise CheckinError("微信测试通知发送失败，请检查 SendKey 和网络。")
            return 0
        with execution_lock():
            config = load_config()
            if args.not_before:
                datetime.strptime(args.not_before, "%H:%M")
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
                else perform_daily(config, logger, args.diagnose, args.not_before)
            )
        logger.info("本次结果：%s", result)
        if not args.setup and not args.diagnose and result in {
            "checked-in",
            "already-checked-in",
        }:
            notify_once(
                "success",
                "接龙管家：今日打卡成功",
                f"{date.today():%Y-%m-%d} 已确认完成打卡。结果：{result}。",
                logger,
            )
        return 0
    except ReauthenticationRequired as exc:
        logger.error("本次未完成（需要重新扫码）：%s", exc)
        if not args.test_notification:
            notify_once(
                "failure",
                "接龙管家：打卡未完成",
                f"{date.today():%Y-%m-%d} 需要重新扫码登录。后续时段仍会自动重试。",
                logger,
            )
        return 4
    except (CheckinError, ValueError, OSError, json.JSONDecodeError) as exc:
        logger.error("本次未完成：%s", exc)
        if not args.test_notification:
            notify_once(
                "failure",
                "接龙管家：打卡未完成",
                f"{date.today():%Y-%m-%d} 首次失败：{exc}。后续时段仍会自动重试。",
                logger,
            )
        return 2
    except Exception:
        logger.exception("发生未预期错误；脚本已停止。")
        if not args.test_notification:
            notify_once(
                "failure",
                "接龙管家：打卡异常",
                f"{date.today():%Y-%m-%d} 出现未预期错误；请查看本机日志。",
                logger,
            )
        return 3


if __name__ == "__main__":
    sys.exit(main())
