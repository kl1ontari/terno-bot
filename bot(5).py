"""
Моніторинг оренди квартир у Тернополі (OLX, DOM.RIA, LUN) -> Telegram.  Версія 3.

Запуск: python bot.py          (робочий режим)
        python bot.py --dry    (тест: нічого не надсилає і не зберігає)
"""
import hashlib
import html as htmllib
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
MAX_PRICE = 450                # $ включно
MAX_PRICE_NO_COMMISSION = 500  # $ включно, якщо без комісії
MAX_NEW_PER_RUN = 40           # ліміт відкриттів нових оголошень за один запуск
MAX_PHOTOS = 5                 # скільки фото надсилати
STATE_FILE = "seen.json"
SCHEMA = 3                     # змінюється, коли змінюється спосіб збору (бот тихо перезапам'ятовує)

OLX_PAGE = "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/ternopol/"
OLX_FRIENDLY = "https://www.olx.ua/api/v1/friendly-links/query-params/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/ternopol/"
OLX_API = "https://www.olx.ua/api/v1/offers/"

DOMRIA_URLS = [
    "https://dom.ria.com/uk/arenda-kvartir/ternopol/",
    "https://dom.ria.com/uk/arenda-kvartir/ternopol-1k/",
    "https://dom.ria.com/uk/arenda-kvartir/ternopol-2k/",
]
DOMRIA_PAGES = 3               # скільки сторінок видачі переглядати (на кожне посилання)
DOMRIA_LINK_RE = re.compile(r"/realty-[^?#]*\d+\.html")

LUN_URL = "https://lun.ua/rent/ternopil/flats"
LUN_PAGES = 3
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
CITY_RE = re.compile(r"тернопіл|тернопол|ternopil|ternopol", re.I)
PRICE_LINE_RE = re.compile(r"^(\d[\d\s\u00a0]{2,})\s*(грн|\$|USD)$", re.I)
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.I)


# ----------------------------------------------------------------- утиліти
def fetch(url, tries=2):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return r.text
        except requests.HTTPError as e:
            last = e
            if e.response is not None and e.response.status_code in (403, 404):
                break  # повторювати немає сенсу
        except requests.RequestException as e:
            last = e
        time.sleep(1.5)
    raise last


def usd_rate():
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
        return float(requests.get(url, timeout=15).json()[0]["rate"])
    except Exception:
        return float(os.getenv("USD_RATE", "41.5"))


def num(s):
    d = re.sub(r"\D", "", str(s))
    return float(d) if d else None


def text_key(t):
    return hashlib.md5(re.sub(r"\s+", " ", t).encode("utf-8")).hexdigest()[:16]


