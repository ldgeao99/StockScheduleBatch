"""연도/시장별 증시 휴장 파일 저장 및 조회 (수집된 일정만 포함)."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "market_holidays"
MARKETS = {"KR": "한국", "US": "미국"}
SOURCE = "https://kr.investing.com/holiday-calendar/"


def calendar_path(market, year, directory=DATA_DIR):
    if market not in MARKETS:
        raise ValueError("market은 KR 또는 US여야 합니다.")
    return Path(directory) / str(int(year)) / f"{market}.json"


def load_calendar(market, year, directory=DATA_DIR):
    """파일이 없으면 FileNotFoundError. 없는 자료를 영업일로 간주하지 않는다."""
    path = calendar_path(market, year, directory)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["market"] != market or data["year"] != int(year):
        raise ValueError(f"휴장 파일의 시장/연도가 일치하지 않습니다: {path}")
    return data


def has_collected_holiday(market, day, directory=DATA_DIR):
    """수집된 휴장 일정 존재 여부. False는 개장일을 보장하지 않는다.

    day는 해당 시장 현지 날짜(YYYY-MM-DD 또는 date).
    """
    day = date.fromisoformat(day) if isinstance(day, str) else day
    data = load_calendar(market, day.year, directory)
    return any(item["date"] == day.isoformat() for item in data["holidays"])


def save_calendars(daily_groups, directory=DATA_DIR):
    """기존 연도 자료에 누적 병합. 이번 응답에 없는 과거 일정은 유지."""
    incoming = {}
    years = set()
    for day, items in daily_groups.items():
        parsed = date.fromisoformat(day)
        years.add(parsed.year)
        for market, country in MARKETS.items():
            for item in items:
                if item["country"] == country:
                    entry = {"date": day, "reason": item["reason"],
                             "exchange": item["market"]}
                    incoming.setdefault((market, parsed.year), []).append(entry)
    if not years:
        raise ValueError("수집된 휴장 일정이 없어 파일을 갱신하지 않습니다.")
    paths = []
    for year in sorted(years):
        for market, country in MARKETS.items():
            path = calendar_path(market, year, directory)
            old = load_calendar(market, year, directory) if path.exists() else None
            entries = (old["holidays"] if old else []) + incoming.get((market, year), [])
            unique = {(entry["date"], entry["exchange"], entry["reason"]): entry
                      for entry in entries}
            holidays = [unique[key] for key in sorted(unique)]
            # 변화가 없으면 타임스탬프도 유지하여 불필요한 커밋 방지.
            if old and old["holidays"] == holidays:
                paths.append(path)
                continue
            data = {"schema_version": 1, "market": market, "country": country,
                    "year": year, "source": SOURCE,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "complete_year": False, "holidays": holidays}
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(path)
            paths.append(path)
    return paths
