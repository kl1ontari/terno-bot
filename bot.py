#!/usr/bin/env python3
"""
Моніторинг оренди квартир у Тернополі (OLX, DOM.RIA, LUN) -> Telegram.

Запуск: python bot.py          (робочий режим)
        python bot.py --dry    (тест: нічого не надсилає і не зберігає)
"""
import json
import os
import random
import re
import sys
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ===================== НАЛАШТУВАННЯ =====================
SEARCH_URLS = {
    "OLX": "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/ternopol/?currency=USD&search[order]=created_at:desc&search[filter_float_price:to]=500&search[filter_enum_number_of_rooms_string][0]=odnokomnatnye&search[filter_enum_number_of_rooms_string][1]=dvuhkomnatnye",
    "DOM.RIA": "https://dom.ria.com/uk/arenda-kvartir/ternopol/",
    "LUN": "https://lun.ua/rent/ternopil/flats",
}
MAX_PRICE = 450                # $ включно
MAX_PRICE_NO_COMMISSION = 500  # $ включно, якщо без комісії
MAX_NEW_PER_RUN = 40           # ліміт нових оголошень за один запуск
STATE_FILE = "seen.json"

# Як відрізнити посилання на оголошення від решти посилань на сторінці
LINK_PATTERNS = {
    "OLX": r"/d/(uk/)?obyavlenie/",
    "DOM.RIA": r"/realty-[^?#]*\d+\.html",
    "LUN": r"/realty/\d+",
}
LUN_HOSTS = ("lun.ua", "www.lun.ua", "apps.lun.ua")
# ========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}

NO_COMM_RE = re.compile(
    r"без\s+комісі|без\s+комиссі|без\s+посередник|від\s+власника|от\s+владельца"
    r"|комісі[яї]\s*[:\-]?\s*(?:0|нема|відсутня)|0\s*%\s*комісі",
    re.I,
)
BAD_REPAIR_RE = re.compile(r"без\s+ремонту|потребує\s+(?:косметичного\s+)?ремонту|під\s+ремонт|аварійн", re.I)
GOOD_REPAIR_RE = re.compile(r"ремонт|відремонтован|євро", re.I)
USD_RE1 = re.compile(r"(\d[\d\s\u00a0]*)\s*(?:\$|USD|дол)", re.I)
USD_RE2 = re.compile(r"\$\s*(\d[\d\s\u00a0]*)")
UAH_RE = re.compile(r"(\d[\d\s\u00a0]*)\s*(?:грн|₴|UAH)", re.I)
CITY_RE = re.compile(r"тернопіл|тернопол", re.I)
LUN_ID_RE = re.compile(r"(?:apps\.lun\.ua/realty/|/realty/)(\d{6,})", re.I)


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def usd_rate():
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
        return float(requests.get(url, timeout=15).json()[0]["rate"])
    except Exception:
        return float(os.getenv("USD_RATE", "41.5"))


def collect_links(site, url):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(url).netloc
    pat = re.compile(LINK_PATTERNS[site], re.I)
    links = []

    for a in soup.find_all("a", href=True):
        full = urljoin(url, unquote(a["href"])).split("#")[0].split("?")[0]
        p = urlparse(full)
        allowed = p.netloc in LUN_HOSTS if site == "LUN" else p.netloc == host
        if allowed and pat.search(unquote(p.path)):
            if site == "LUN":
                m = LUN_ID_RE.search(full)
                if m:
                    full = "https://apps.lun.ua/realty/" + m.group(1)
            links.append(full)

    # LUN часто тримає посилання на оголошення всередині JSON/JS сторінки
    if site == "LUN":
        norm = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
        for lid in LUN_ID_RE.findall(norm):
            links.append("https://apps.lun.ua/realty/" + lid)

    return list(dict.fromkeys(links))


def meta(soup, *names):
    for n in names:
        t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if t and t.get("content"):
            return t["content"]
    return ""


def num(s):
    d = re.sub(r"\D", "", s)
    return float(d) if d else None


def find_price(sources, rate):
    for s in sources:
        for rx in (USD_RE1, USD_RE2):
            for m in rx.finditer(s):
                v = num(m.group(1))
                if v and 100 <= v <= 5000:
                    return v
        for m in UAH_RE.finditer(s):
            v = num(m.group(1))
            if v and 3000 <= v <= 100000:
                return round(v / rate)
    return None


def find_rooms(title):
    t = title.lower()
    for w, n in (("одно", 1), ("дво", 2), ("три", 3), ("чотири", 4)):
        if w + "кімн" in t:
            return n
    m = re.search(r"(\d)\s*[-–]?\s*(?:кімн|комн)", t)
    return int(m.group(1)) if m else None


