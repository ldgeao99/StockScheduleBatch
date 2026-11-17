import requests
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import urllib3
import ssl
import re
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from crawler_schedule import update_my_schedule
from openai import OpenAI

# 정수 변환 제한 확장
sys.set_int_max_str_digits(10000)

# 1. 환경 설정 및 클라이언트 초기화
FIREBASE_KEY_PATH = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"

if os.path.exists(FIREBASE_KEY_PATH):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_KEY_PATH
    db = firestore.Client(project="stockcalender-13042")
else:
    print(f"⚠️ [경고] 파이어베이스 인증 파일({FIREBASE_KEY_PATH})을 찾을 수 없습니다.")
    print("로컬 드라이런 모드로 계속 진행합니다.")
    db = None

openai_key = os.environ.get("OPENAI_API_KEY")
if openai_key:
    ai_client = OpenAI(api_key=openai_key)
else:
    ai_client = None
    print("⚠️ [경고] OPENAI_API_KEY 환경 변수가 없습니다. GPT 요약 기능은 생략됩니다.")

events_ref = db.collection("events") if db else None
logs_ref = db.collection("crawler_logs") if db else None


def has_confirmed_price(detail_text: str) -> bool:
    """잘못 수집된 1원 등은 재조회하고 정상 확정가만 스킵한다."""
    for line in detail_text.splitlines():
        match = re.fullmatch(r"공모가:\s*(.+)", line.strip())
        if match and parse_offer_price(match.group(1)):
            return True
    return False


def parse_offer_price(value: str) -> str:
    """가격 한 개만 허용한다. 범위, 비율, 설명의 숫자를 합치지 않는다."""
    match = re.fullmatch(r"\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*원?\s*", value)
    if not match:
        return ""
    price = int(match.group(1).replace(",", ""))
    # 뉴스 제목 끝의 한두 자리 숫자가 가격으로 저장된 기존 오류도 재조회한다.
    return f"{price:,}원" if price >= 100 else ""


def extract_confirmed_price(soup) -> str:
    """중첩 표의 조상 셀과 다른 항목을 제외하고 확정공모가의 값만 읽는다."""
    for label in soup.find_all(["th", "td"]):
        if label.find("table"):
            continue
        if re.sub(r"\s+", "", label.get_text()) != "확정공모가":
            continue
        row = label.parent
        if row.name != "tr":
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        index = next(i for i, cell in enumerate(cells) if cell is label)
        if index + 1 < len(cells) and not cells[index + 1].find("table"):
            price = parse_offer_price(cells[index + 1].get_text())
            if price:
                return price
    return ""


def expand_table_grid(table):
    """rowspan/colspan을 펼쳐 헤더와 데이터의 실제 열 위치를 맞춘다."""
    grid = {}
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while (row_index, column) in grid:
                column += 1
            text = re.sub(r"\s+", "", cell.get_text()) if not cell.find("table") else ""
            rowspan = int(cell.get("rowspan", 1))
            colspan = int(cell.get("colspan", 1))
            for r in range(row_index, min(row_index + rowspan, len(rows))):
                for c in range(column, column + colspan):
                    grid[r, c] = text
            column += colspan
    width = max((column for _, column in grid), default=-1) + 1
    return [[grid.get((r, c), "") for c in range(width)] for r in range(len(rows))]


def parse_share_count(value: str):
    """'1,234' -> 1234, '-'은 0으로 본다. 숫자 형식이 아니면 None."""
    value = re.sub(r"\s+", "", value or "").removesuffix("주")
    if value in ("-", "0"):
        return 0
    if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+", value):
        return int(value.replace(",", ""))
    return None


def extract_post_ipo_total_shares(soup) -> str:
    """주주구성 표에서 '공모 후' 보유 주식수 열(매각제한·유통가능·지분율 제외)의 합계 행 값을 읽는다."""
    seen = []
    for cell in soup.find_all(["th", "td"]):
        label = re.sub(r"\s+", "", cell.get_text())
        if cell.find("table") or not label.startswith("공모후") or len(label) > 20:
            continue
        table = cell.find_parent("table")
        if table is None or any(table is item for item in seen):
            continue
        seen.append(table)
        rows = expand_table_grid(table)
        # 숫자가 처음 나오는 행 이전까지만 헤더로 본다. ('공모후합계' 같은 데이터 라벨 오인 방지)
        header_rows = []
        for row in rows[:5]:
            if any(re.fullmatch(r"[0-9][0-9,]*", value) for value in row):
                break
            header_rows.append(row)
        columns = []
        for c in range(len(rows[0]) if rows else 0):
            labels = [row[c] for row in header_rows]
            if not any(label.startswith("공모후") for label in labels):
                continue
            if any(k in label for label in labels for k in ("매각제한", "유통가능", "지분율", "%")):
                continue
            columns.append(c)
        if not columns:
            continue
        column = columns[0]
        for row in reversed(rows):
            if re.fullmatch(r"(공모후)?(합계|총계|총합계)", row[0]):
                shares = parse_share_count(row[column])
                if shares:
                    return f"{shares:,}주"
    return ""


