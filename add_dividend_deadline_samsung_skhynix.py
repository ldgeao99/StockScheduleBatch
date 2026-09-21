from datetime import datetime, timedelta
import os
import sys

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from crawler_schedule import update_my_schedule

# 정수 변환 제한 확장
sys.set_int_max_str_digits(10000)

# 파이어베이스 엔진 초기화
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"
db = firestore.Client()
events_ref = db.collection("events")
logs_ref = db.collection("crawler_logs")

CATEGORY_NAME = "일반"

SAMSUNG_DETAIL = (
    "배당 기준일로부터 '이틀 전' 거래일이 배당을 받기위한 마지막 매수일이 됨.\n\n"
    "삼성전자의 경우 '삼성전자우'가  삼성전자보다 강세를 보일 가능성이 큼.\n\n"
    "삼성전자의 분기 배당기준일은 다음과 같음\n"
    "1분기 = 매년 3월 31일 \n"
    "2분기 = 매년 6월 30일 \n"
    "3분기 = 매년 9월 30일 \n"
    "4분기  = 매년 12월 31일 \n"
    "\n"
    "* 배당 기준일이 공휴일인 경우 하루 앞당김."
)

SKHYNIX_DETAIL = (
    "SK하이닉스의 분기 배당기준일은 다음과 같음\n"
    "2월말, 5월말, 8월말, 11월말\n\n"
    "배당 기준일로부터 이틀 전 거래일이 배당을 받기위한 마지막 매수일이 됨.\n"
    "배당 기준일이 공휴일인 경우 하루 앞당김.\n\n"
    "* SK하이닉스는 배당기준일을 매번 회사가 별도로 정하므로 공시 확인 필요."
)

# 월(1~12) -> (회사명, 세부내용). 해당 월의 1일에 일정 생성.
#  삼성전자: 3/6/9/12월(분기말 배당기준일) / SK하이닉스: 2/5/8/11월(2·5·8·11월말 배당기준일)
COMPANY_BY_MONTH = {
    3: ("삼성전자", SAMSUNG_DETAIL),
    6: ("삼성전자", SAMSUNG_DETAIL),
    9: ("삼성전자", SAMSUNG_DETAIL),
    12: ("삼성전자", SAMSUNG_DETAIL),
    2: ("SK하이닉스", SKHYNIX_DETAIL),
    5: ("SK하이닉스", SKHYNIX_DETAIL),
    8: ("SK하이닉스", SKHYNIX_DETAIL),
    11: ("SK하이닉스", SKHYNIX_DETAIL),
}


def build_target_months(base_date: datetime):
    """이번달·다음달·다다음달(총 3개월)을 (연, 월) 리스트로 반환."""
    months = []
    y, mth = base_date.year, base_date.month
    for _ in range(3):
        months.append((y, mth))
        mth += 1
        if mth == 13:
            mth, y = 1, y + 1
    return months


def run_dividend_deadline_crawler():
    now = datetime.now()

    print("\n" + "=" * 60)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🚀 삼성전자·SK하이닉스 배당 마지막 매수일 일정 생성기 가동")
    print("=" * 60)

    success_count = 0
    update_count = 0
    skip_count = 0

    try:
        target_months = build_target_months(now)

        for year, month in target_months:
            if month not in COMPANY_BY_MONTH:
                print(f"⏭️  {year}-{month:02d} : 해당 월엔 배당 대상 없음")
                continue

            company, detail = COMPANY_BY_MONTH[month]
            # 일자 = 해당 월 1일. 실제 마감일은 미정이라 제목에 '[N월 미정]' 표기.
            db_date_str = f"{year}-{month:02d}-01"
            event_name = f"[{month}월 미정] {company} 분기 배당을 위한 마지막 매수일"

            print(f"📅 대상 일정: {db_date_str} | {event_name}")

            existing_docs = events_ref.where(
                filter=FieldFilter("date", "==", db_date_str)
            ).where(
                filter=FieldFilter("category", "==", CATEGORY_NAME)
            ).where(
                filter=FieldFilter("eventName", "==", event_name)
            ).get()

            if len(existing_docs) > 0:
                doc = existing_docs[0]
                existing_data = doc.to_dict()
                # 세부내용이 최신이고 중요표시(isImportant)까지 되어 있으면 스킵, 아니면 갱신
                if existing_data.get("detail") == detail and existing_data.get("isImportant") is True:
                    print(f"⏭️  [중복 스킵] 날짜: {db_date_str} | 이미 존재합니다.")
                    skip_count += 1
                else:
                    doc.reference.update({"detail": detail, "isImportant": True, "url": ""})
                    update_count += 1
                    print(f"🔄  [정보 업데이트] 날짜: {db_date_str} | 세부내용/중요표시를 갱신했습니다.")
            else:
                payload = {
                    "date": db_date_str,
                    "category": CATEGORY_NAME,
                    "eventName": event_name,
                    "detail": detail,
                    "relatedStocks": "",
                    "url": "",
                    "isImportant": True
                }
                events_ref.add(payload)
                success_count += 1
                print(f"✅  [신규 삽입] 날짜: {db_date_str} | 일정을 신규 등록했습니다.")

        log_payload = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "SUCCESS",
            "task_name": "[add_dividend_deadline_samsung_skhynix] 삼성전자·SK하이닉스 배당 마지막 매수일 생성",
            "added_count": success_count,
            "updated_count": update_count,
            "skipped_count": skip_count,
            "message": f"동기화 종료 - 신규 삽입: {success_count}건, 정보 업데이트: {update_count}건, 중복 스킵: {skip_count}건"
        }
        logs_ref.add(log_payload)

        print("\n" + "=" * 60)
        print(f"🏁 파이프라인 연동 완수! [신규]: {success_count}건 | [업데이트]: {update_count}건 | [스킵]: {skip_count}건")
        print("=" * 60 + "\n")

    except Exception as e:
        error_msg = str(e)
        print(f"\n❌ [에러 발생 및 중단] : {error_msg}")
        logs_ref.add({
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "FAILED",
            "task_name": "[add_dividend_deadline_samsung_skhynix] 삼성전자·SK하이닉스 배당 마지막 매수일 생성",
            "added_count": success_count,
            "updated_count": update_count,
            "skipped_count": skip_count,
            "message": f"삼성전자·SK하이닉스 배당 마지막 매수일 생성 실패 에러 로그: {error_msg}"
        })


if __name__ == "__main__":
    # 실행 시 이 배치 자신의 다음 실행 예정시간만 Firestore(crawler_schedules)에 기록
    update_my_schedule(db, __file__, display_name="삼성전자·SK하이닉스 배당 마지막 매수일 생성")

    # cron('0 0 1 * *')이 UTC·KST 모두 1일이라 별도 날짜 가드 없이 바로 실행
    run_dividend_deadline_crawler()