def clean_text(t):
    t = re.sub(r"<br\s*/?>", " ", t or "", flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def meta(soup, *names):
    for n in names:
        t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if t and t.get("content"):
            return t["content"]
    return ""


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


def find_rooms(text):
    t = (text or "").lower()
    for w, n in (("одно", 1), ("дво", 2), ("три", 3), ("чотири", 4)):
        if w + "кімн" in t:
            return n
    m = re.search(r"(\d)\s*[-–]?\s*(?:кімн|комн)", t)
    return int(m.group(1)) if m else None


def dedupe(seq):
    return list(dict.fromkeys(x for x in seq if x))


def base_item(**kw):
    it = {
        "url": "", "title": "", "desc": "", "photos": [], "price": None, "rooms": None,
        "area": None, "floor": "", "address": "", "no_comm": False, "bad_repair": False,
        "repair": False, "in_city": True, "direct": True,
    }
    it.update(kw)
    return it


def flags_from_text(text):
    bad = bool(BAD_REPAIR_RE.search(text))
    return {
        "no_comm": bool(NO_COMM_RE.search(text)),
        "bad_repair": bad,
        "repair": bool(GOOD_REPAIR_RE.search(text)) and not bad,
    }


# ----------------------------------------------------------------- JSON / фото
def json_blobs(soup):
    for s in soup.find_all("script"):
        t = (s.string or s.get_text() or "").strip()
        if not t:
            continue
        typ = (s.get("type") or "").lower()
        if typ in ("application/ld+json", "application/json") or s.get("id") == "__NEXT_DATA__":
            try:
                yield json.loads(t)
            except Exception:
                continue


def walk(o):
    yield o
    if isinstance(o, dict):
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def norm_img(u, base=""):
    if not isinstance(u, str):
        return ""
    u = u.strip().replace("\\/", "/")
    u = u.replace("{width}x{height}", "1000x750").replace("{width}", "1000").replace("{height}", "750")
    if not u or u.startswith("data:") or ".svg" in u.lower():
        return ""
    return urljoin(base, u) if base else u


def images_from(v, base=""):
    out = []
    if isinstance(v, str):
        out.append(norm_img(v, base))
    elif isinstance(v, dict):
        for k in ("url", "src", "link", "contentUrl", "original", "large"):
            if isinstance(v.get(k), str):
                out.append(norm_img(v[k], base))
                break
    elif isinstance(v, list):
        for x in v:
            out.extend(images_from(x, base))
    return dedupe(out)


def price_from_dict(d):
    """Повертає (сума, валюта|None) зі словника типу schema.org / власних даних."""
    cur = d.get("priceCurrency") or d.get("currency")
    p = d.get("price")
    if isinstance(p, dict):
        cur = p.get("currency") or p.get("priceCurrency") or cur
        p = p.get("amount") or p.get("value") or p.get("price")
    if p is None and isinstance(d.get("offers"), (dict, list)):
        offers = d["offers"] if isinstance(d["offers"], list) else [d["offers"]]
        for o in offers:
            if isinstance(o, dict):
                v = price_from_dict(o)
                if v[0]:
                    return v
    try:
        v = float(str(p).replace(" ", "").replace("\u00a0", "").replace(",", ".")) if p is not None else None
    except ValueError:
        v = None
    if cur:
        cur = {"UAH": "UAH", "грн": "UAH", "USD": "USD", "$": "USD"}.get(str(cur).strip(), str(cur))
    return (v, cur)


def listing_dicts(soup):
    """Шукає в JSON сторінки словники, схожі на оголошення (id/url + ціна/фото)."""
    found = []
    for blob in json_blobs(soup):
        for d in walk(blob):
            if not isinstance(d, dict):
                continue
            url = next((d[k] for k in ("url", "link", "href", "@id") if isinstance(d.get(k), str) and d[k].startswith("http")), "")
            ident = next((str(d[k]) for k in ("id", "realty_id", "realtyId", "listing_id") if re.fullmatch(r"\d{6,18}", str(d.get(k, "")))), "")
            photos = []
            for k in ("image", "images", "photo", "photos", "pictures", "gallery"):
                if k in d:
                    photos += images_from(d[k])
            price, cur = price_from_dict(d)
            if not (url or ident) or not (price or photos):
                continue
            found.append({
                "url": url, "id": ident, "photos": dedupe(photos), "price": price, "cur": cur,
                "rooms": d.get("rooms") or d.get("room_count") or d.get("numberOfRooms"),
                "name": str(d.get("name") or d.get("title") or d.get("address") or ""),
            })
    return found


def ld_price_usd(soup, rate):
    for d in listing_dicts_any(soup):
        v, cur = d
        if v and cur == "USD" and 100 <= v <= 5000:
            return v
        if v and cur == "UAH" and 3000 <= v <= 100000:
            return round(v / rate)
    return None


def listing_dicts_any(soup):
    for blob in json_blobs(soup):
        for d in walk(blob):
            if isinstance(d, dict):
                v, cur = price_from_dict(d)
                if v:
                    yield (v, cur)


# ----------------------------------------------------------------- DOM.RIA
def extract_links(html, base_url, pattern):
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(base_url).netloc
    links = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        p = urlparse(full)
        if p.netloc == host and pattern.search(unquote(p.path)):
            links.append(full)
    return dedupe(links)


def collect_domria(rate):
    out, seen, last_err = [], set(), None
    for base in DOMRIA_URLS:
        for page in range(1, DOMRIA_PAGES + 1):
            url = base if page == 1 else f"{base}?page={page}"
            try:
                links = extract_links(fetch(url), url, DOMRIA_LINK_RE)
            except Exception as e:
                last_err = e
                print(f"  DOM.RIA {url}: {e}")
                break
            fresh = [l for l in links if l not in seen]
            if not fresh:
                break
            seen.update(fresh)
            out += [(l, None, l) for l in fresh]
            time.sleep(random.uniform(0.5, 1.2))
    if not out and last_err:
        raise last_err
    print(f"DOM.RIA: знайдено посилань на оголошення: {len(out)}")
    return out, None


RIA_PHOTO_RE = re.compile(r"https://cdn[\w.-]*riastatic\.com/photosnew/dom/photo/[^\"'\s<>)\\]+?\.(?:jpe?g|webp)", re.I)


def domria_photos(html, soup):
    urls = []
    og = meta(soup, "og:image", "twitter:image")
    if og:
        urls.append(norm_img(og))
    for blob in json_blobs(soup):
        for d in walk(blob):
            if isinstance(d, dict) and "image" in d:
                urls += images_from(d["image"])
    by_id = {}
    for u in RIA_PHOTO_RE.findall(html.replace("\\/", "/")):
        m = re.search(r"(\d{6,})[a-z]{0,3}\.(?:jpe?g|webp)$", u, re.I)
        by_id.setdefault(m.group(1) if m else u, u)
    urls += list(by_id.values())
    # одне фото — один запис (за числовим ідентифікатором)
    uniq, ids = [], set()
    for u in dedupe(urls):
        m = re.search(r"(\d{6,})[a-z]{0,3}\.(?:jpe?g|webp)", u, re.I)
        k = m.group(1) if m else u
        if k not in ids:
            ids.add(k)
            uniq.append(u)
    return uniq[:MAX_PHOTOS]


def parse_page(url, rate):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    title = meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else url)
    desc = meta(soup, "og:description", "description")
    parts = []
    for sel in ('[data-cy="ad_description"]', "#descriptionBlock", '[data-testid="ad-price-container"]'):
        el = soup.select_one(sel)
        if el:
            parts.append(el.get_text(" ", strip=True))
    body = soup.get_text(" ", strip=True)
    text = " ".join([title, desc] + parts)
    if len(text) < 120:
        text += " " + body[:4000]
    price = ld_price_usd(soup, rate) or find_price([title, desc] + parts + [body[:3000]], rate)
    return base_item(
        url=url, title=title.strip(), photos=domria_photos(html, soup), price=price,
        rooms=find_rooms(title + " " + desc), in_city=bool(CITY_RE.search(title + " " + body)),
        **flags_from_text(text),
    )


