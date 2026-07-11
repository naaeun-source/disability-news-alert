import os
import re
import json
import time
import requests
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SENT_FILE = "sent_urls.json"

# 최근 몇 시간 이내 기사만 발송할지 (오래된 기사 차단용)
MAX_AGE_HOURS = 48
# 텔레그램 한 메시지 최대 길이(4096자) 대비 안전 한도
MSG_LIMIT = 3800
# 제목 유사도 기준(0~1). 높을수록 '거의 똑같아야' 같은 묶음으로 봄
SIM_THRESHOLD = 0.70
# 실행 간 중복 판별용으로 보관할 최근 제목 지문 개수
SIG_KEEP = 1000

# ── 감시 키워드 ──────────────────────────────────────────────
# label : 텔레그램에 표시될 이름
# query : 네이버 검색어. "+단어"=반드시 포함(AND). 네이버는 본문까지 색인하므로
#         +로 두 단어를 강제하면 '제목 또는 본문'에 두 단어가 있는 기사가 검색됨
# title_words : 제목 전용 모드일 때 제목에 있어야 하는 단어(선택)
KEYWORDS = [
    {"label": "전장연", "query": "전장연"},
    {"label": "탈시설", "query": "탈시설"},
    {"label": "장애인+서울시", "query": "+장애인 +서울시", "title_words": ["장애인", "서울시"]},
]
# 제목에만 단어가 든 기사로 좁히려면 True (정확도 우선, 본문전용 기사 누락 가능)
USE_TITLE_ONLY = False
# ────────────────────────────────────────────────────────────


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&quot;", '"').replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return text.strip()


def norm_title(t):
    # 대괄호·괄호·기호·공백 제거하여 제목 지문 생성
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"【[^】]*】", "", t)
    t = re.sub(r"<[^>]*>", "", t)
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
    return t.lower()


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


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
            data = json.load(f)
        return data.get("urls", []), data.get("sigs", [])
    return [], []


def save_sent(urls, sigs):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"urls": urls[-3000:], "sigs": sigs[-SIG_KEEP:]},
                  f, ensure_ascii=False, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "disable_web_page_preview": True}
    requests.post(url, data=payload, timeout=15)


def send_grouped(clusters_by_label, cluster_count):
    parts = [f"🔔 새 기사 {cluster_count}건"]
    for label, clusters in clusters_by_label.items():
        parts.append(f"\n[{label}]")
        for i, c in enumerate(clusters, 1):
            extra = f" (외 {len(c['members']) - 1}개 매체)" if len(c["members"]) > 1 else ""
            parts.append(f"{i}. {c['rep_title']}{extra}\n{c['rep_link']}")

    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > MSG_LIMIT and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p) if cur else p
    if cur:
        chunks.append(cur)

    for c in chunks:
        send_telegram(c)
        time.sleep(1)


def main():
    first_run = not os.path.exists(SENT_FILE)
    urls, sigs = load_sent()
    seen_urls = set(urls)
    order = list(urls)
    recent_sigs = list(sigs)

    # 라벨별로 이번 실행 신규 기사를 유사도로 묶음
    clusters_by_label = {}

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

            if USE_TITLE_ONLY and kw.get("title_words"):
                if not all(w in title for w in kw["title_words"]):
                    continue
            if link in seen_urls:
                continue

            seen_urls.add(link)
            order.append(link)

            if first_run:
                # 첫 실행: 발송 안 함. 지문만 축적해 이후 중복 판별에 사용
                recent_sigs.append(norm_title(title))
                continue
            if not is_recent(item.get("pubDate", "")):
                continue

            sig = norm_title(title)
            # 지난 실행들에서 이미 보낸 기사와 거의 동일하면 발송 제외
            if any(similar(sig, s) >= SIM_THRESHOLD for s in recent_sigs):
                continue

            # 이번 실행 안에서 유사 기사끼리 묶기
            clusters = clusters_by_label.setdefault(kw["label"], [])
            placed = False
            for c in clusters:
                if similar(sig, c["sig"]) >= SIM_THRESHOLD:
                    c["members"].append((title, link))
                    placed = True
                    break
            if not placed:
                clusters.append({"rep_title": title, "rep_link": link,
                                 "sig": sig, "members": [(title, link)]})

    if first_run:
        save_sent(order, recent_sigs)
        print(f"첫 실행: {len(order)}건 기록 완료 (발송 안 함)")
        return

    # 발송 확정된 묶음의 대표 지문을 최근 지문에 추가
    cluster_count = 0
    for label, clusters in clusters_by_label.items():
        for c in clusters:
            recent_sigs.append(c["sig"])
            cluster_count += 1

    save_sent(order, recent_sigs)

    if cluster_count == 0:
        print("신규 발송: 0건")
        return

    send_grouped(clusters_by_label, cluster_count)
    print(f"신규 발송: {cluster_count}개 묶음")


if __name__ == "__main__":
    main()
