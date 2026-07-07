import os
import re
import json
import time
import requests
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SENT_FILE = "sent_urls.json"

# 최근 몇 시간 이내 기사만 발송할지 (오래된 기사 차단용)
MAX_AGE_HOURS = 48

# 감시 키워드
# label : 텔레그램에 표시될 이름
# query : 네이버에 검색할 단어
# must_include : 제목+요약에 반드시 모두 포함돼야 하는 단어 목록(AND 조건)
KEYWORDS = [
    {"label": "전장연", "query": "전장연", "must_include": ["전장연"]},
    {"label": "탈시설", "query": "탈시설", "must_include": ["탈시설"]},
    {"label": "장애인+서울시", "query": "장애인 서울시", "must_include": ["장애인", "서울시"]},
]


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&quot;", '"').replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return text.strip()


def is_recent(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        return (datetime.now(timezone.utc) - dt) <= timedelta(hours=MAX_AGE_HOURS)
    except Exception:
        return True


def search_naver(query, display=100):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display, "sort": "date"}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("items", [])


def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"urls": []}


def save_sent(urls):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, data=payload, timeout=15)


def main():
    first_run = not os.path.exists(SENT_FILE)
    sent = load_sent()
    sent_urls = set(sent.get("urls", []))
    order = list(sent.get("urls", []))
    new_count = 0

    for kw in KEYWORDS:
        try:
            items = search_naver(kw["query"])
        except Exception as e:
            print(f"검색 오류 ({kw['label']}): {e}")
            continue

        for item in items:
            link = item.get("originallink") or item.get("link")
            if not link:
                continue
            title = clean_html(item.get("title", ""))
            desc = clean_html(item.get("description", ""))
            combined = title + " " + desc

            # AND 조건 확인
            if not all(w in combined for w in kw["must_include"]):
                continue
            # 이미 본 기사면 건너뜀
            if link in sent_urls:
                continue

            # 신규 기사 → 일단 '본 기사'로 기록
            sent_urls.add(link)
            order.append(link)

            # 첫 실행이면 발송하지 않음(과거 기사 폭탄 방지)
            if first_run:
                continue
            # 오래된 기사면 기록만 하고 발송하지 않음
            if not is_recent(item.get("pubDate", "")):
                continue

            # 최근 신규 기사만 발송
            message = f"🔔 [{kw['label']}]\n\n{title}\n\n{link}"
            try:
                send_telegram(message)
                new_count += 1
                time.sleep(1)
            except Exception as e:
                print(f"발송 오류: {e}")

    order = order[-3000:]
    save_sent(order)

    if first_run:
        print(f"첫 실행: {len(order)}건 기록 완료 (발송 안 함)")
    else:
        print(f"신규 발송: {new_count}건")


if __name__ == "__main__":
    main()