def tables_after(soup, label: str, limit: int = 3):
    """공백을 무시하고 라벨 텍스트 뒤의 표를 순서대로 돌려준다. (단위 표 등 앞쪽 보조 표 대비)"""
    pattern = re.compile(r"\s*".join(map(re.escape, label.replace(" ", ""))))
    for text in soup.find_all(string=pattern):
        table = text
        for _ in range(limit):
            table = table.find_next("table")
            if table is None:
                break
            yield expand_table_grid(table)


def extract_retail_allocation(soup):
    """일반투자자 세부 배정현황 표에서 총배정수량 열의 맨 아래(합계) 값을 읽는다."""
    for rows in tables_after(soup, "일반투자자 세부 배정현황"):
        for header_index, header in enumerate(rows[:4]):
            columns = [i for i, value in enumerate(header) if value.startswith("총배정수량")]
            if len(columns) != 1:
                continue
            column = columns[0]
            for row in reversed(rows[header_index + 1:]):
                if row[column].startswith("총배정수량"):
                    break
                shares = parse_share_count(row[column])
                if shares is not None:
                    return shares
    return None


def extract_institution_unlocked(soup):
    """의무보유확약기간별 배정현황 표의 미확약 행에서 오른쪽에서 두 번째 값(합계 수량)을 읽는다."""
    for rows in tables_after(soup, "의무보유확약기간별 배정현황"):
        for row in rows:
            if row and row[0] == "미확약" and len(row) >= 3:
                return parse_share_count(row[-2])
    return None


DART_BASE_URL = "https://dart.fss.or.kr"
DART_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def extract_dart_rcp_nos(detail_html: str):
    """38 상세페이지의 공시 링크(viewDetail2)에서 DART 접수번호를 최신순으로 읽는다."""
    found = []
    for rcp_no in re.findall(r"viewDetail2\('(\d{14})'\)", detail_html or ""):
        if rcp_no not in found:
            found.append(rcp_no)
    return found


class DartUnavailable(Exception):
    """DART 접속 자체가 안 되는 상태(미공시와 구분)."""


# 한 번 접속이 막히면 같은 실행 동안 재시도하지 않는다. (종목당 45초씩 낭비 방지)
DART_STATE = {"down": False}


def dart_get(url: str) -> str:
    if DART_STATE["down"]:
        raise DartUnavailable("이번 실행에서 DART 접속 불가로 판정됨")
    try:
        res = requests.get(url, headers=DART_HEADERS, timeout=(5, 15))
    except (requests.ConnectionError, requests.Timeout) as e:
        DART_STATE["down"] = True
        raise DartUnavailable(f"DART 접속 실패: {e}") from e
    res.raise_for_status()
    res.encoding = "utf-8"
    return res.text


def find_issuance_report_rcp_no(main_html: str) -> str:
    """DART 문서의 관련문서 목록에서 증권발행실적보고서 접수번호를 찾는다."""
    match = re.search(r'value="rcpNo=(\d{14})"[^>]*title="증권발행실적보고서"', main_html)
    return match.group(1) if match else ""


def parse_dart_toc(main_html: str):
    """DART 뷰어 목차(node1[...]) 블록을 dict 목록으로 파싱한다."""
    nodes = []
    for block in re.split(r"var node1 = \{\};", main_html)[1:]:
        node = dict(re.findall(r"node1\['(\w+)'\]\s*=\s*\"([^\"]*)\"", block))
        if node.get("dcmNo") and node.get("eleId"):
            nodes.append(node)
    return nodes