def parse(url, rate):
    soup = BeautifulSoup(fetch(url), "lxml")
    title = meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else url)
    desc = meta(soup, "og:description", "description")
    parts = []
    for sel in ('[data-cy="ad_description"]', "#descriptionBlock", '[data-testid="ad-price-container"]'):
        el = soup.select_one(sel)
        if el:
            parts.append(el.get_text(" ", strip=True))
    body = soup.get_text(" ", strip=True)
    text = " ".join([title, desc] + parts)
    if len(text) < 120:  # метаданих замало, беремо початок сторінки
        text += " " + body[:4000]
    return {
        "url": url,
        "title": title.strip(),
        "photo": urljoin(url, meta(soup, "og:image", "twitter:image")) if meta(soup, "og:image", "twitter:image") else "",
        "price": find_price([title, desc] + parts + [body[:3000]], rate),
        "rooms": find_rooms(title + " " + desc),
        "no_comm": bool(NO_COMM_RE.search(text)),
        "bad_repair": bool(BAD_REPAIR_RE.search(text)),
        "repair": bool(GOOD_REPAIR_RE.search(text)) and not BAD_REPAIR_RE.search(text),
        "in_city": bool(CITY_RE.search(title + " " + body)),
    }


def evaluate(it):
    if not it["in_city"]:
        return False, "не Тернопіль"
    if it["rooms"] and it["rooms"] > 2:
        return False, f"{it['rooms']} кімн."
    if it["bad_repair"]:
        return False, "без ремонту"
    if it["price"] is not None:
        limit = MAX_PRICE_NO_COMMISSION if it["no_comm"] else MAX_PRICE
        if it["price"] > limit:
            return False, f"${it['price']:.0f} > ${limit}"
    return True, "ок"


def message(site, it):
    head = "🔥 БЕЗ КОМІСІЇ" if it["no_comm"] else "🏠 Нова квартира"
    price = f"${it['price']:.0f}" if it["price"] is not None else "ціну не визначено"
    rooms = f" · {it['rooms']}-кімн." if it["rooms"] else ""
    repair = "ремонт: є" if it["repair"] else "ремонт: не вказано"
    return f"{head} · {site}\n{price}{rooms} · {repair}\n{it['title']}\n{it['url']}"


def send_photo(photo, caption):
    """Надсилає фото з підписом. Якщо фото не вдалося — звичайне повідомлення."""
    token, chat = os.environ["TELEGRAM_TOKEN"].strip(), os.environ["TELEGRAM_CHAT_ID"].strip()
    if photo:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                json={"chat_id": chat, "photo": photo, "caption": caption[:1024]},
                timeout=30,
            )
            if r.ok:
                return
            print(f"  фото не надіслалось ({r.status_code}), шлю текстом")
        except Exception as e:
            print(f"  фото не надіслалось ({e}), шлю текстом")
    send(caption)


def send(text):
    token, chat = os.environ["TELEGRAM_TOKEN"].strip(), os.environ["TELEGRAM_CHAT_ID"].strip()
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("seen", {})
        s.setdefault("fails", {})
        s.setdefault("sites", [])
        return s
    return {"seen": {}, "fails": {}, "sites": []}


def save_state(state):
    newest = sorted(state["seen"].items(), key=lambda kv: kv[1], reverse=True)[:3000]
    state["seen"] = dict(newest)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    dry = "--dry" in sys.argv
    first = not os.path.exists(STATE_FILE) and not dry
    state = load_state()
    rate = usd_rate()
    now = int(time.time())
    queue, fetched = [], 0

    for site, url in SEARCH_URLS.items():
        if not url.startswith("http"):
            print(f"{site}: пропущено (не вказано посилання)")
            continue
        try:
            links = collect_links(site, url)
            state["fails"][site] = 0
        except Exception as e:
            n = state["fails"].get(site, 0) + 1
            state["fails"][site] = n
            print(f"{site}: помилка {e}")
            if n == 3 and not dry:
                send(f"⚠️ {site}: 3 невдалі перевірки поспіль ({e}). Сайт міг заблокувати бота або змінити верстку.")
            continue

        print(f"{site}: знайдено посилань на оголошення: {len(links)}")
        # Сайт, який вперше дав результати, просто запам'ятовуємо без розсилки
        site_first = first or (links and site not in state["sites"])
        if links and site not in state["sites"]:
            state["sites"].append(site)

        for link in links:
            if link in state["seen"]:
                continue
            if site_first:
                state["seen"][link] = now
                continue
            if fetched >= MAX_NEW_PER_RUN:
                break
            fetched += 1
            time.sleep(random.uniform(1, 2.5))
            try:
                item = parse(link, rate)
            except Exception as e:
                print(f"  не вдалось відкрити {link}: {e}")
                continue  # не позначаємо як побачене, спробуємо наступного разу
            state["seen"][link] = now
            ok, reason = evaluate(item)
            print(f"  {'+' if ok else '-'} {reason}: {item['title'][:60]}")
            if ok:
                queue.append((site, item))

    if dry:
        print(f"\nПройшли б фільтр: {len(queue)}")
        return

    if first:
        send(f"✅ Бот запущено. Поточні оголошення ({len(state['seen'])}) запам'ятав, надсилатиму тільки нові.")
    queue.sort(key=lambda x: not x[1]["no_comm"])  # без комісії першими
    for site, item in queue:
        send_photo(item.get("photo"), message(site, item))
        time.sleep(1)
    save_state(state)


if __name__ == "__main__":
    main()