import io
import unittest
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import crawl_ipo_schedule as crawler

from crawl_ipo_schedule import (
    extract_confirmed_price,
    extract_dart_rcp_nos,
    extract_institution_unlocked,
    extract_post_ipo_total_shares,
    extract_retail_allocation,
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


FIXTURES = Path(__file__).parent / "tests" / "fixtures"
DART_LINK = "<a href=\"javascript:viewDetail2('20261029000504')\">증권신고서</a>"


class PostIpoSharesTests(unittest.TestCase):
    def test_real_page_table(self):
        html = (FIXTURES / "optonics_float.html").read_text()
        self.assertEqual(extract_post_ipo_total_shares(BeautifulSoup(html, "html.parser")), "9,566,000주")

    def test_header_variants_skip_locked_and_float_columns(self):
        # '공모후' 아래에 보유/매각제한/유통가능이 함께 있는 3단 헤더와 '공모후합계' 라벨
        soup = BeautifulSoup("""
        <table><tr><td rowspan="3">구분</td><td colspan="2" rowspan="2">공모전</td>
          <td colspan="6">공모후</td></tr>
        <tr><td colspan="2">보유주식</td><td colspan="2">매각제한물량</td><td colspan="2">유통가능물량</td></tr>
        <tr><td>주식수</td><td>지분율</td><td>주식수</td><td>지분율</td><td>주식수</td><td>지분율</td>
          <td>주식수</td><td>지분율</td></tr>
        <tr><td>소계</td><td>1</td><td>1%</td><td>132,000</td><td>0.74%</td><td>1</td><td>1%</td><td>1</td><td>1%</td></tr>
        <tr><td>공모후합계</td><td>13,197,964</td><td>100%</td><td>17,729,964</td><td>100%</td>
          <td>12,264,583</td><td>69.17%</td><td>5,465,381</td><td>30.83%</td></tr></table>
        """, "html.parser")
        self.assertEqual(extract_post_ipo_total_shares(soup), "17,729,964주")

    def test_missing_table(self):
        soup = BeautifulSoup("<table><tr><td>자산총계</td><td>1,000</td></tr></table>", "html.parser")
        self.assertEqual(extract_post_ipo_total_shares(soup), "")


class DartAllocationTests(unittest.TestCase):
    def test_real_issuance_report_tables(self):
        soup = BeautifulSoup((FIXTURES / "optonics_dart_allocation.html").read_text(), "html.parser")
        self.assertEqual(extract_retail_allocation(soup), 550_000)
        self.assertEqual(extract_institution_unlocked(soup), 333_705)

    def test_dash_means_zero_and_missing_tables_return_none(self):
        soup = BeautifulSoup("""
        <p>의무보유확약기간별 배정현황</p><table><tr><td>(단위 : 주, %)</td></tr></table>
        <table><tr><td>확약기간</td><td>수량</td><td>비중</td></tr>
        <tr><td>미확약</td><td>-</td><td>-</td></tr></table>
        """, "html.parser")
        self.assertEqual(extract_institution_unlocked(soup), 0)
        self.assertIsNone(extract_retail_allocation(soup))

    def test_rcp_numbers_and_report_lookup(self):
        html = ("<a href=\"javascript:viewDetail2('20261029000504')\">a</a>"
                "<a href=\"javascript:viewDetail2('20261021000210')\">b</a>"
                "<a href=\"javascript:viewDetail2('20261029000504')\">c</a>")
        self.assertEqual(extract_dart_rcp_nos(html), ["20261029000504", "20261021000210"])
        main = '<option value="rcpNo=20261110000258" title="증권발행실적보고서">'
        self.assertEqual(crawler.find_issuance_report_rcp_no(main), "20261110000258")
        self.assertEqual(crawler.find_issuance_report_rcp_no('<option title="투자설명서">'), "")


class BusinessSectionTests(unittest.TestCase):
    def test_section_starts_at_business_heading_not_dates(self):
        text = "수요예측일 2026.11.02 ~ 2026.11.06 " + "가" * 3000 + " 1.사업현황 -당사는 암세포 스크리닝"
        chunk = crawler.extract_business_section(text)
        self.assertTrue(chunk.startswith("1.사업현황 -당사는"))

    def test_missing_section(self):
        self.assertEqual(crawler.extract_business_section("공모청약일 2026.11.12"), "")

    def test_placeholder_summary_is_retried_for_existing_event(self):
        with patch.object(crawler, "summarize_business_with_ai", return_value="항암제 스크리닝 플랫폼") as ai:
            document = ExistingEventTests().run_crawler(
                float_html="<p>1. 사업현황 -당사는 항암제 스크리닝</p>",
                existing_detail="참조 가능한 정보 없음\n공모가: 10,000원")
        ai.assert_called_once()
        detail = document.reference.update.call_args.args[0]["detail"]
        self.assertTrue(detail.startswith("항암제 스크리닝 플랫폼\n"))

    def test_real_summary_is_not_regenerated(self):
        with patch.object(crawler, "summarize_business_with_ai") as ai:
            ExistingEventTests().run_crawler(float_html="<p>1. 사업현황 -당사는</p>")
        ai.assert_not_called()


class OpenDartTests(unittest.TestCase):
    XML = """<DOCUMENT><BODY>
    <P>4. 일반투자자 세부 배정현황</P>
    <TABLE><TBODY><TR><TH ROWSPAN="2">청약단위</TH><TH COLSPAN="2">배정</TH><TH ROWSPAN="2">총배정수량(D=B+C)</TH></TR>
    <TR><TH>균등</TH><TH>비례</TH></TR>
    <TR><TE>20</TE><TE>1</TE><TE>2</TE><TE>205,188</TE></TR>
    <TR><TE>합 계</TE><TE>275,000</TE><TE>275,000</TE><TE>550,000</TE></TR></TBODY></TABLE>
    <P>5. 기관투자자 의무보유확약기간별 배정현황</P>
    <TABLE><TR><TE>(단위 : 주, %)</TE></TR></TABLE>
    <TABLE><TR><TH>확약기간</TH><TH>수량</TH><TH>비중</TH></TR>
    <TR><TE>미확약</TE><TE>333,705</TE><TE>21.22</TE></TR></TABLE>
    </BODY></DOCUMENT>"""

    def responses(self, list_status="000"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("20261110000258.xml", self.XML)
        listing = Mock()
        listing.json.return_value = {"status": list_status, "total_page": 1, "list": [
            {"corp_name": "옵토닉스", "report_nm": "증권발행실적보고서", "rcept_no": "20261110000258"},
            {"corp_name": "옵토닉스", "report_nm": "투자설명서", "rcept_no": "20261029000524"},
        ]}
        return [listing, Mock(content=buffer.getvalue(), text="")]

    def test_api_report_is_parsed(self):
        with patch.object(crawler.requests, "get", side_effect=self.responses()) as get, \
                patch.dict(crawler.OPENDART_CACHE, clear=True), \
                patch.dict(crawler.os.environ, {"OPENDART_API_KEY": "k"}), redirect_stdout(StringIO()):
            self.assertEqual(crawler.fetch_allocation("옵토닉스", []), (550_000, 333_705))
        self.assertEqual(get.call_args_list[1].kwargs["params"]["rcept_no"], "20261110000258")

    def test_unfiled_report_returns_none(self):
        with patch.object(crawler.requests, "get", side_effect=self.responses()), \
                patch.dict(crawler.OPENDART_CACHE, clear=True), \
                patch.dict(crawler.os.environ, {"OPENDART_API_KEY": "k"}), redirect_stdout(StringIO()):
            self.assertIsNone(crawler.fetch_allocation("아델", []))

    def test_api_error_falls_back_to_web(self):
        with patch.object(crawler.requests, "get", side_effect=self.responses("020")), \
                patch.object(crawler, "fetch_dart_allocation", return_value=(1, 2)) as web, \
                patch.dict(crawler.OPENDART_CACHE, clear=True), \
                patch.dict(crawler.os.environ, {"OPENDART_API_KEY": "k"}), redirect_stdout(StringIO()):
            self.assertEqual(crawler.fetch_allocation("옵토닉스", ["20261029000504"]), (1, 2))
        web.assert_called_once_with(["20261029000504"])


class ExistingEventTests(unittest.TestCase):
    def run_crawler(self, price="12,400원", status=200, error=None, today=None,
                    stock_name="이에스티", listing_date="2026/11/04", float_html="",
                    existing_detail=None, allocation=None, dart_error=None, expect_dart=True):
        document = Mock()
        document.to_dict.return_value = {
            "eventName": f"{stock_name} 상장",
            "detail": "정전척 개발 및 제조\n공모가: 10,000원\n유통가능물량: 5,243,400주(37.70%)\n"
                      "유통가능액수: 약 524억 원 🔵\n\n공모후 주식수 합계: 13,908,000주\n"
                      "일반투자자 배정: 3,000,000주\n기관투자자 배정 미확약: 2,243,400주",
        }
        if existing_detail is not None:
            document.to_dict.return_value["detail"] = existing_detail
        events = Mock()
        events.where.return_value.where.return_value.get.return_value = [document]
        listing = Mock(status_code=200, text=f"""
        <table summary="신규상장종목"><tr>
        <td><a href="./?o=v&no=2332">{stock_name}</a></td>
        <td>{listing_date}</td><td>-</td><td>-</td></tr></table>
        """)
        detail = Mock(status_code=status, text=f"""
        <table><tr><td>확정공모가</td><td>{price}</td></tr></table>{float_html}
        {DART_LINK if allocation or dart_error else ""}
        """)
        session = Mock()
        session.get.side_effect = [listing, error or detail]
        with patch.object(crawler, "events_ref", events), \
                patch.object(crawler, "logs_ref", None), \
                patch.object(crawler, "datetime", wraps=datetime) as clock, \
                patch.object(crawler.requests, "Session", return_value=session), \
                patch.object(crawler, "fetch_dart_allocation", return_value=allocation,
                             side_effect=dart_error) as dart, \
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
        if (allocation or dart_error) and expect_dart:
            dart.assert_called_once_with(["20261029000504"])
        elif not expect_dart:
            dart.assert_not_called()
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

    def test_optonics_detail_uses_dart_allocation(self):
        html = (FIXTURES / "optonics_float.html").read_text()
        document = self.run_crawler(
            stock_name="옵토닉스", listing_date="2026/11/17", price="12,000원", float_html=html,
            existing_detail="광학식 자이로스코프 제조 및 개발\n공모가: 12,000원",
            allocation=(550_000, 333_705))
        detail = document.reference.update.call_args.args[0]["detail"]
        self.assertEqual(detail, "광학식 자이로스코프 제조 및 개발\n"
                                 "공모가: 12,000원\n"
                                 "유통가능물량: 883,705주(9.24%)\n"
                                 "유통가능액수: 약 106억 원 🔵\n"
                                 "\n"
                                 "공모후 주식수 합계: 9,566,000주\n"
                                 "일반투자자 배정: 550,000주\n"
                                 "기관투자자 배정 미확약: 333,705주")

    def test_missing_dart_report_is_marked_pending(self):
        document = self.run_crawler(existing_detail="정전척 개발 및 제조\n공모가: 10,000원")
        detail = document.reference.update.call_args.args[0]["detail"]
        self.assertIn("유통가능물량: 증권발행실적보고서 공시 대기중", detail)
        self.assertIn("유통가능액수: 증권발행실적보고서 공시 대기중", detail)

    def test_stored_allocation_skips_dart_lookup(self):
        document = self.run_crawler(price="10,000원", allocation=(1, 1), expect_dart=False)
        document.reference.update.assert_not_called()

    def test_dart_connection_failure_is_not_reported_as_pending(self):
        with patch.object(crawler.requests, "get", side_effect=crawler.requests.ConnectTimeout("t")) as get, \
                patch.dict(crawler.DART_STATE, {"down": False}), \
                patch.object(crawler, "DART_RETRY_WAITS", (0, 0)), redirect_stdout(StringIO()):
            with self.assertRaises(crawler.DartUnavailable):
                crawler.fetch_dart_allocation(["20261110000253", "20261015000329"])
            with self.assertRaises(crawler.DartUnavailable):
                crawler.fetch_dart_allocation(["20261029000504"])
        self.assertEqual(get.call_count, 3)  # 재시도 2회 후 포기, 이후 같은 실행에서는 요청하지 않음

    def test_dart_failure_label_in_detail(self):
        document = self.run_crawler(existing_detail="정전척 개발 및 제조\n공모가: 10,000원",
                                    dart_error=crawler.DartUnavailable("down"))
        detail = document.reference.update.call_args.args[0]["detail"]
        self.assertIn("유통가능물량: DART 조회 실패 - 다음 실행 때 재조회", detail)
        self.assertNotIn("공시 대기중", detail)

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