# ----------------------------------------------------------------- OLX (API)
def olx_pick_ids(data):
    ids = {}
    for d in walk(data):
        if isinstance(d, dict):
            for k in ("category_id", "region_id", "city_id", "district_id"):
                if k in d and d[k] and k not in ids:
                    ids[k] = d[k]
    return ids


ROOMS_WORDS = {"odnokomnatnye": 1, "dvuhkomnatnye": 2, "trehkomnatnye": 3, "chetyrehkomnatnye": 4}


def olx_item(o, rate):
    title = o.get("title") or ""
    desc = clean_text(o.get("description"))
    params = {p.get("key"): p for p in (o.get("params") or []) if isinstance(p, dict)}
    price = None
    pv = (params.get("price") or {}).get("value") or {}
    val, cur = pv.get("value"), pv.get("currency")
    if val and cur == "USD":
        price = float(val)
    elif val and cur == "UAH":
        price = round(float(val) / rate)
    elif pv.get("converted_value") and pv.get("converted_currency") == "USD":
        price = float(pv["converted_value"])
    rooms = None
    for k, p in params.items():
        if "room" in str(k):
            v = p.get("value") or {}
            rooms = ROOMS_WORDS.get(v.get("key")) or (int(re.search(r"\d", str(v.get("label", ""))).group()) if re.search(r"\d", str(v.get("label", ""))) else None)
    rooms = rooms or find_rooms(title)
    loc = (o.get("location") or {})
    city = ((loc.get("city") or {}).get("name") or "")
    district = ((loc.get("district") or {}).get("name") or "")
    text = f"{title} {desc}"
    return base_item(
        url=(o.get("url") or "").split("?")[0], title=title.strip(), desc=desc[:300],
        photos=dedupe(images_from(o.get("photos") or []))[:MAX_PHOTOS], price=price, rooms=rooms,
        address=district, in_city=bool(CITY_RE.search(city + " " + title)) if city or title else True,
        **flags_from_text(text),
    )