def fetch_dart_allocation(rcp_nos):
    """증권발행실적보고서에서 (일반투자자 배정, 기관 미확약) 수량을 가져온다.
    미공시면 None, 접속 불가면 DartUnavailable을 던진다."""
    report_rcp_no = ""
    for rcp_no in rcp_nos[:3]:
        try:
            report_rcp_no = find_issuance_report_rcp_no(
                dart_get(f"{DART_BASE_URL}/dsaf001/main.do?rcpNo={rcp_no}"))
        except DartUnavailable:
            raise
        except Exception as e:
            print(f"⚠️ DART 관련문서 조회 실패({rcp_no}): {e}")
        if report_rcp_no:
            break
    if not report_rcp_no:
        print("⏳ 증권발행실적보고서 미공시")
        return None

    nodes = parse_dart_toc(dart_get(f"{DART_BASE_URL}/dsaf001/main.do?rcpNo={report_rcp_no}"))
    # 청약 및 배정 목차를 우선 조회하고, 목차명이 다르면 나머지 목차도 순서대로 확인한다.
    nodes.sort(key=lambda n: 0 if "배정" in n.get("text", "") else 1)
    retail = unlocked = None
    for node in nodes:
        params = "&".join(f"{key}={node.get(key, '')}" for key in
                          ("rcpNo", "dcmNo", "eleId", "offset", "length", "dtd"))
        soup = BeautifulSoup(dart_get(f"{DART_BASE_URL}/report/viewer.do?{params}"), "html.parser")
        retail = retail if retail is not None else extract_retail_allocation(soup)
        unlocked = unlocked if unlocked is not None else extract_institution_unlocked(soup)
        if retail is not None and unlocked is not None:
            print(f"📑 DART 증권발행실적보고서({report_rcp_no}) 일반 {retail:,}주 / 미확약 {unlocked:,}주")
            return retail, unlocked
    print(f"⚠️ DART 증권발행실적보고서({report_rcp_no})에서 배정 표를 찾지 못함")
    return None


def clean_text(text):
    """인코딩 깨짐 및 유령 공백 문자를 완전히 박멸하는 함수"""
    if not text:
        return ""
    cleaned = re.sub(r'[\s\xa0\xad\t\n\r]+', ' ', text)
    return cleaned.strip()


SUMMARY_PLACEHOLDERS = ("참조 가능한 정보 없음", "신규상장 예정 종목", "")


def extract_business_section(page_text: str) -> str:
    """'1. 사업현황' 본문부터 잘라낸다. (청약일 '2026.11.' 같은 날짜의 '1.'에 걸리지 않게)"""
    text = clean_text(page_text)
    match = re.search(r"1\s*\.\s*사업\s*현황", text) or re.search(r"사업\s*현황", text)
    return text[match.start():match.start() + 3500] if match else ""


def summarize_business_with_ai(stock_name, business_raw_text):
    if not ai_client:
        return "신규상장 예정 종목"
    if not business_raw_text or "사업현황" not in business_raw_text:
        return "참조 가능한 정보 없음"

    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 주식 증권사 리서치 애널리스트야. 제공된 공모 기업의 사업 현황을 읽고, "
                        "증권사 리포트에서 사용하는 사업 분야를 한 줄로 요약해줘.\n"
                        "조건:\n1. 핵심 기술과 적용 산업만 포함한다.\n2. 고객사는 제외한다.\n"
                        "3. 주어는 사용하지 않는다.\n4. 기업명은 포함하지 않는다.\n"
                        "5. 명사형으로 끝낸다.\n6. 10~20자 내외로 작성한다.\n"
                        "7. '인공지능'키워드는 'AI'로 대체해줘\n8. 설명 없이 결과 한 줄만 출력한다."
                    )
                },
                {"role": "user", "content": f"기업명: {stock_name}\n\n[사업 현황 원문]\n{business_raw_text[:2500]}"}
            ],
            max_tokens=100,
            temperature=0.4
        )
        ai_result = response.choices[0].message.content.strip()
        ai_result = re.sub(r'^[\s\-\•\.\,\_]+', '', ai_result)
        return f"{ai_result}"
    except Exception as e:
        print(f"❌ GPT API 통신 실패: {str(e)}")
        return "신규상장 예정 종목"


class SSL38Adapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_ciphers('DEFAULT:@SECLEVEL=0')
        kwargs['ssl_context'] = context
        return super(SSL38Adapter, self).init_poolmanager(*args, **kwargs)


