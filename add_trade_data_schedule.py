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

# 매월 (일자, 일정명, 세부내용). 관세청 수출입데이터 발표는 매월 1·11·21일.
RELEASES = [
    (1, "수출입데이터 발표(전월 전체 잠정치)",
     "관세청 오전 9시 발표, 전월 21일~말일 포함 전월 전체 잠정치"),
    (11, "수출입데이터 발표(당월 1~10일 잠정치)",
     "관세청 오전 9시 발표, 당월 1~10일 잠정치"),
    (21, "수출입데이터 발표(당월 1~20일 잠정치)",
     "관세청 오전 9시 발표, 당월 1~20일 잠정치"),
]


def build_target_months(base_date: datetime):
    """이번달·다음달(총 2개월)을 (연, 월) 리스트로 반환."""
    this_year, this_month = base_date.year, base_date.month
    if this_month == 12:
        next_year, next_month = this_year + 1, 1
    else:
        next_year, next_month = this_year, this_month + 1
    return [(this_year, this_month), (next_year, next_month)]


def run_trade_data_crawler():
    now = datetime.now()

    print("\n" + "=" * 60)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🚀 수출입데이터 발표 일정 생성기 가동")
    print("=" * 60)

    success_count = 0
    update_count = 0
    skip_count = 0

    try:
        target_months = build_target_months(now)

        for year, month in target_months:
            for day, event_name, detail in RELEASES:
                db_date_str = f"{year}-{month:02d}-{day:02d}"
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
                    # 세부내용이 최신이고 검증 표시(isVerified)까지 되어 있으면 스킵, 아니면 갱신
                    if (existing_data.get("detail") == detail
                            and existing_data.get("isVerified") is True):
                        print(f"⏭️  [중복 스킵] 날짜: {db_date_str} | 이미 존재합니다.")
                        skip_count += 1
                    else:
                        doc.reference.update({"detail": detail, "isVerified": True, "url": ""})
                        update_count += 1
                        print(f"🔄  [정보 업데이트] 날짜: {db_date_str} | 세부내용/검증표시를 갱신했습니다.")
                else:
                    payload = {
                        "date": db_date_str,
                        "category": CATEGORY_NAME,
                        "eventName": event_name,
                        "detail": detail,
                        "relatedStocks": "",
                        "url": "",
                        "isVerified": True
                    }
                    events_ref.add(payload)
                    success_count += 1
                    print(f"✅  [신규 삽입] 날짜: {db_date_str} | 일정을 신규 등록했습니다.")

        log_payload = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "SUCCESS",
            "task_name": "[add_trade_data_schedule] 수출입데이터 발표 일정 생성",
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
            "task_name": "[add_trade_data_schedule] 수출입데이터 발표 일정 생성",
            "added_count": success_count,
            "updated_count": update_count,
            "skipped_count": skip_count,
            "message": f"수출입데이터 발표 일정 생성 실패 에러 로그: {error_msg}"
        })


if __name__ == "__main__":
    # 실행 시 이 배치 자신의 다음 실행 예정시간만 Firestore(crawler_schedules)에 기록
    update_my_schedule(db, __file__, display_name="수출입데이터 발표 일정 생성")

    # cron('0 0 1 * *')이 UTC·KST 모두 1일이라 별도 날짜 가드 없이 바로 실행
    run_trade_data_crawler()