def collect_olx(rate):
    s = requests.Session()
    s.headers.update({
        "User-Agent": HEADERS["User-Agent"], "Accept": "application/json",
        "Accept-Language": "uk-UA,uk;q=0.9", "Referer": OLX_PAGE, "Origin": "https://www.olx.ua",
    })
    ids = {}
    try:
        r = s.get(OLX_FRIENDLY, timeout=20)
        if r.ok:
            ids = olx_pick_ids(r.json())
    except Exception as e:
        print(f"  OLX friendly-links: {e}")
    params = {"offset": 0, "limit": 50, "sort_by": "created_at:desc", "currency": "USD",
              "filter_float_price:to": MAX_PRICE_NO_COMMISSION,
              "filter_enum_number_of_rooms_string[0]": "odnokomnatnye",
              "filter_enum_number_of_rooms_string[1]": "dvuhkomnatnye"}
    params.update({k: v for k, v in ids.items() if k in ("category_id", "region_id", "city_id", "district_id")})
    if "city_id" not in ids:
        params["query"] = "Тернопіль"
        params.setdefault("category_id", 1760)
        print("  OLX: city_id не визначено, шукаю за словом «Тернопіль»")
    r = s.get(OLX_API, params=params, timeout=25)
    r.raise_for_status()
    offers = r.json().get("data") or []
    out = []
    for o in offers:
        url = (o.get("url") or "").split("?")[0]
        if url:
            out.append((url, olx_item(o, rate), None))
    print(f"OLX: знайдено оголошень: {len(out)}")
    return out, None


# ----------------------------------------------------------------- LUN
def card_containers(soup):
    """Для кожного рядка з ціною знаходить контейнер-картку (вищий елемент з однією ціною)."""
    def is_price(s):
        return bool(PRICE_LINE_RE.match(s.strip()))
    nodes = soup.find_all(string=is_price)
    cards = []
    for node in nodes:
        el = node.parent
        for _ in range(8):
            parent = el.parent
            if parent is None or parent.name in ("body", "html"):
                break
            if len(parent.find_all(string=is_price)) > 1:
                break
            el = parent
        cards.append((node.strip(), el))
    return cards


def card_media(el, base):
    photos, links = [], []
    for img in el.find_all("img"):
        cand = [img.get("src"), img.get("data-src"), img.get("data-lazy-src")]
        ss = img.get("srcset") or img.get("data-srcset")
        if ss:
            cand.append(ss.split(",")[0].strip().split(" ")[0])
        photos += [norm_img(c, base) for c in cand if c]
    for src in el.find_all("source"):
        ss = src.get("srcset")
        if ss:
            photos.append(norm_img(ss.split(",")[0].strip().split(" ")[0], base))
    for m in re.finditer(r"url\(['\"]?([^)'\"]+)", str(el)):
        photos.append(norm_img(m.group(1), base))
    photos = [p for p in dedupe(photos) if IMG_EXT_RE.search(p) or "lunstatic" in p or "googleapis" in p]
    for a in el.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        p = urlparse(href)
        if p.netloc.endswith("lun.ua") and re.search(r"\d{6,}", p.path):
            links.append(href)
    for attr_el in [el] + el.find_all(True):
        for k, v in (attr_el.attrs or {}).items():
            if k.startswith("data-") and re.fullmatch(r"\d{9,18}", str(v)) and "id" in k:
                links.append("https://apps.lun.ua/realty/" + str(v))
    return photos, dedupe(links)