def run_stock_crawler():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 GPT AI 엔진 장착 크롤러 가동 (38커뮤니케이션 전용 SSL 튜닝 패치 적용)...")

    base_url = "https://www.38.co.kr"
    list_url = f"{base_url}/html/fund/index.htm?o=nw"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
    }

    # 💡 [디버깅 모니터링 엔진] 수집 결과 기록용 동적 버킷 정의
    processed_list = []
    skipped_list = []

    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = requests.Session()
        session.mount("https://", SSL38Adapter())

        response = session.get(list_url, headers=headers, verify=False, timeout=15)
        response.encoding = 'euc-kr'

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", {"summary": "신규상장종목"})
        if not table:
            raise Exception("상장 일정 테이블 요소를 찾을 수 없습니다.")

        rows = table.find_all("tr")
        success_count = 0
        update_count = 0
        skip_count = 0
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        current_year = today.year

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue

            stock_a = cols[0].find("a")
            if not stock_a or not stock_a.get("href"):
                continue

            stock_name = clean_text(stock_a.text)
            raw_date = clean_text(cols[1].text).replace(".", "/")

            if not stock_name or not raw_date or raw_date in ["-", "미정", "상장일"] or "종목명" in stock_name:
                continue

            stock_name = stock_name.split("(")[0].strip()
            stock_name = stock_name.replace("(주)", "").replace("주식회사", "").strip()

            # 파이어베이스에 저장할 일정 제목: 종목명 + ' 상장'
            event_name = f"{stock_name} 상장"

            try:
                if len(raw_date.split('/')[0]) == 4:
                    date_obj = datetime.strptime(raw_date, "%Y/%m/%d")
                else:
                    date_obj = datetime.strptime(f"{current_year}/{raw_date}", "%Y/%m/%d")
                formatted_date = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                continue

            # 과거 상장 종목은 DB와 상세 페이지를 조회하기 전에 제외한다.
            if date_obj.date() < today:
                skip_count += 1
                skipped_list.append({
                    "date": formatted_date,
                    "name": stock_name,
                    "reason": "신규상장일이 오늘(한국 시간)보다 과거"
                })
                continue

            # 기존 일정도 매 실행마다 상세 페이지를 조회하여 변경 사항을 반영한다.
            existing_doc = None
            existing_detail = ""
            if events_ref:
                # 신규 이름('종목명 상장') + 구 이름('종목명')을 함께 조회 → 이름이 바뀌어도 중복 없이 기존 문서를 찾음
                duplicate_query = events_ref.where(
                    filter=FieldFilter("date", "==", formatted_date)
                ).where(
                    filter=FieldFilter("eventName", "in", [event_name, stock_name])
                ).get()

                if len(duplicate_query) > 0:
                    existing_doc = duplicate_query[0]
                    existing_detail = existing_doc.to_dict().get("detail") or ""

                    print(f"🔄 {stock_name} - 기존 일정의 최신 공모가 재확인")

            detail_route = stock_a.get("href")
            if "index.htm" in detail_route:
                detail_route = detail_route.replace("index.htm", "").replace("?", "")

            detail_url = base_url + detail_route if detail_route.startswith(
                "/") else f"{base_url}/html/fund/{detail_route}"
            if "o=v" not in detail_url and "no=" in detail_url:
                detail_url = detail_url.replace("?", "?o=v&")

            print(f"\n🔍 [디버깅 대상 종목 시작] -------------------------------")
            print(f"🏢 종목명: {stock_name} | 🔗 URL: {detail_url}")

            if existing_doc:
                # 💡 이미 등록된 종목의 공모가 재확인 케이스: 기존에 AI가 요약해둔 사업 요약 첫 줄은
                #    그대로 재사용하고(비용 절감), 공모가/유통 정보만 최신화한다.
                detail_desc = existing_detail.split("\n")[0].strip() if existing_detail else "신규상장 예정 종목"
            else:
                detail_desc = "신규상장 예정 종목"
            confirmed_price = ""
            total_shares = ""
            retail_shares = None
            unlocked_shares = None
            floating_amount = ""
            detail_loaded = False
            dart_failed = False
            if existing_doc:
                # 일부 항목이 일시적으로 사라져도 기존에 수집한 값은 보존한다.
                for line in existing_detail.splitlines():
                    key, _, value = line.partition(":")
                    if key == "공모가":
                        confirmed_price = parse_offer_price(value)
                    elif key == "공모후 주식수 합계":
                        total_shares = value.strip()
                    elif key == "일반투자자 배정":
                        retail_shares = parse_share_count(value)
                    elif key == "기관투자자 배정 미확약":
                        unlocked_shares = parse_share_count(value)

            try:
                detail_res = session.get(detail_url, headers=headers, verify=False, timeout=15)
                detail_res.encoding = 'euc-kr'

                if detail_res.status_code == 200:
                    detail_soup = BeautifulSoup(detail_res.text, "html.parser")

                    # 1. 확정공모가 추적
                    confirmed_price = extract_confirmed_price(detail_soup) or confirmed_price

                    # 2. 주주구성 표의 '공모 후' 주식수 합계
                    total_shares = extract_post_ipo_total_shares(detail_soup) or total_shares

                    # 3. 유통가능물량은 DART 증권발행실적보고서의 배정 결과로 계산한다.
                    rcp_nos = extract_dart_rcp_nos(detail_res.text)
                    if rcp_nos:
                        try:
                            allocation = fetch_dart_allocation(rcp_nos)
                        except Exception as dart_e:
                            allocation = None
                            dart_failed = True
                            print(f"⚠️ {stock_name} DART 배정현황 조회 실패: {dart_e}")
                        if allocation:
                            retail_shares, unlocked_shares = allocation

                    detail_loaded = True

                    # 4. OpenAI 요약 파트 (신규 종목, 또는 기존 요약이 안내 문구뿐인 종목만 수행)
                    if not existing_doc or detail_desc in SUMMARY_PLACEHOLDERS:
                        target_chunk = extract_business_section(detail_soup.get_text())
                        if target_chunk:
                            detail_desc = summarize_business_with_ai(stock_name, target_chunk)
                            print(f"🤖 OpenAI 한줄 요약 변환: {detail_desc}")
            except Exception as sub_e:
                print(f"❌ {stock_name} 상세 데이터 가공 중 에러 발생: {str(sub_e)}")

            if existing_doc and not detail_loaded:
                skip_count += 1
                skipped_list.append({
                    "date": formatted_date,
                    "name": stock_name,
                    "reason": "상세 조회 실패 - 기존 정보 유지"
                })
                continue

            floating_shares = None
            if retail_shares is not None and unlocked_shares is not None:
                floating_shares = retail_shares + unlocked_shares

            # 금액 연산 파트
            if confirmed_price and floating_shares:
                try:
                    num_price = int(re.sub(r'[^\d]', '', confirmed_price))
                    num_shares = floating_shares
                    calc_amount_billion = round((num_price * num_shares) / 100000000)
                    if calc_amount_billion > 0:
                        color_circle = "🔴" if calc_amount_billion >= 1000 else "🔵"
                        floating_amount = f"약 {calc_amount_billion:,}억 원 {color_circle}"
                except Exception as calc_err:
                    print(f"⚠️ {stock_name} 유통가능액수 수식 연산 오류: {str(calc_err)}")

            # 구조 결합
            # 💡 확정 공모가가 아직 없는 경우에도 '미정' 상태를 명시적으로 남겨서
            #    다음 크롤링에서 공모가가 새로 확정됐는지 계속 재확인할 수 있게 한다.
            if confirmed_price:
                detail_desc = f"{detail_desc}\n공모가: {confirmed_price}"
            else:
                detail_desc = f"{detail_desc}\n공모가: 미정"

            if total_shares:
                detail_desc = f"{detail_desc}\n공모후 주식수 합계: {total_shares}"

            if floating_shares is not None:
                floating_text = f"{floating_shares:,}주"
                total_num = parse_share_count(total_shares)
                if total_num:
                    floating_text += f"({floating_shares / total_num * 100:.2f}%)"
                detail_desc = (f"{detail_desc}\n유통가능물량: {floating_text}"
                               f"\n일반투자자 배정: {retail_shares:,}주"
                               f"\n기관투자자 배정 미확약: {unlocked_shares:,}주")
            else:
                pending = "DART 조회 실패 - 다음 실행 때 재조회" if dart_failed else "증권발행실적보고서 공시 대기중"
                detail_desc = f"{detail_desc}\n유통가능물량: {pending}"

            if floating_amount:
                detail_desc = f"{detail_desc}\n유통가능액수: {floating_amount}"
            elif not confirmed_price:
                detail_desc = f"{detail_desc}\n유통가능액수: 공모가 확정 대기중"
            elif floating_shares is None:
                detail_desc = f"{detail_desc}\n유통가능액수: {pending}"

            if existing_doc:
                # 💡 기존에 등록된 일정 - detail 내용에 실질적인 변화(공모가 확정 등)가 있을 때만 갱신한다.
                if detail_desc == existing_detail and existing_doc.to_dict().get("eventName") == event_name:
                    skip_count += 1
                    skipped_list.append({
                        "date": formatted_date,
                        "name": stock_name,
                        "reason": "최신 정보 확인 완료 - 변경 없음"
                    })
                    print(f"⏭️ 최신 정보 확인 완료 - 변경 없음")
                    print(f"🔍 [디버깅 대상 종목 종료] -------------------------------\n")
                    continue

                if events_ref:
                    existing_doc.reference.update({"detail": detail_desc, "eventName": event_name})
                    print(f"🔄 공모가 등 변경 감지 - 기존 일정 갱신 완료")
                else:
                    print(f"📝 [드라이런 모드 로그 출력] 공모가 갱신 대상 날짜: {formatted_date} | 종목: {stock_name}")
                update_count += 1
            else:
                if events_ref:
                    payload = {
                        "date": formatted_date,
                        "category": "신규상장",
                        "eventName": event_name,
                        "detail": detail_desc,
                        "relatedStocks": "",
                        "url": detail_url
                    }
                    events_ref.add(payload)
                    print(f"✅ 데이터 최종 적재 성공 완료")
                else:
                    print(f"📝 [드라이런 모드 로그 출력] 날짜: {formatted_date} | 종목: {stock_name}")
                success_count += 1

            # 💡 정상 처리 완료 리스트 기록 추가
            processed_list.append({
                "date": formatted_date,
                "name": stock_name,
                "detail": detail_desc.replace("\n", " | ")
            })
            print(f"🔍 [디버깅 대상 종목 종료] -------------------------------\n")

        # 💡 [핵심 교정부] 최종 요약 현황 테이블 콘솔 전수 출력
        print("\n" + "=" * 80)
        print("📊 [최종 실행 보고서] 상장 일정 파이프라인 정밀 분석 결과")
        print("=" * 80)

        print(f"\n✅ 1. 파이어베이스 신규 적재 목록 (총 {len(processed_list)}건)")
        print("-" * 80)
        if not processed_list:
            print("  [알림] 새로 추가된 일정이 존재하지 않습니다.")
        else:
            for idx, item in enumerate(processed_list, start=1):
                print(f"  [{idx:02d}] 상장일: {item['date']} | 종목명: {item['name']}")
                print(f"       ↳ 데이터 요약: {item['detail']}")
        print("-" * 80)

        print(f"\n⏭️  2. 데이터베이스 중복 스킵 목록 (총 {len(skipped_list)}건)")
        print("-" * 80)
        if not skipped_list:
            print("  [알림] 중복으로 인한 스킵 건수가 없습니다. 깨끗한 원천 데이터 상태입니다.")
        else:
            for idx, item in enumerate(skipped_list, start=1):
                print(f"  [{idx:02d}] 등록일: {item['date']} | 종목명: {item['name']:<12} ➡️ 사유: {item['reason']}")
        print("-" * 80)

        if logs_ref:
            log_payload = {
                "timestamp": firestore.SERVER_TIMESTAMP,
                "status": "SUCCESS",
                "task_name": "[crawl_ipo_schedule] 38커뮤니케이션 IPO일정 수집",
                "added_count": success_count,
                "updated_count": update_count,
                "skipped_count": skip_count,
                "message": f"AI 수집 자동화 정상 종료 - 신규: {success_count}건, 공모가 갱신: {update_count}건, 중복 스킵: {skip_count}건"
            }
            logs_ref.add(log_payload)
        print(f"\n🏁 AI 파이프라인 프로세스 종료. 추가: {success_count}건 / 공모가 갱신: {update_count}건 / 스킵: {skip_count}건")
        print("=" * 80 + "\n")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 에러 발생: {error_msg}")
        if logs_ref:
            logs_ref.add({
                "timestamp": firestore.SERVER_TIMESTAMP,
                "status": "FAILED",
                "task_name": "[crawl_ipo_schedule] 38커뮤니케이션 IPO일정 수집",
                "added_count": 0,
                "skipped_count": 0,
                "message": f"크롤링 실패 에러 로그: {error_msg}"
            })


if __name__ == "__main__":
    # 실행 시 이 배치 자신의 다음 실행 예정시간만 Firestore(crawler_schedules)에 기록
    update_my_schedule(db, __file__, display_name="38커뮤니케이션 IPO일정 수집")

    run_stock_crawler()
