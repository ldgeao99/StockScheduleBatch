from curl_cffi import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import re
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from crawler_schedule import update_my_schedule

# 대규모 인트 연산 제한 방지용 설정
sys.set_int_max_str_digits(10000)

# 파이어베이스 엔진 초기화
FIREBASE_KEY_PATH = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"

if os.path.exists(FIREBASE_KEY_PATH):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_KEY_PATH
    db = firestore.Client(project="stockcalender-13042")
else:
    print(f"⚠️ [경고] 파이어베이스 인증 파일({FIREBASE_KEY_PATH})을 찾을 수 없습니다.")
    print("로컬 드라이런 모드로 계속 진행합니다.")
    db = None

# 매일 변하는 시계열 데이터라 일정용 events 와 분리된 전용 컬렉션에 적재
flows_ref = db.collection("btc_etf_flows") if db else None
logs_ref = db.collection("crawler_logs") if db else None

TASK_NAME = "[crawl_btc_etf_flow] 비트코인 ETF 순매수 수집"

# 최근 30일치만 유지. 이보다 오래된 날짜는 저장하지 않고, 기존 문서도 삭제.
RETENTION_DAYS = 30

URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"

MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _to_iso(date_str):
    """'31 Aug 2026' -> '2026-08-31' (실패 시 None)"""
    m = re.match(r'(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})', date_str.strip())
    if not m:
        return None
    d, mon, y = m.group(1), m.group(2).title(), m.group(3)
    if mon not in MONTHS:
        return None
    return f"{y}-{MONTHS[mon]}-{int(d):02d}"


def _parse_flow(s):
    """'216.7' -> 216.7, '(236.5)' -> -236.5, '1,373.8' -> 1373.8 (빈값/'-' -> None)"""
    s = s.strip().replace(",", "")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def fetch_btc_etf_flows():
    """farside BTC ETF 표에서 [(iso_date, total_raw, netFlow)] 리스트 반환(일자행만)."""
    print("🌐 1. farside.co.uk BTC ETF 페이지 요청 중...")
    r = requests.get(URL, impersonate="chrome", timeout=25)
    if r.status_code != 200:
        raise Exception(f"페이지 접근 실패 (상태 코드: {r.status_code})")
    print(f"📡 HTTP 응답 성공 (코드: {r.status_code}) | 데이터 길이: {len(r.text)} bytes")

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="etf")
    if table is None:
        raise Exception("table.etf 를 찾지 못했습니다. (페이지 구조 변경 가능성)")

    body = table.find("tbody") or table
    results = []
    for tr in body.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        iso = _to_iso(cells[0])          # 맨 왼쪽 = 일자
        if not iso:                      # Average/Maximum/Minimum 등 요약행 제외
            continue
        total_raw = cells[-1]            # 맨 오른쪽 = Total(일별 순매수)
        net = _parse_flow(total_raw)
        results.append((iso, total_raw, net))
    if not results:
        raise Exception("일자별 데이터가 비어 있습니다. (파싱 실패)")
    return results


def run_btc_etf_flow_crawler():
    kst_now = datetime.utcnow() + timedelta(hours=9)
    cutoff = (kst_now.date() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")

    print("\n" + "=" * 60)
    print(f"[{kst_now.strftime('%Y-%m-%d %H:%M:%S')} KST] 🚀 비트코인 ETF 순매수 수집 크롤러 가동")
    print(f"🎯 컬렉션: btc_etf_flows | 최근 {RETENTION_DAYS}일치 유지(기준: {cutoff} 이후)")
    print("=" * 60)

    added = updated = skipped = 0
    pruned = 0

    try:
        rows = fetch_btc_etf_flows()
        print(f"🔎 표에서 일자행 {len(rows)}건 파싱 완료\n")

        print("🔥 파이어베이스 Firestore 동기화")
        print("-" * 60)
        if not flows_ref:
            for iso, raw, net in rows:
                print(f"📝 [드라이런] {iso} | Total={raw} → {net}")
        else:
            # 기존 문서 1회 읽어 비교(변경분만 기록) + prune 에 재사용
            existing = {doc.id: doc.to_dict() for doc in flows_ref.stream()}
            for iso, raw, net in rows:
                if iso < cutoff:
                    continue  # 30일보다 오래된 데이터는 저장하지 않음
                prev = existing.get(iso)
                if prev and prev.get("netFlow") == net and prev.get("rawTotal") == raw:
                    skipped += 1
                    continue
                flows_ref.document(iso).set({
                    "date": iso,
                    "netFlow": net,          # 일별 순매수(US$ 백만, 음수=순유출)
                    "rawTotal": raw,         # 원본 문자열(예: "(236.5)")
                    "unit": "USD_million",
                    "source": "farside.co.uk/bitcoin-etf-flow-all-data",
                    "crawlTimestamp": firestore.SERVER_TIMESTAMP,
                })
                if prev:
                    updated += 1
                    print(f"🔄  [갱신] {iso} | {raw} → {net}")
                else:
                    added += 1
                    print(f"✅  [신규] {iso} | {raw} → {net}")

            # 보관기간(30일) 초과 문서 삭제
            for did in existing:
                if did < cutoff:
                    flows_ref.document(did).delete()
                    pruned += 1
            if pruned:
                print(f"🧹 보관기간({RETENTION_DAYS}일) 초과 {pruned}건 삭제 (기준: {cutoff} 이전)")
        print("-" * 60)

        if logs_ref:
            logs_ref.add({
                "timestamp": firestore.SERVER_TIMESTAMP,
                "status": "SUCCESS",
                "task_name": TASK_NAME,
                "added_count": added,
                "updated_count": updated,
                "skipped_count": skipped,
                "message": (f"동기화 종료 - 신규 {added}건 / 갱신 {updated}건 / "
                            f"스킵 {skipped}건 / 만료삭제 {pruned}건")
            })

        print("\n" + "=" * 60)
        print(f"🏁 파이프라인 종료. 신규 {added} / 갱신 {updated} / 스킵 {skipped} / 삭제 {pruned}")
        print("=" * 60 + "\n")

    except Exception as e:
        error_msg = str(e)
        print(f"\n❌ [에러 발생 및 중단] : {error_msg}")
        if logs_ref:
            logs_ref.add({
                "timestamp": firestore.SERVER_TIMESTAMP,
                "status": "FAILED",
                "task_name": TASK_NAME,
                "added_count": added,
                "updated_count": updated,
                "skipped_count": skipped,
                "message": f"비트코인 ETF 순매수 크롤러 실패 에러 로그: {error_msg}"
            })


if __name__ == "__main__":
    # 실행 시 이 배치 자신의 다음 실행 예정시간만 Firestore(crawler_schedules)에 기록
    update_my_schedule(db, __file__, display_name=TASK_NAME)

    # cron('0 19 * * 1-5') = 19:00 UTC 월~금 → +9h → 04:00 KST 화~토. 별도 가드 불필요.
    run_btc_etf_flow_crawler()