def lun_cards(html, url, rate):
    soup = BeautifulSoup(html, "lxml")
    listings = listing_dicts(soup)
    cards, used = [], set()
    for price_line, el in card_containers(soup):
        lines = [l.strip() for l in el.get_text("\n").split("\n") if l.strip()]
        text = " ".join(lines)
        if not re.search(r"кімнат", text, re.I):
            continue
        m = PRICE_LINE_RE.match(price_line)
        raw = num(m.group(1))
        cur = "UAH" if m.group(2).lower() == "грн" else "USD"
        price = round(raw / rate) if cur == "UAH" else raw
        rooms = None
        for l in lines:
            mm = re.fullmatch(r"(\d)\s*кімнат\w*", l, re.I)
            if mm:
                rooms = int(mm.group(1))
                break
        if rooms is None:
            mm = re.search(r"(\d)\s*[-–]?\s*кімнат", text, re.I)
            rooms = int(mm.group(1)) if mm else None
        am = re.search(r"(\d+(?:[.,]\d+)?)\s*/\s*[-\d.,]+\s*/\s*[-\d.,]+\s*м", text)
        area = am.group(1) if am else ""
        fm = re.search(r"поверх\s*(\d+)\s*з\s*(\d+)", text, re.I)
        floor = f"поверх {fm.group(1)} з {fm.group(2)}" if fm else ""
        address = next((l for l in lines if re.search(r"вул|просп|бульв|пров|площ|майдан|^\w+\s+вулиця", l, re.I) and len(l) < 70), "")
        desc = next((l for l in lines if len(l) > 60), "")
        photos, links = card_media(el, url)
        # збагачення з JSON сторінки (якщо картки не містять посилань/фото)
        if not links or not photos:
            for i, d in enumerate(listings):
                if i in used or not d["price"]:
                    continue
                if abs(d["price"] - raw) <= 1 and (d["cur"] in (None, cur)):
                    if d["rooms"] and rooms and str(d["rooms"]).isdigit() and int(d["rooms"]) != rooms:
                        continue
                    used.add(i)
                    if not links:
                        if d["url"] and "lun.ua" in d["url"]:
                            links = [d["url"].split("?")[0]]
                        elif d["id"]:
                            links = ["https://apps.lun.ua/realty/" + d["id"]]
                    if not photos:
                        photos = d["photos"]
                    break
        # стабільний ключ: не залежить від часу публікації, курсу та сусідніх карток
        key = "lun:" + text_key("|".join([address.lower(), str(rooms), area, floor, str(int(raw)), cur]))
        bits = [f"{rooms}-кімн." if rooms else "", f"{area} м²" if area else "", floor, address]
        cards.append((key, base_item(
            url=links[0] if links else LUN_URL, title=" · ".join(b for b in bits if b) or desc[:80],
            desc=desc[:300], photos=photos[:MAX_PHOTOS], price=price, rooms=rooms,
            area=area, floor=floor, address=address, direct=bool(links),
            **flags_from_text(text),
        )))
    return cards


def lun_debug_report(html, cards):
    soup = BeautifulSoup(html, "lxml")
    href_re = re.compile(r"href=.([^\"]*[0-9]{6,}[^\"]*)")
    hrefs = href_re.findall(html)[:5]
    lines = [f"LUN debug · розмір сторінки {len(html)} · карток {len(cards)}",
             f"з прямим посиланням: {sum(1 for _, c in cards if c['direct'])}",
             f"з фото: {sum(1 for _, c in cards if c['photos'])}",
             f"ld+json/json блоків: {sum(1 for _ in json_blobs(soup))}",
             f"схожих на оголошення словників у JSON: {len(listing_dicts(soup))}",
             f"lunstatic-зображень у HTML: {html.count('lunstatic')}",
             f"посилань з цифрами (приклади): {hrefs}", ""]
    conts = card_containers(soup)
    for i, (_, el) in enumerate(conts[:2]):
        lines.append(f"--- HTML картки {i + 1} ---")
        lines.append(re.sub(r"\s+", " ", str(el))[:2500])
    return "\n".join(lines)


