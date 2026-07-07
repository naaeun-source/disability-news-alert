import os
import re
import json
import time
import requests

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SENT_FILE = "sent_urls.json"

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


def search_naver(query, display=30):
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

            if not all(w in combined for w in kw["must_include"]):
                continue
            if link in sent_urls:
                continue

            sent_urls.add(link)
            order.append(link)

            if not first_run:
                message = f"🔔 [{kw['label']}]\n\n{title}\n\n{link}"
                try:
                    send_telegram(message)
                    new_count += 1
                    time.sleep(1)
                except Exception as e:
                    print(f"발송 오류: {e}")

    order = order[-2000:]
    save_sent(order)

    if first_run:
        print(f"첫 실행: {len(order)}건 기록 완료 (발송 안 함)")
    else:
        print(f"신규 발송: {new_count}건")


if __name__ == "__main__":
    main()
