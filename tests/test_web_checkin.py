from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "web_checkin.py"
SPEC = importlib.util.spec_from_file_location("web_checkin", MODULE_PATH)
web_checkin = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = web_checkin
SPEC.loader.exec_module(web_checkin)


class WebCheckinCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "active_from": "2026-07-28",
            "active_until": "2026-08-31",
        }

    def test_date_range_is_inclusive(self) -> None:
        self.assertTrue(web_checkin.date_is_active(date(2026, 7, 28), self.config))
        self.assertTrue(web_checkin.date_is_active(date(2026, 8, 31), self.config))
        self.assertFalse(web_checkin.date_is_active(date(2026, 9, 1), self.config))

    def test_marker_matching(self) -> None:
        self.assertTrue(web_checkin.contains_any("状态：今日已打卡", ["今日已打卡"]))
        self.assertFalse(web_checkin.contains_any("尚未打卡", ["今日已打卡"]))

    def test_button_label_ignores_visual_spacing(self) -> None:
        self.assertEqual(web_checkin.normalize_button_label("提 交"), "提交")
        self.assertEqual(web_checkin.normalize_button_label(" 立即\n打卡 "), "立即打卡")

    def test_serverchan_endpoints(self) -> None:
        self.assertEqual(
            web_checkin.serverchan_endpoint("SCT123"),
            "https://sctapi.ftqq.com/SCT123.send",
        )
        self.assertEqual(
            web_checkin.serverchan_endpoint("sctp42tABC"),
            "https://42.push.ft07.com/send/sctp42tABC.send",
        )
        with self.assertRaises(web_checkin.CheckinError):
            web_checkin.serverchan_endpoint("invalid")

    def test_visible_wechat_login_prompt_is_detected(self) -> None:
        class Candidate:
            def is_visible(self) -> bool:
                return True

        class Locator:
            def count(self) -> int:
                return 1

            def nth(self, index: int) -> Candidate:
                return Candidate()

        class Page:
            def get_by_text(self, label: str, exact: bool) -> Locator:
                return Locator()

        self.assertTrue(web_checkin.wechat_login_prompt_visible(Page()))

    def test_public_activity_without_form_is_not_authenticated(self) -> None:
        class EmptyLocator:
            def count(self) -> int:
                return 0

        class Body:
            def inner_text(self, timeout: int) -> str:
                return "每晚打卡情况"

        class Page:
            url = "https://i.jielong.com/c/example"

            def get_by_text(self, label: str, exact: bool) -> EmptyLocator:
                return EmptyLocator()

            def locator(self, selector: str):
                if selector == "body":
                    return Body()
                return EmptyLocator()

            def get_by_role(self, role: str, name: str, exact: bool) -> EmptyLocator:
                return EmptyLocator()

        config = {
            "success_markers": ["今日已打卡"],
            "primary_buttons": ["立即打卡"],
        }
        self.assertEqual(
            web_checkin.request_login_or_verify_session(Page(), config), "unknown"
        )

    def test_daily_quote_is_deterministic(self) -> None:
        quotes = ["甲。", "乙。", "丙。"]
        first = web_checkin.quote_for_day(date(2026, 8, 13), quotes, "2026-08-13")
        second = web_checkin.quote_for_day(date(2026, 8, 13), quotes, "2026-08-13")
        self.assertEqual(first, second)
        self.assertIn(first, quotes)

    def test_consecutive_days_use_different_quotes(self) -> None:
        quotes = ["甲。", "乙。", "丙。"]
        values = [
            web_checkin.quote_for_day(date(2026, 8, 13 + offset), quotes, "2026-08-13")
            for offset in range(3)
        ]
        self.assertEqual(values, quotes)

    def test_quote_coverage_rejects_short_or_duplicate_lists(self) -> None:
        short = {
            "active_from": "2026-08-13",
            "active_until": "2026-08-15",
            "daily_quotes": ["甲。", "乙。"],
        }
        with self.assertRaises(web_checkin.CheckinError):
            web_checkin.validate_quote_coverage(short)
        duplicate = {
            "active_from": "2026-08-13",
            "active_until": "2026-08-14",
            "daily_quotes": ["甲。", "甲。"],
        }
        with self.assertRaises(web_checkin.CheckinError):
            web_checkin.validate_quote_coverage(duplicate)

    def test_other_text_contains_status_then_quote(self) -> None:
        self.assertEqual(
            web_checkin.compose_other_text("一切平安", "星星之火，可以燎原。"),
            "一切平安。星星之火，可以燎原。",
        )

    def test_logon_catch_up_does_not_run_early(self) -> None:
        self.assertFalse(
            web_checkin.scheduled_time_reached(
                datetime(2026, 8, 13, 5, 59), "06:00"
            )
        )
        self.assertTrue(
            web_checkin.scheduled_time_reached(
                datetime(2026, 8, 13, 6, 0), "06:00"
            )
        )
        self.assertTrue(
            web_checkin.scheduled_time_reached(
                datetime(2026, 8, 13, 9, 30), "06:00"
            )
        )

    def test_manual_run_has_no_time_gate(self) -> None:
        self.assertTrue(
            web_checkin.scheduled_time_reached(
                datetime(2026, 8, 13, 1, 0), None
            )
        )

    def test_default_login_uses_current_official_site(self) -> None:
        self.assertEqual(
            web_checkin.DEFAULT_CONFIG["login_url"], "https://i.jielong.com/"
        )

    def test_dashboard_card_is_not_treated_as_detail(self) -> None:
        class Locator:
            def inner_text(self, timeout: int) -> str:
                return "我参与的\n每晚打卡情况"

        class Page:
            url = "https://i.jielong.com/"

            def locator(self, selector: str) -> Locator:
                return Locator()

        self.assertFalse(
            web_checkin.is_target_activity_page(Page(), "每晚打卡情况")
        )

    def test_detail_route_is_recognized(self) -> None:
        class Locator:
            def inner_text(self, timeout: int) -> str:
                return "每晚打卡情况"

        class Page:
            url = "https://i.jielong.com/#/c/123456"

            def locator(self, selector: str) -> Locator:
                return Locator()

        self.assertTrue(web_checkin.is_target_activity_page(Page(), "每晚打卡情况"))


if __name__ == "__main__":
    unittest.main()