def collect_lun(rate):
    out, seen, first_html, all_cards = [], set(), None, []
    for page in range(1, LUN_PAGES + 1):
        url = LUN_URL if page == 1 else f"{LUN_URL}?page={page}"
        try:
            html = fetch(url)
        except Exception as e:
            if page == 1:
                raise
            print(f"  LUN сторінка {page}: {e}")
            break
        if first_html is None:
            first_html = html
        fresh = [c for c in lun_cards(html, url, rate) if c[0] not in seen]
        if not fresh:
            break
        for k, c in fresh:
            seen.add(k)
            out.append((k, c, None))
            all_cards.append((k, c))
        time.sleep(random.uniform(0.5, 1.2))
    direct = sum(1 for _, c in all_cards if c["direct"])
    photos = sum(1 for _, c in all_cards if c["photos"])
    print(f"LUN: карток: {len(out)} (з прямим посиланням: {direct}, з фото: {photos})")
    note = None
    if out and (direct == 0 or photos == 0):
        note = lun_debug_report(first_html, all_cards)
        print(note[:1500])
    return out, note


COLLECTORS = {"OLX": collect_olx, "DOM.RIA": collect_domria, "LUN": collect_lun}


# ----------------------------------------------------------------- фільтр і повідомлення
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
    lines = [f"{head} · {site}", f"{price}{rooms} · {repair}", it["title"]]
    if it.get("desc"):
        lines.append(it["desc"])
    if site == "LUN" and not it.get("direct"):
        lines.append("(LUN не віддав пряме посилання — відкрий список і знайди за адресою/ціною)")
    lines.append(it["url"])
    return "\n".join(l for l in lines if l)


# ----------------------------------------------------------------- Telegram
def creds():
    return os.environ["TELEGRAM_TOKEN"].strip(), os.environ["TELEGRAM_CHAT_ID"].strip()


def tg(method, **kw):
    token, _ = creds()
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, timeout=60, **kw)
    if r.status_code == 429:
        try:
            wait = int(r.json().get("parameters", {}).get("retry_after", 5))
        except Exception:
            wait = 5
        time.sleep(min(wait + 1, 60))
        r = requests.post(url, timeout=60, **kw)
    return r


