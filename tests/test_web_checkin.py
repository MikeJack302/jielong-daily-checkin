from datetime import date
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
