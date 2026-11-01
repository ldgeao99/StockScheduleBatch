import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import crawl_ipo_schedule as crawler

from crawl_ipo_schedule import (
    extract_confirmed_price,
    has_confirmed_price,
    parse_offer_price,
)


class OfferPriceTests(unittest.TestCase):
    def test_nested_tables_do_not_overwrite_price_with_unrelated_number(self):
        html = """
        <table><tr><td>
          <table><tr><td>확정공모가</td><td><b>12,400</b> 원</td></tr></table>
        </td><td>1</td></tr></table>
        <table><tr><td>확정공모가 대비 수익률</td><td>1%</td></tr></table>
        <table><tr><td>이에스티, 확정공모가 12,400원</td>
          <td>동원파츠, 확정공모가 2</td></tr></table>
        """
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(extract_confirmed_price(soup), "12,400원")

    def test_unconfirmed_price_does_not_use_other_numbers(self):
        soup = BeautifulSoup("""
        <table><tr><td>확정공모가</td><td>-</td>
          <td>희망공모가</td><td>10,000~12,400원</td></tr></table>
        <table><tr><td>확정공모가 대비</td><td>1</td></tr></table>
        """, "html.parser")
        self.assertEqual(extract_confirmed_price(soup), "")

    def test_price_formats_and_invalid_values(self):
        for value in ("12,400원", " 12400 원 ", "12,400\n원"):
            with self.subTest(value=value):
                self.assertEqual(parse_offer_price(value), "12,400원")
        for value in ("", "-", "미정", "0원", "1원", "1%", "12,400~15,000원",
                      "12,400원 (1주)", "12,40원"):
            with self.subTest(value=value):
                self.assertEqual(parse_offer_price(value), "")

    def test_bad_saved_price_is_retried(self):
        for value in ("1원", "2원", "99원", "미정", "", "0원", "1%"):
            with self.subTest(value=value):
                detail = f"정전척 개발 및 제조\n공모가: {value}\n유통가능물량: 5,243,400주(37.70%)"
                self.assertFalse(has_confirmed_price(detail))
        self.assertTrue(has_confirmed_price("정전척 개발 및 제조\n공모가: 12,400원"))
        self.assertFalse(has_confirmed_price("공모가 정보 없음"))


class ExistingEventTests(unittest.TestCase):
    def run_crawler(self, price="12,400원", status=200, error=None, today=None,
                    stock_name="이에스티", listing_date="2026/11/04"):
        document = Mock()
        document.to_dict.return_value = {
            "eventName": f"{stock_name} 상장",
            "detail": "정전척 개발 및 제조\n공모가: 10,000원\n"
                      "유통가능물량: 5,243,400주(37.70%)\n유통가능액수: 약 524억 원 🔵",
        }
        events = Mock()
        events.where.return_value.where.return_value.get.return_value = [document]
        listing = Mock(status_code=200, text=f"""
        <table summary="신규상장종목"><tr>
        <td><a href="./?o=v&no=2332">{stock_name}</a></td>
        <td>{listing_date}</td><td>-</td><td>-</td></tr></table>
        """)
        detail = Mock(status_code=status, text=f"""
        <table><tr><td>확정공모가</td><td>{price}</td></tr></table>
        """)
        session = Mock()
        session.get.side_effect = [listing, error or detail]
        with patch.object(crawler, "events_ref", events), \
                patch.object(crawler, "logs_ref", None), \
                patch.object(crawler, "datetime", wraps=datetime) as clock, \
                patch.object(crawler.requests, "Session", return_value=session), \
                redirect_stdout(StringIO()):
            clock.now.return_value = today or datetime(2026, 11, 2, tzinfo=ZoneInfo("Asia/Seoul"))
            crawler.run_stock_crawler()
        if clock.now.return_value.date() > datetime.strptime(listing_date, "%Y/%m/%d").date():
            self.assertEqual(session.get.call_count, 1)
            events.where.assert_not_called()
            document.reference.update.assert_not_called()
        else:
            self.assertEqual(session.get.call_count, 2)
        clock.now.assert_any_call(ZoneInfo("Asia/Seoul"))
        events.add.assert_not_called()
        return document

    def test_confirmed_price_is_refreshed_and_amount_recalculated(self):
        document = self.run_crawler()
        document.reference.update.assert_called_once()
        detail = document.reference.update.call_args.args[0]["detail"]
        self.assertIn("공모가: 12,400원", detail)
        self.assertIn("유통가능액수: 약 650억 원", detail)
        self.assertTrue(detail.startswith("정전척 개발 및 제조\n"))

    def test_past_listing_skips_database_and_detail_requests(self):
        self.run_crawler(today=datetime(2026, 11, 5, tzinfo=ZoneInfo("Asia/Seoul")))

    def test_today_listing_is_still_refreshed(self):
        document = self.run_crawler(today=datetime(2026, 11, 4, tzinfo=ZoneInfo("Asia/Seoul")))
        document.reference.update.assert_called_once()

    def test_nexeye_existing_confirmed_price_is_corrected(self):
        document = self.run_crawler(
            stock_name="넥스아이", listing_date="2026/11/12", price="8,000원")
        document.reference.update.assert_called_once()
        payload = document.reference.update.call_args.args[0]
        self.assertEqual(payload["eventName"], "넥스아이 상장")
        self.assertIn("공모가: 8,000원", payload["detail"])

    def test_unchanged_price_does_not_write(self):
        document = self.run_crawler(price="10,000원")
        document.reference.update.assert_not_called()

    def test_missing_price_preserves_existing_price(self):
        document = self.run_crawler(price="-")
        document.reference.update.assert_not_called()

    def test_failed_request_preserves_existing_event(self):
        for options in ({"status": 503}, {"error": crawler.requests.Timeout("timeout")}):
            with self.subTest(options=options):
                document = self.run_crawler(**options)
                document.reference.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
