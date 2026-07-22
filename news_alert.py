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
MAX_AGE_HOURS = 48
MSG_LIMIT = 3800
SIM_THRESHOLD = 0.70
SIG_KEEP = 1000

KEYWORDS = [
    {"label": "전장연", "query": "전장연"},
    {"label": "탈시설", "query": "탈시설"},
    {"label": "장애인+서울시", "query": "+장애인 +서울시", "title_words": ["장애인", "서울시"]},
]
USE_TITLE_ONLY = False


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&quot;", '"').replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return text.strip()


def norm_title(t):
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
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID,
               "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "sort": "date"}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("items", [])


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
    """성공하면 True, 실패하면 False 반환하고 이유를 로그에 출력"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=15)
        body = r.json()
    except Exception as e:
        print(f"  [발송 실패] 네트워크 오류: {e}")
        return False
    if r.ok and body.get("ok"):
        return True
    # 텔레그램이 알려주는 실패 이유를 그대로 출력
    print(f"  [발송 실패] code={r.status_code} "
          f"reason={body.get('description', '알 수 없음')}")
    return False


def send_grouped(clusters, cluster_count):
    """묶음 전체를 메시지로 만들어 발송. 모든 조각이 성공해야 True"""
    parts = [f"🔔 새 기사 {cluster_count}건"]
    for label, group in clusters.items():
        parts.append(f"\n[{label}]")
        for i, c in enumerate(group, 1):
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

    all_ok = True
    for c in chunks:
        if not send_telegram(c):
            all_ok = False
        time.sleep(1)
    return all_ok


def main():
    first_run = not os.path.exists(SENT_FILE)
    urls, sigs = load_sent()
    seen = set(urls)
    order = list(urls)
    recent_sigs = list(sigs)
    run_seen = set()

    clusters = {}  # label -> [{rep_title, rep_link, sig, members:[(title,link)]}]

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
            if link in seen or link in run_seen:
                continue
            run_seen.add(link)

            # 첫 실행: 발송 없이 지문·URL만 기록
            if first_run:
                order.append(link)
                seen.add(link)
                recent_sigs.append(norm_title(title))
                continue
            # 오래된 기사: 발송 안 함, 본 것으로만 기록
            if not is_recent(item.get("pubDate", "")):
                order.append(link)
                seen.add(link)
                continue

            sig = norm_title(title)
            # 지난 실행에서 이미 보낸 기사와 유사 → 발송 안 함, 본 것으로 기록
            if any(similar(sig, s) >= SIM_THRESHOLD for s in recent_sigs):
                order.append(link)
                seen.add(link)
                continue

            # 발송 후보 → 이번 실행 안에서 유사끼리 묶음
            group = clusters.setdefault(kw["label"], [])
            placed = False
            for c in group:
                if similar(sig, c["sig"]) >= SIM_THRESHOLD:
                    c["members"].append((title, link))
                    placed = True
                    break
            if not placed:
                group.append({"rep_title": title, "rep_link": link,
                              "sig": sig, "members": [(title, link)]})

    if first_run:
        save_sent(order, recent_sigs)
        print(f"첫 실행: {len(order)}건 기록 완료 (발송 안 함)")
        return

    cluster_count = sum(len(g) for g in clusters.values())
    if cluster_count == 0:
        save_sent(order, recent_sigs)
        print("신규 발송: 0건")
        return

    ok = send_grouped(clusters, cluster_count)

    if ok:
        # 발송 성공한 경우에만 '본 기사'로 기록 (실패 시 다음 실행에서 재시도)
        for label, group in clusters.items():
            for c in group:
                for _, link in c["members"]:
                    order.append(link)
                    seen.add(link)
                recent_sigs.append(c["sig"])
        save_sent(order, recent_sigs)
        print(f"신규 발송 성공: {cluster_count}묶음")
    else:
        # 실패분은 기록하지 않고 저장만(다음 실행 재시도). 오래된/중복 기록은 유지
        save_sent(order, recent_sigs)
        print(f"발송 실패: {cluster_count}묶음 — 다음 실행에서 재시도됨 (위 실패 사유 확인)")


if __name__ == "__main__":
    main()