def send(text):
    _, chat = creds()
    r = tg("sendMessage", json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": False})
    r.raise_for_status()


def send_document(name, content, caption):
    _, chat = creds()
    tg("sendDocument", data={"chat_id": chat, "caption": caption[:900]}, files={"document": (name, content.encode("utf-8"))})


def send_photos(photos, caption):
    _, chat = creds()
    caption = caption[:1024]
    photos = photos[:MAX_PHOTOS]
    try:  # 1) Telegram сам завантажує фото за посиланням
        if len(photos) == 1:
            r = tg("sendPhoto", json={"chat_id": chat, "photo": photos[0], "caption": caption})
        else:
            media = [{"type": "photo", "media": u} for u in photos]
            media[0]["caption"] = caption
            r = tg("sendMediaGroup", json={"chat_id": chat, "media": media})
        if r.ok:
            return True
        print(f"  фото за посиланням не пройшло: {r.text[:120]}")
    except Exception as e:
        print(f"  фото за посиланням не пройшло: {e}")
    blobs = []  # 2) качаємо самі й завантажуємо файлами
    for u in photos:
        try:
            rr = requests.get(u, headers={**HEADERS, "Referer": u}, timeout=25)
            if rr.ok and rr.content and len(rr.content) < 9_500_000 and rr.headers.get("content-type", "").startswith("image"):
                blobs.append(rr.content)
        except Exception:
            pass
    if not blobs:
        return False
    try:
        if len(blobs) == 1:
            r = tg("sendPhoto", data={"chat_id": chat, "caption": caption}, files={"photo": ("photo.jpg", blobs[0])})
        else:
            media, files = [], {}
            for i, b in enumerate(blobs):
                files[f"p{i}"] = (f"p{i}.jpg", b)
                m = {"type": "photo", "media": f"attach://p{i}"}
                if i == 0:
                    m["caption"] = caption
                media.append(m)
            r = tg("sendMediaGroup", data={"chat_id": chat, "media": json.dumps(media)}, files=files)
        return r.ok
    except Exception as e:
        print(f"  фото-файли не пройшли: {e}")
        return False


def send_item(site, item):
    text = message(site, item)
    if item.get("photos") and send_photos(item["photos"], text):
        return
    send(text)


# ----------------------------------------------------------------- стан
def load_state():
    s = {"seen": {}, "fails": {}, "sites": [], "schema": 0, "lun_debug": False}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    s.setdefault("seen", {})
    s.setdefault("fails", {})
    s.setdefault("sites", [])
    return s


def save_state(state):
    newest = sorted(state["seen"].items(), key=lambda kv: kv[1], reverse=True)[:4000]
    state["seen"] = dict(newest)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------- головна
def main():
    dry = "--dry" in sys.argv
    first = not os.path.exists(STATE_FILE) and not dry
    state = load_state()
    migrated = (not first) and state.get("schema") != SCHEMA
    if migrated:
        state["sites"] = []  # новий спосіб збору: усе поточне запам'ятовуємо мовчки
    state["schema"] = SCHEMA
    rate = usd_rate()
    now = int(time.time())
    queue, fetched, notes = [], 0, []

    for site, collect in COLLECTORS.items():
        try:
            entries, note = collect(rate)
            state["fails"][site] = 0
        except Exception as e:
            n = state["fails"].get(site, 0) + 1
            state["fails"][site] = n
            print(f"{site}: помилка {e}")
            if n == 3 and not dry:
                send(f"⚠️ {site}: 3 невдалі перевірки поспіль ({e}). Сайт міг заблокувати бота або змінити верстку.")
            continue
        if note:
            notes.append((site, note))

        site_first = first or (bool(entries) and site not in state["sites"])
        if entries and site not in state["sites"]:
            state["sites"].append(site)

        for key, item, url in entries:
            if key in state["seen"]:
                continue
            if site_first:
                state["seen"][key] = now
                continue
            if item is None:
                if fetched >= MAX_NEW_PER_RUN:
                    break
                fetched += 1
                time.sleep(random.uniform(1, 2.5))
                try:
                    item = parse_page(url, rate)
                except Exception as e:
                    print(f"  не вдалось відкрити {url}: {e}")
                    continue  # не позначаємо як побачене, спробуємо наступного разу
            state["seen"][key] = now
            ok, reason = evaluate(item)
            print(f"  {'+' if ok else '-'} {reason}: {item['title'][:60]}")
            if ok:
                queue.append((site, item))

    if dry:
        print(f"\nПройшли б фільтр: {len(queue)}")
        return

    if first:
        send(f"✅ Бот запущено. Поточні оголошення ({len(state['seen'])}) запам'ятав, надсилатиму тільки нові.")
    elif migrated:
        send("🔄 Бот оновлено до версії 3: OLX через API, прямі посилання й фото. Поточні оголошення запам'ятав, надсилатиму тільки нові.")

    queue.sort(key=lambda x: not x[1]["no_comm"])  # без комісії першими
    for site, item in queue:
        send_item(site, item)
        time.sleep(1.2)

    for site, note in notes:
        if site == "LUN" and not state.get("lun_debug"):
            send_document("lun_debug.txt", note, "Діагностика LUN: перешли цей файл у чат із Copilot")
            state["lun_debug"] = True

    save_state(state)


if __name__ == "__main__":
    main()
