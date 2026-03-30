import os
import re
import io
import json
import time
import html
from datetime import datetime, date
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

st.set_page_config(page_title="미야언니 관리프로그램", layout="wide")

BASE_URL = "https://www.misharp.co.kr"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
BASE_COLUMNS = [
    "product_no", "product_url", "product_name", "category", "sub_category", "price", "fabric",
    "fit_type", "size_range", "recommended_body_type", "body_cover_features", "style_tags",
    "season", "length_type", "sleeve_type", "color_options", "recommended_age",
    "coordination_items", "product_summary"
]
MEASUREMENT_COLUMNS = [
    "shoulder", "chest", "chest_measure_type", "armhole", "sleeve", "sleeve_circumference",
    "length", "length_front", "length_back", "measurement_source", "raw_measurements"
]
DB_COLUMNS = BASE_COLUMNS + MEASUREMENT_COLUMNS
EXCLUDED_URL_KEYWORDS = [
    "cate_no=", "/product/list", "/category/", "/board/", "/article/", "/member/",
    "/order/", "/myshop/", "/exec/front/newcoupon", "/search/", "/product/recent_view_product",
]
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))


def get_client():
    if OPENAI_API_KEY and OpenAI is not None:
        return OpenAI(api_key=OPENAI_API_KEY)
    return None


def clean_text(text: str) -> str:
    if text is None:
        return ""
    text = html.unescape(str(text))
    text = text.replace("\xa0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def uniq_keep_order(items):
    out, seen = [], set()
    for item in items:
        item = clean_text(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def to_abs_url(url: str) -> str:
    if not url:
        return ""
    return urljoin(BASE_URL, url)


def extract_product_no(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"product_no=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/product/[^\s\"'>]+/(\d+)(?:/|\?|$)", url)
    if m:
        return m.group(1)
    return ""


def normalize_product_url(url: str) -> str:
    pno = extract_product_no(url)
    return f"{BASE_URL}/product/detail.html?product_no={pno}" if pno else to_abs_url(url)


def is_product_url(url: str) -> bool:
    url = (url or "").lower()
    return bool(extract_product_no(url)) and "/product/list" not in url and "/category/" not in url


def is_category_url(url: str) -> bool:
    url = (url or "").lower()
    return ((("/product/list.html" in url and "cate_no=" in url) or "/category/" in url) and not is_product_url(url))


def build_page_url(category_url: str, page: int) -> str:
    parsed = urlparse(category_url)
    q = parse_qs(parsed.query)
    q["page"] = [str(page)]
    new_query = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def fetch_html(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Referer": BASE_URL}
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.text


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_html_cached(url: str) -> str:
    return fetch_html(url)


def extract_total_count(html_text: str):
    t = clean_text(html_text)
    m = re.search(r"TOTAL\s*[:：]?\s*(\d+)", t, flags=re.I)
    return int(m.group(1)) if m else None


def parse_product_cards_from_category_html(category_url: str, html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    records = []
    candidates = soup.select("li[id^='anchorBoxId_']")
    if not candidates:
        candidates = soup.select("ul.prdList > li, .xans-product-listnormal li, .prdList li")

    for li in candidates:
        li_html = str(li)
        pno = ""
        m = re.search(r"anchorBoxId_(\d+)", li.get("id", ""))
        if m:
            pno = m.group(1)

        link = ""
        for a in li.select("a[href]"):
            href = to_abs_url(a.get("href", ""))
            if not href:
                continue
            if any(bad in href.lower() for bad in EXCLUDED_URL_KEYWORDS):
                continue
            found_pno = extract_product_no(href)
            if found_pno:
                pno = pno or found_pno
                link = normalize_product_url(href)
                break

        if not pno:
            m = re.search(r"product_no=(\d+)", li_html)
            if m:
                pno = m.group(1)
                link = f"{BASE_URL}/product/detail.html?product_no={pno}"

        if not pno:
            continue

        text = clean_text(li.get_text(" ", strip=True))
        name = ""
        m = re.search(r"상품명\s*[:：]\s*(.*?)(?:상품 요약설명|판매가|할인판매가|$)", text)
        if m:
            name = clean_text(m.group(1))
        if not name:
            texts = [clean_text(a.get_text(" ", strip=True)) for a in li.select("a")]
            texts = [x for x in texts if len(x) >= 4 and not re.fullmatch(r"자세히|장바구니 담기|관심상품 등록 전|할인기간|닫기", x)]
            if texts:
                name = max(texts, key=len)

        price = ""
        m = re.search(r"할인판매가\s*[:：]\s*([0-9,]+)원", text)
        if m:
            price = m.group(1).replace(",", "")
        if not price:
            m = re.search(r"판매가\s*[:：]\s*([0-9,]+)원", text)
            if m:
                price = m.group(1).replace(",", "")

        summary = ""
        m = re.search(r"상품 요약설명\s*[:：]\s*(.*?)(?:판매가|할인판매가|$)", text)
        if m:
            summary = clean_text(m.group(1))

        records.append({
            "product_no": pno,
            "product_url": link or f"{BASE_URL}/product/detail.html?product_no={pno}",
            "card_name": name,
            "card_price": price,
            "card_summary": summary,
        })

    cleaned = []
    seen = set()
    for r in records:
        pno = clean_text(r.get("product_no"))
        if not pno.isdigit():
            continue
        name = clean_text(r.get("card_name"))
        if not name or name in {"전체상품", "이번주 신상"}:
            continue
        if pno in seen:
            continue
        seen.add(pno)
        cleaned.append(r)
    return cleaned


def collect_product_cards_from_category(category_url: str, max_products: int = 500, delay_sec: float = 0.2):
    first_html = fetch_html_cached(category_url)
    total_count = extract_total_count(first_html)
    all_cards = []
    seen = set()
    page = 1
    empty_streak = 0
    max_pages = 50

    while page <= max_pages:
        page_url = category_url if page == 1 else build_page_url(category_url, page)
        try:
            html_text = first_html if page == 1 else fetch_html(page_url)
        except Exception:
            break

        cards = parse_product_cards_from_category_html(category_url, html_text)
        newly_added = 0
        for c in cards:
            pno = c["product_no"]
            if pno in seen:
                continue
            seen.add(pno)
            all_cards.append(c)
            newly_added += 1
            if len(all_cards) >= max_products:
                return all_cards, total_count

        if newly_added == 0:
            empty_streak += 1
        else:
            empty_streak = 0
        if total_count and len(all_cards) >= total_count:
            break
        if empty_streak >= 2:
            break

        page += 1
        if delay_sec > 0:
            time.sleep(delay_sec)

    return all_cards, total_count


def normalize_name(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"\s*\([^)]*color[^)]*\)", "", name, flags=re.I)
    return clean_text(name)


def infer_category_from_name(name: str):
    name_l = (name or "").lower()
    pairs = [
        ("아우터", "자켓", ["자켓", "재킷", "jk"]),
        ("아우터", "점퍼", ["점퍼", "후드", "사파리"]),
        ("아우터", "코트", ["코트"]),
        ("니트/가디건", "니트", ["니트"]),
        ("니트/가디건", "가디건", ["가디건"]),
        ("팬츠", "슬랙스", ["슬랙스"]),
        ("팬츠", "데님", ["데님", "청바지", "진"]),
        ("팬츠", "팬츠", ["팬츠", "바지"]),
        ("블라우스/셔츠", "블라우스", ["블라우스"]),
        ("블라우스/셔츠", "셔츠", ["셔츠"]),
        ("티셔츠", "티셔츠", ["티셔츠", "맨투맨", "mtm"]),
        ("원피스/스커트", "원피스", ["원피스"]),
        ("원피스/스커트", "스커트", ["스커트"]),
    ]
    for cat, sub, kws in pairs:
        if any(kw in name_l for kw in kws):
            return cat, sub
    return "", ""


def infer_fabric(text: str) -> str:
    t = clean_text(text)
    patterns = [
        r"(면\s*\d+%[^\n,.]*)", r"(코튼\s*\d+%[^\n,.]*)", r"(폴리(?:에스터)?\s*\d+%[^\n,.]*)",
        r"(레이온\s*\d+%[^\n,.]*)", r"(울\s*\d+%[^\n,.]*)", r"(비스코스\s*\d+%[^\n,.]*)",
        r"(나일론\s*\d+%[^\n,.]*)", r"(스판(?:덱스)?\s*\d+%[^\n,.]*)",
    ]
    found = []
    for p in patterns:
        for m in re.findall(p, t, flags=re.I):
            cm = clean_text(m)
            if cm not in found:
                found.append(cm)
    return " / ".join(found[:4]) if found else ""


def infer_fit_type(text: str) -> str:
    t = clean_text(text)
    rules = [
        ("오버핏", ["오버핏"]),
        ("루즈핏", ["루즈핏"]),
        ("세미루즈", ["세미루즈", "여유 있는 핏", "살짝 여유"]),
        ("슬림핏", ["슬림핏"]),
        ("정핏", ["정핏", "단정한 핏", "기본 핏"]),
        ("A라인", ["A라인", "A LINE"]),
    ]
    for val, keys in rules:
        if any(k in t for k in keys):
            return val
    return ""


def infer_style_tags(text: str, name: str) -> str:
    t = f"{name} {text}"
    rules = [
        ("클래식", ["클래식", "단정", "라운드 자켓"]),
        ("페미닌", ["페미닌", "여성스러운", "플라워", "트위드"]),
        ("데일리", ["데일리", "기본", "편하게"]),
        ("오피스룩", ["오피스", "출근룩", "직장인"]),
        ("학모룩", ["학부모", "학교", "상담룩"]),
        ("모임룩", ["모임룩", "하객룩"]),
    ]
    out = [tag for tag, keys in rules if any(k in t for k in keys)]
    return ";".join(out[:4])


def infer_season(text: str, name: str) -> str:
    t = f"{name} {text}"
    out = []
    for tag, keys in [
        ("봄", ["봄", "간절기"]),
        ("여름", ["여름", "반팔", "린넨"]),
        ("가을", ["가을", "간절기"]),
        ("겨울", ["겨울", "울", "기모"]),
        ("간절기", ["간절기"]),
    ]:
        if any(k in t for k in keys) and tag not in out:
            out.append(tag)
    return ";".join(out[:3])


def infer_length_type(name: str, text: str) -> str:
    t = f"{name} {text}"
    if "크롭" in t:
        return "크롭"
    if "롱" in t:
        return "롱"
    if "하프" in t:
        return "하프"
    return "기본"


def infer_sleeve_type(name: str, text: str) -> str:
    t = f"{name} {text}"
    if "반팔" in t:
        return "반팔"
    if "퍼프" in t:
        return "퍼프소매"
    if "드롭숄더" in t:
        return "드롭숄더"
    return "긴팔"


def infer_color_options(text: str, name: str) -> str:
    t = f"{name} {text}"
    colors = [c for c in ["블랙", "화이트", "아이보리", "베이지", "그레이", "핑크", "네이비", "브라운", "카키", "소라"] if c in t]
    m = re.search(r"\((\d+)\s*color\)", name, flags=re.I)
    if not colors and m:
        return f"{m.group(1)}컬러"
    return ";".join(colors[:6]) if colors else ""


def infer_body_cover(text: str, name: str) -> str:
    t = f"{name} {text}"
    rules = [
        ("팔뚝커버", ["팔뚝", "퍼프", "레글런"]),
        ("뱃살커버", ["복부", "배라인", "군살"]),
        ("힙커버", ["힙", "롱기장", "하프"]),
        ("허리라인보정", ["허리선", "라인", "A라인"]),
    ]
    out = [tag for tag, keys in rules if any(k in t for k in keys)]
    return ";".join(out[:4])


def infer_recommended_body_type(name: str, text: str) -> str:
    t = f"{name} {text}"
    out = []
    if any(k in t for k in ["어깨", "레글런"]):
        out.append("어깨좁음")
    if any(k in t for k in ["팔뚝", "퍼프"]):
        out.append("팔뚝통통")
    if any(k in t for k in ["복부", "배라인", "허리"]):
        out.append("복부체형")
    if any(k in t for k in ["와이드", "A라인", "힙"]):
        out.append("하체통통")
    if not out:
        out.append("4050 여성 일반체형")
    return ";".join(out[:3])


def infer_coordination_items(name: str, text: str) -> str:
    t = f"{name} {text}"
    out = []
    for item in ["슬랙스", "데님", "스커트", "원피스", "니트"]:
        if item in t:
            out.append(item)
    return ";".join(out[:4]) if out else "슬랙스;데님;스커트"


def extract_detail_text_blocks(soup: BeautifulSoup) -> str:
    blocks = []
    selectors = [
        "meta[property='og:title']",
        ".headingArea h2", ".headingArea h3", ".headingArea .name",
        ".infoArea", ".xans-product-detaildesign", ".prdInfo", ".detailArea",
        "#prdDetail", ".cont", ".ec-base-table", ".xans-product-additional"
    ]
    for sel in selectors:
        for node in soup.select(sel):
            if node.name == "meta":
                txt = clean_text(node.get("content", ""))
            else:
                txt = clean_text(node.get_text(" ", strip=True))
            if txt:
                blocks.append(txt)
    return "\n".join(uniq_keep_order(blocks))[:20000]


def extract_size_context(soup: BeautifulSoup, full_text: str) -> str:
    blocks = []
    for sel in [".infoArea", ".xans-product-detaildesign", ".detailArea", "#prdDetail", ".ec-base-table", "table"]:
        for node in soup.select(sel):
            txt = clean_text(node.get_text(" ", strip=True))
            if any(k.lower() in txt.lower() for k in ["사이즈", "추천", "free", "프리", "55", "66", "77", "88", "xl", "l(", "xl("]):
                blocks.append(txt)
    if not blocks:
        blocks.append(full_text)
    return " ".join(uniq_keep_order(blocks))[:12000]


def infer_size_range(text: str):
    t = clean_text(text)
    if not t:
        return "", ""

    ranges = re.findall(r"(44|55반|55|66반|66|77반|77|88|99)\s*[-~]\s*(44|55반|55|66반|66|77반|77|88|99)", t)
    if ranges:
        order = {"44": 1, "55": 2, "55반": 3, "66": 4, "66반": 5, "77": 6, "77반": 7, "88": 8, "99": 9}
        mins = sorted(ranges, key=lambda x: order.get(x[0], 999))[0][0]
        maxs = sorted(ranges, key=lambda x: order.get(x[1], -1))[-1][1]
        return f"{mins}-{maxs}", "option_range"

    m = re.search(r"(44|55반|55|66반|66|77반|77|88|99)\s*까지\s*(?:추천|착용|가능)?", t)
    if m:
        return f"55-{m.group(1)}", "recommend_text"

    singles = re.findall(r"(?<!\d)(44|55반|55|66반|66|77반|77|88|99)(?!\d)", t)
    if len(singles) >= 2:
        uniq = []
        for s in singles:
            if s not in uniq:
                uniq.append(s)
        return f"{uniq[0]}-{uniq[-1]}", "size_tokens"

    if re.search(r"\bFREE\b|프리사이즈|FREE사이즈|F사이즈|\bF\b", t, re.I):
        return "55-66", "free_default"

    return "", ""


def _extract_number(text: str) -> str:
    m = re.search(r"(-?\d+(?:\.\d+)?)", clean_text(text))
    return m.group(1) if m else ""


def _normalize_measure_header(header: str) -> str:
    h = clean_text(header).replace(" ", "")
    if not h:
        return ""
    if "어깨" in h:
        return "shoulder"
    if "가슴" in h and "둘레" in h:
        return "chest_circumference"
    if "가슴" in h:
        return "chest"
    if "암홀" in h:
        return "armhole"
    if "소매" in h and "둘레" in h:
        return "sleeve_circumference"
    if "소매" in h:
        return "sleeve"
    if ("총장" in h or "기장" in h) and "앞" in h:
        return "length_front"
    if ("총장" in h or "기장" in h) and "뒤" in h:
        return "length_back"
    if "총장" in h or "기장" in h:
        return "length"
    return ""


def _measurement_payload():
    return {
        "shoulder": "", "chest": "", "chest_measure_type": "", "armhole": "", "sleeve": "",
        "sleeve_circumference": "", "length": "", "length_front": "", "length_back": "",
        "measurement_source": "", "raw_measurements": "",
    }


def _apply_measure_value(payload: dict, key: str, raw_value: str):
    value = _extract_number(raw_value)
    if not value:
        return
    if key == "chest_circumference":
        payload["chest_measure_type"] = "circumference"
        try:
            f = float(value)
            payload["chest"] = str(int(f / 2)) if float(f / 2).is_integer() else f"{f/2:.1f}"
        except Exception:
            payload["chest"] = value
        return
    payload[key] = value


def parse_measurement_tables(soup: BeautifulSoup) -> dict:
    payload = _measurement_payload()
    raw_pairs = []
    found = False

    for table in soup.select("table"):
        rows = []
        for tr in table.select("tr"):
            cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.select("th,td")]
            cells = [c for c in cells if c]
            if cells:
                rows.append(cells)
        if not rows:
            continue

        flat = " ".join([" ".join(r) for r in rows])
        if not any(k in flat for k in ["어깨", "가슴", "암홀", "소매", "총장", "기장"]):
            continue

        if len(rows) >= 2:
            headers = rows[0]
            values = rows[1]
            if len(headers) == len(values) and len(headers) >= 2:
                local_hits = 0
                for h, v in zip(headers, values):
                    key = _normalize_measure_header(h)
                    if key:
                        _apply_measure_value(payload, key, v)
                        raw_pairs.append({clean_text(h): _extract_number(v)})
                        local_hits += 1
                if local_hits >= 2:
                    found = True

        for row in rows:
            if len(row) >= 2:
                key = _normalize_measure_header(row[0])
                if key:
                    _apply_measure_value(payload, key, row[1])
                    raw_pairs.append({clean_text(row[0]): _extract_number(row[1])})
                    found = True

    if payload["length"] == "":
        vals = [v for v in [payload["length_front"], payload["length_back"]] if v]
        if vals:
            try:
                payload["length"] = str(max(float(v) for v in vals)).rstrip('0').rstrip('.')
            except Exception:
                payload["length"] = vals[-1]

    if found:
        payload["measurement_source"] = "table"
        payload["raw_measurements"] = json.dumps(raw_pairs, ensure_ascii=False)
    return payload


def parse_measurements_from_text(full_text: str) -> dict:
    payload = _measurement_payload()
    patterns = {
        "shoulder": [r"어깨단면", r"어깨"],
        "chest": [r"가슴단면", r"가슴"],
        "armhole": [r"암홀둘레", r"암홀"],
        "sleeve": [r"소매길이", r"소매장", r"소매"],
        "sleeve_circumference": [r"소매둘레"],
        "length_front": [r"총장\(앞\)", r"앞총장"],
        "length_back": [r"총장\(뒤\)", r"뒤총장"],
        "length": [r"총장", r"기장"],
    }
    raw_pairs = []
    for field, keys in patterns.items():
        for key in keys:
            m = re.search(rf"(?:{key})\s*[:：]?\s*(-?\d+(?:\.\d+)?)", full_text)
            if m:
                payload[field] = m.group(1)
                raw_pairs.append({key: m.group(1)})
                break

    m = re.search(r"가슴둘레\s*[:：]?\s*(-?\d+(?:\.\d+)?)", full_text)
    if m and not payload["chest"]:
        raw_val = m.group(1)
        payload["chest_measure_type"] = "circumference"
        try:
            f = float(raw_val)
            payload["chest"] = str(int(f / 2)) if float(f / 2).is_integer() else f"{f/2:.1f}"
        except Exception:
            payload["chest"] = raw_val
        raw_pairs.append({"가슴둘레": raw_val})

    if payload["length"] == "":
        vals = [v for v in [payload["length_front"], payload["length_back"]] if v]
        if vals:
            try:
                payload["length"] = str(max(float(v) for v in vals)).rstrip('0').rstrip('.')
            except Exception:
                payload["length"] = vals[-1]

    if any(payload[k] for k in ["shoulder", "chest", "armhole", "sleeve", "sleeve_circumference", "length", "length_front", "length_back"]):
        payload["measurement_source"] = "text"
        payload["raw_measurements"] = json.dumps(raw_pairs, ensure_ascii=False)
    return payload


def parse_detail_page(url: str, fallback_name: str = "", fallback_price: str = "", fallback_summary: str = "") -> dict:
    html_text = fetch_html_cached(url)
    soup = BeautifulSoup(html_text, "html.parser")
    full_text = extract_detail_text_blocks(soup)

    name = fallback_name
    for sel in [".headingArea h2", ".headingArea h3", ".headingArea .name"]:
        node = soup.select_one(sel)
        if node and clean_text(node.get_text(" ", strip=True)):
            name = clean_text(node.get_text(" ", strip=True))
            break
    if not name:
        meta = soup.select_one("meta[property='og:title']")
        if meta:
            name = clean_text(meta.get("content", ""))
    name = normalize_name(name)

    price = fallback_price
    if not price:
        text = clean_text(full_text)
        m = re.search(r"할인판매가\s*[:：]?\s*([0-9,]+)원", text)
        if m:
            price = m.group(1).replace(",", "")
        else:
            m = re.search(r"판매가\s*[:：]?\s*([0-9,]+)원", text)
            if m:
                price = m.group(1).replace(",", "")

    category, sub_category = infer_category_from_name(name)
    fabric = infer_fabric(full_text)
    size_range, size_source = infer_size_range(extract_size_context(soup, full_text))
    fit_type = infer_fit_type(full_text)
    recommended_body_type = infer_recommended_body_type(name, full_text)
    body_cover_features = infer_body_cover(full_text, name)
    style_tags = infer_style_tags(full_text, name)
    season = infer_season(full_text, name)
    length_type = infer_length_type(name, full_text)
    sleeve_type = infer_sleeve_type(name, full_text)
    color_options = infer_color_options(full_text, name)
    coordination_items = infer_coordination_items(name, full_text)

    measurements = parse_measurement_tables(soup)
    if not measurements.get("measurement_source"):
        measurements = parse_measurements_from_text(full_text)

    summary_src = fallback_summary or full_text
    summary_clean = clean_text(summary_src)
    summary_clean = re.sub(r"^.*?상품 요약설명\s*[:：]?", "", summary_clean)
    summary_clean = re.sub(r"(최근 본 상품|전체상품목록 바로가기|본문 바로가기|LOGIN|JOIN|MYPAGE|CART|ABOUT|SHOP).*$", "", summary_clean)
    product_summary = clean_text(summary_clean)[:220] or clean_text(name)[:220]

    return {
        "product_no": extract_product_no(url),
        "product_url": normalize_product_url(url),
        "product_name": name,
        "category": category,
        "sub_category": sub_category,
        "price": price,
        "fabric": fabric,
        "fit_type": fit_type,
        "size_range": size_range or "",
        "recommended_body_type": recommended_body_type,
        "body_cover_features": body_cover_features,
        "style_tags": style_tags,
        "season": season,
        "length_type": length_type,
        "sleeve_type": sleeve_type,
        "color_options": color_options,
        "recommended_age": "4050",
        "coordination_items": coordination_items,
        "product_summary": product_summary,
        **measurements,
        "_size_source": size_source,
    }


def normalize_with_openai(row: dict) -> dict:
    client = get_client()
    if client is None:
        return row
    normalize_cols = BASE_COLUMNS
    prompt = f"""
다음 미샵 상품 정보를 미야언니 DB 형식으로 표준화하세요.
반드시 아래 키만 JSON으로 반환하세요.
키 순서:
{normalize_cols}
규칙:
- 출력은 한국어
- style_tags, body_cover_features, coordination_items, season은 세미콜론(;)으로 구분
- product_summary는 120자 내외
- 빈 값은 추론하되 과장 금지
- 제공된 product_no, product_url, product_name, price는 바꾸지 말 것
입력 데이터:
{json.dumps({k: row.get(k, '') for k in DB_COLUMNS}, ensure_ascii=False)}
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.2,
            messages=[
                {"role": "system", "content": "너는 패션 상품 DB 정규화 전문가다."},
                {"role": "user", "content": prompt},
            ],
        )
        text = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return row
        data = json.loads(m.group(0))
        out = row.copy()
        for col in normalize_cols:
            out[col] = clean_text(data.get(col, row.get(col, "")))
        return out
    except Exception:
        return row


def build_dataframe(rows):
    normalized = []
    for r in rows:
        item = {col: clean_text(r.get(col, "")) for col in DB_COLUMNS}
        normalized.append(item)
    df = pd.DataFrame(normalized, columns=DB_COLUMNS)
    if not df.empty:
        df = df.drop_duplicates(subset=["product_no"], keep="first")
        df = df[df["product_no"].astype(str).str.fullmatch(r"\d+")]
        df = df[df["product_name"].astype(str).str.len() >= 2]
    return df.reset_index(drop=True)


def analyze_urls(input_text: str, use_openai: bool, delay_sec: float, max_products: int):
    urls = [clean_text(x) for x in re.split(r"[\n,]", input_text) if clean_text(x)]
    if not urls:
        return pd.DataFrame(columns=DB_COLUMNS), []

    audit = []
    rows = []
    product_targets = []

    for url in urls:
        if is_category_url(url):
            cards, total_count = collect_product_cards_from_category(url, max_products=max_products, delay_sec=delay_sec)
            product_targets.extend(cards)
            audit.append({"input_url": url, "type": "category", "detected_total": total_count or "", "collected_products": len(cards)})
        elif is_product_url(url):
            purl = normalize_product_url(url)
            product_targets.append({"product_no": extract_product_no(purl), "product_url": purl, "card_name": "", "card_price": "", "card_summary": ""})
            audit.append({"input_url": url, "type": "product", "detected_total": "", "collected_products": 1})

    dedup_targets = []
    seen = set()
    for t in product_targets:
        pno = clean_text(t.get("product_no"))
        if not pno or pno in seen:
            continue
        seen.add(pno)
        dedup_targets.append(t)

    prog = st.progress(0)
    status = st.empty()
    total = len(dedup_targets)

    for i, t in enumerate(dedup_targets, start=1):
        status.info(f"{i}/{total} 처리 중: {t.get('product_no')} {t.get('card_name','')[:40]}")
        try:
            row = parse_detail_page(t["product_url"], fallback_name=t.get("card_name", ""), fallback_price=t.get("card_price", ""), fallback_summary=t.get("card_summary", ""))
            if use_openai:
                row = normalize_with_openai(row)
            rows.append(row)
        except Exception as e:
            audit.append({"input_url": t.get("product_url", ""), "type": "detail_error", "detected_total": "", "collected_products": str(e)})
        prog.progress(i / max(total, 1))
        if delay_sec > 0:
            time.sleep(delay_sec)

    status.success(f"완료: {len(rows)}개 상품 DB 생성")
    df = build_dataframe(rows)
    return df, audit


def ensure_db_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=DB_COLUMNS)
    out = df.copy()
    out.columns = [clean_text(c) for c in out.columns]
    for col in DB_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[DB_COLUMNS].copy()
    for col in out.columns:
        out[col] = out[col].fillna("").astype(str).map(clean_text)
    return out


def parse_uploaded_table(uploaded_file):
    if uploaded_file is None:
        return None
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        for enc in ["utf-8-sig", "utf-8", "cp949", "euc-kr"]:
            try:
                uploaded_file.seek(0)
                return pd.read_csv(uploaded_file, encoding=enc)
            except Exception:
                continue
        uploaded_file.seek(0)
        return pd.read_csv(uploaded_file)
    if name.endswith(".xlsx"):
        uploaded_file.seek(0)
        return pd.read_excel(uploaded_file)
    if name.endswith(".jsonl"):
        uploaded_file.seek(0)
        rows = [json.loads(line) for line in uploaded_file.read().decode("utf-8").splitlines() if clean_text(line)]
        return pd.DataFrame(rows)
    if name.endswith(".json"):
        uploaded_file.seek(0)
        obj = json.load(uploaded_file)
        if isinstance(obj, list):
            return pd.DataFrame(obj)
        if isinstance(obj, dict):
            for key in ["rows", "data", "logs", "items"]:
                if isinstance(obj.get(key), list):
                    return pd.DataFrame(obj[key])
            return pd.DataFrame([obj])
    return None


def compare_db(current_df: pd.DataFrame, new_df: pd.DataFrame):
    current_df = ensure_db_columns(current_df)
    new_df = ensure_db_columns(new_df)
    current_no = set(current_df["product_no"].astype(str)) if not current_df.empty else set()
    new_no = set(new_df["product_no"].astype(str)) if not new_df.empty else set()
    added = sorted(new_no - current_no)
    removed = sorted(current_no - new_no)
    same = sorted(current_no & new_no)

    changed_rows = []
    current_map = current_df.set_index("product_no").to_dict(orient="index") if not current_df.empty else {}
    new_map = new_df.set_index("product_no").to_dict(orient="index") if not new_df.empty else {}
    for pno in same:
        before = current_map.get(pno, {})
        after = new_map.get(pno, {})
        changed_cols = [c for c in DB_COLUMNS if clean_text(before.get(c, "")) != clean_text(after.get(c, ""))]
        if changed_cols:
            changed_rows.append({
                "product_no": pno,
                "product_name": after.get("product_name") or before.get("product_name"),
                "changed_columns": ", ".join(changed_cols),
                "changed_count": len(changed_cols),
            })

    added_df = new_df[new_df["product_no"].isin(added)].copy() if added else pd.DataFrame(columns=DB_COLUMNS)
    removed_df = current_df[current_df["product_no"].isin(removed)].copy() if removed else pd.DataFrame(columns=DB_COLUMNS)
    changed_df = pd.DataFrame(changed_rows)
    return added_df, removed_df, changed_df


def guess_column(df: pd.DataFrame, candidates):
    cols = {c.lower(): c for c in df.columns}
    for cand in candidates:
        for lc, original in cols.items():
            if cand in lc:
                return original
    return None


def normalize_log_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [clean_text(c) for c in out.columns]

    ts_col = guess_column(out, ["timestamp", "time", "created_at", "date"])
    event_col = guess_column(out, ["event_type", "event", "type", "action"])
    product_no_col = guess_column(out, ["product_no", "pn", "상품번호"])
    product_name_col = guess_column(out, ["product_name", "상품명"])
    fallback_col = guess_column(out, ["fallback", "임시답변", "temporary_answer", "safe_llm_fallback"])
    error_col = guess_column(out, ["error", "rate_limit", "api_error", "timeout"])
    latency_col = guess_column(out, ["latency", "duration", "response_ms", "elapsed"])
    session_col = guess_column(out, ["session_id", "session", "conversation_id", "chat_id"])
    user_text_col = guess_column(out, ["user_text", "user_message", "question", "query"])

    normalized = pd.DataFrame()
    normalized["timestamp"] = pd.to_datetime(out[ts_col], errors="coerce") if ts_col else pd.NaT
    normalized["date"] = normalized["timestamp"].dt.date.astype("string") if ts_col else ""
    normalized["event_type"] = out[event_col].astype(str).map(clean_text) if event_col else ""
    normalized["product_no"] = out[product_no_col].astype(str).map(clean_text) if product_no_col else ""
    normalized["product_name"] = out[product_name_col].astype(str).map(clean_text) if product_name_col else ""
    normalized["session_id"] = out[session_col].astype(str).map(clean_text) if session_col else ""
    normalized["user_text"] = out[user_text_col].astype(str).map(clean_text) if user_text_col else ""

    if latency_col:
        normalized["latency_ms"] = pd.to_numeric(out[latency_col], errors="coerce")
    else:
        normalized["latency_ms"] = pd.NA

    if fallback_col:
        series = out[fallback_col].astype(str).str.lower().map(clean_text)
        normalized["is_fallback"] = series.isin(["1", "true", "y", "yes", "fallback", "임시답변", "전환"])
    else:
        normalized["is_fallback"] = normalized["event_type"].str.contains("fallback|temporary", case=False, na=False)

    if error_col:
        err_text = out[error_col].astype(str).map(clean_text)
    else:
        err_text = normalized["event_type"]
    normalized["error_text"] = err_text
    normalized["is_error"] = normalized["error_text"].str.contains("error|rate|timeout|fail", case=False, na=False)
    normalized["is_rate_limit"] = normalized["error_text"].str.contains("rate", case=False, na=False)

    if "url" in out.columns and (normalized["product_no"] == "").any():
        mask = normalized["product_no"] == ""
        normalized.loc[mask, "product_no"] = out.loc[mask, "url"].astype(str).map(extract_product_no)

    return normalized


def build_log_template():
    rows = [
        {
            "timestamp": "2026-03-30 09:10:11",
            "event_type": "user_question",
            "session_id": "sess_001",
            "product_no": "28628",
            "product_name": "쓰리 핀턱 소프트 배기핏 슬랙스",
            "user_text": "77도 가능할까요?",
            "is_fallback": False,
            "error_text": "",
            "latency_ms": 820,
        },
        {
            "timestamp": "2026-03-30 09:10:12",
            "event_type": "safe_llm_fallback",
            "session_id": "sess_001",
            "product_no": "28628",
            "product_name": "쓰리 핀턱 소프트 배기핏 슬랙스",
            "user_text": "77도 가능할까요?",
            "is_fallback": True,
            "error_text": "",
            "latency_ms": 1120,
        },
        {
            "timestamp": "2026-03-30 09:12:00",
            "event_type": "rate_limit_error",
            "session_id": "sess_002",
            "product_no": "",
            "product_name": "",
            "user_text": "배송 얼마나 걸려요?",
            "is_fallback": False,
            "error_text": "RateLimitError",
            "latency_ms": 0,
        },
    ]
    return pd.DataFrame(rows)


def get_today_log_df(log_df: pd.DataFrame) -> pd.DataFrame:
    if log_df is None or log_df.empty or "timestamp" not in log_df.columns:
        return pd.DataFrame()
    ts = pd.to_datetime(log_df["timestamp"], errors="coerce")
    today = pd.Timestamp.now().date()
    return log_df[ts.dt.date == today].copy()


def compute_chat_metrics(log_df: pd.DataFrame) -> dict:
    if log_df is None or log_df.empty:
        return {
            "today_consults": 0,
            "today_sessions": 0,
            "avg_latency": None,
            "fallback_count": 0,
            "error_count": 0,
            "rate_limit_count": 0,
            "top_products": pd.DataFrame(),
            "top_questions": pd.DataFrame(),
            "daily": pd.DataFrame(),
        }

    ts_valid = log_df.dropna(subset=["timestamp"]).copy() if "timestamp" in log_df.columns else pd.DataFrame()
    today_df = get_today_log_df(log_df)

    question_mask = pd.Series([True] * len(log_df))
    if "event_type" in log_df.columns:
        question_mask = log_df["event_type"].astype(str).str.contains("question|user|message|chat", case=False, na=False)
        if not question_mask.any():
            question_mask = pd.Series([True] * len(log_df), index=log_df.index)

    today_question_mask = pd.Series(dtype=bool)
    if not today_df.empty:
        today_question_mask = today_df["event_type"].astype(str).str.contains("question|user|message|chat", case=False, na=False)
        if not today_question_mask.any():
            today_question_mask = pd.Series([True] * len(today_df), index=today_df.index)

    today_consults = int(today_question_mask.sum()) if not today_df.empty else 0
    today_sessions = int(today_df["session_id"].replace("", pd.NA).dropna().nunique()) if (not today_df.empty and "session_id" in today_df.columns) else 0
    avg_latency = pd.to_numeric(today_df["latency_ms"], errors="coerce").dropna().mean() if (not today_df.empty and "latency_ms" in today_df.columns) else None
    fallback_count = int(today_df["is_fallback"].sum()) if (not today_df.empty and "is_fallback" in today_df.columns) else 0
    error_count = int(today_df["is_error"].sum()) if (not today_df.empty and "is_error" in today_df.columns) else 0
    rate_limit_count = int(today_df["is_rate_limit"].sum()) if (not today_df.empty and "is_rate_limit" in today_df.columns) else 0

    top_products = pd.DataFrame()
    if not today_df.empty and "product_no" in today_df.columns:
        top_products = today_df[today_df["product_no"].astype(str) != ""].groupby(["product_no", "product_name"], dropna=False).agg(
            상담수=("event_type", "size"),
            임시답변전환=("is_fallback", "sum"),
            평균응답ms=("latency_ms", "mean"),
        ).reset_index().sort_values("상담수", ascending=False).head(10)

    top_questions = pd.DataFrame()
    if not today_df.empty and "user_text" in today_df.columns:
        top_questions = today_df[today_df["user_text"].astype(str) != ""]["user_text"].value_counts().head(10).reset_index()
        if not top_questions.empty:
            top_questions.columns = ["질문", "횟수"]

    daily = pd.DataFrame()
    if not ts_valid.empty:
        daily = ts_valid.groupby(ts_valid["timestamp"].dt.date).agg(
            상담수=("event_type", "size"),
            임시답변전환=("is_fallback", "sum"),
            에러=("is_error", "sum"),
            평균응답ms=("latency_ms", "mean"),
        )

    return {
        "today_consults": today_consults,
        "today_sessions": today_sessions,
        "avg_latency": avg_latency,
        "fallback_count": fallback_count,
        "error_count": error_count,
        "rate_limit_count": rate_limit_count,
        "top_products": top_products,
        "top_questions": top_questions,
        "daily": daily,
    }


def render_common_upload_bar(current_db_df: pd.DataFrame, log_df: pd.DataFrame):
    st.markdown("### 데이터 불러오기")
    up1, up2 = st.columns(2)
    with up1:
        current_db_file = st.file_uploader("현재 미야언니 DB 업로드", type=["csv", "xlsx"], key="current_db_top")
    with up2:
        log_file = st.file_uploader("미야언니 V2 로그 업로드", type=["csv", "xlsx", "json", "jsonl"], key="log_file_top")

    st.caption("로그 파일은 미야언니 V2에서 남긴 상담 로그 파일입니다. 최소 컬럼 예시는 timestamp, event_type, session_id, product_no, product_name, user_text, is_fallback, error_text, latency_ms 입니다.")

    if log_file is not None:
        raw_log_df = parse_uploaded_table(log_file)
        log_df = normalize_log_df(raw_log_df) if raw_log_df is not None and not raw_log_df.empty else pd.DataFrame()
    if current_db_file is not None:
        current_db_df = ensure_db_columns(parse_uploaded_table(current_db_file))

    return current_db_df, log_df


def render_dashboard(db_df: pd.DataFrame, log_df: pd.DataFrame):
    st.subheader("메인 대시보드")
    st.caption("첫 화면은 DB가 아니라 상담 운영 상태를 바로 보는 화면으로 구성했습니다.")

    metrics = compute_chat_metrics(log_df)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("오늘 상담수", f"{metrics['today_consults']:,}")
    c2.metric("오늘 상담세션", f"{metrics['today_sessions']:,}")
    c3.metric("평균 상담시간(ms)", f"{metrics['avg_latency']:,.0f}" if pd.notna(metrics['avg_latency']) else "-")
    c4.metric("임시답변 전환", f"{metrics['fallback_count']:,}")
    c5.metric("에러 수", f"{metrics['error_count']:,}")
    c6.metric("RateLimit", f"{metrics['rate_limit_count']:,}")

    a, b = st.columns([1.5, 1])
    with a:
        st.markdown("##### 일별 상담 추이")
        if metrics["daily"] is not None and not metrics["daily"].empty:
            st.line_chart(metrics["daily"][["상담수", "임시답변전환", "에러"]])
        else:
            st.info("상담 로그를 올리면 여기서 일별 상담 추이를 바로 볼 수 있습니다.")
    with b:
        st.markdown("##### 로그 업로드는 이렇게")
        st.markdown(
            "- 미야언니 V2가 남긴 상담 로그 파일을 올리면 됩니다\n"
            "- 형식: CSV / XLSX / JSON / JSONL\n"
            "- 최소 권장 컬럼: `timestamp`, `event_type`, `session_id`, `product_no`, `product_name`, `user_text`, `is_fallback`, `error_text`, `latency_ms`\n"
            "- 아직 로그 파일이 없으면 아래 템플릿을 먼저 내려받아 구조를 맞추시면 됩니다"
        )
        template = build_log_template()
        st.download_button(
            "로그 템플릿 다운로드",
            data=template.to_csv(index=False).encode("utf-8-sig"),
            file_name="miya_v2_log_template.csv",
            mime="text/csv",
            use_container_width=True,
        )

    lower1, lower2 = st.columns(2)
    with lower1:
        st.markdown("##### 오늘 많이 상담된 상품")
        if metrics["top_products"] is not None and not metrics["top_products"].empty:
            st.dataframe(metrics["top_products"], use_container_width=True, height=320)
        else:
            st.info("오늘 로그 기준 상품별 상담수가 여기에 표시됩니다.")
    with lower2:
        st.markdown("##### 오늘 자주 나온 질문")
        if metrics["top_questions"] is not None and not metrics["top_questions"].empty:
            st.dataframe(metrics["top_questions"], use_container_width=True, hide_index=True, height=320)
        else:
            st.info("오늘 고객 질문이 누적되면 자주 나온 질문이 여기에 표시됩니다.")

    st.markdown("##### DB 현황 요약")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("현재 DB 상품 수", f"{len(db_df):,}" if db_df is not None else "0")
    d2.metric("사이즈 누락", f"{int((db_df['size_range'] == '').sum()):,}" if db_df is not None and not db_df.empty else "0")
    d3.metric("컬러 누락", f"{int((db_df['color_options'] == '').sum()):,}" if db_df is not None and not db_df.empty else "0")
    d4.metric("실측 누락", f"{int((db_df['measurement_source'] == '').sum()):,}" if db_df is not None and not db_df.empty else "0")


def render_db_generation():
    st.subheader("자동 상품DB 생성")
    st.caption("형준님이 이미 만든 DB생성기 출력 구조에 맞춰 misharp_miya_db.csv 형태로 생성합니다.")

    set1, set2, set3 = st.columns(3)
    with set1:
        use_openai = st.toggle("OpenAI 정규화", value=False)
    with set2:
        max_products = st.number_input("카테고리 최대 수집 수", min_value=1, max_value=2000, value=500, step=50)
    with set3:
        delay_sec = st.slider("요청 간 딜레이(초)", min_value=0.0, max_value=2.0, value=0.2, step=0.1)

    input_text = st.text_area(
        "상품 URL / 카테고리 URL 입력",
        height=180,
        placeholder="https://www.misharp.co.kr/product/list.html?cate_no=541\nhttps://www.misharp.co.kr/product/detail.html?product_no=28579",
    )

    col1, col2 = st.columns(2)
    preview_only = col1.button("카테고리 상품 URL 미리보기", use_container_width=True)
    run = col2.button("DB 생성 시작", type="primary", use_container_width=True)

    if preview_only and input_text.strip():
        urls = [clean_text(x) for x in re.split(r"[\n,]", input_text) if clean_text(x)]
        preview_rows = []
        for u in urls:
            if is_category_url(u):
                cards, total_count = collect_product_cards_from_category(u, max_products=max_products, delay_sec=delay_sec)
                for c in cards:
                    preview_rows.append({"product_no": c["product_no"], "product_name": c["card_name"], "product_url": c["product_url"]})
                st.info(f"카테고리 예상 TOTAL: {total_count or '미확인'} / 실제 수집: {len(cards)}")
            elif is_product_url(u):
                preview_rows.append({"product_no": extract_product_no(u), "product_name": "", "product_url": normalize_product_url(u)})
        if preview_rows:
            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True)

    if run and input_text.strip():
        df, audit = analyze_urls(input_text, use_openai, delay_sec, max_products)
        st.success(f"최종 DB 행 수: {len(df)}")
        st.dataframe(df, use_container_width=True, height=420)
        st.download_button("misharp_miya_db.csv 다운로드", data=df.to_csv(index=False).encode("utf-8-sig"), file_name="misharp_miya_db.csv", mime="text/csv", use_container_width=True)

        if audit:
            audit_df = pd.DataFrame(audit)
            st.markdown("##### 수집 감사 로그")
            st.dataframe(audit_df, use_container_width=True)
            st.download_button("audit CSV 다운로드", data=audit_df.to_csv(index=False).encode("utf-8-sig"), file_name="misharp_miya_db_audit.csv", mime="text/csv", use_container_width=True)


def render_db_compare(current_db_df: pd.DataFrame):
    st.subheader("반자동 DB업로드 준비")
    st.caption("현재 DB와 신규 생성 DB를 비교해서 전체 교체용과 신규 상품용 파일을 내려받는 화면입니다.")

    new_db_file = st.file_uploader("신규 생성 DB 업로드 (CSV/XLSX)", type=["csv", "xlsx"], key="new_db_upload")
    if new_db_file is None:
        st.info("신규 생성된 DB 파일을 올리면 현재 DB와 비교해서 추가/삭제/변경 상품을 보여드립니다.")
        return

    new_db_df = ensure_db_columns(parse_uploaded_table(new_db_file))
    if new_db_df is None or new_db_df.empty:
        st.error("신규 DB를 읽지 못했습니다.")
        return

    current_db_df = ensure_db_columns(current_db_df)
    added_df, removed_df, changed_df = compare_db(current_db_df, new_db_df)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 DB 상품 수", f"{len(current_db_df):,}")
    c2.metric("신규 DB 상품 수", f"{len(new_db_df):,}")
    c3.metric("신규 추가 상품", f"{len(added_df):,}")
    c4.metric("변경 상품", f"{len(changed_df):,}")

    tab1, tab2, tab3 = st.tabs(["신규 추가", "삭제 후보", "변경 상품"])
    with tab1:
        st.dataframe(added_df, use_container_width=True, height=360)
    with tab2:
        st.dataframe(removed_df, use_container_width=True, height=360)
    with tab3:
        st.dataframe(changed_df, use_container_width=True, height=360)

    st.markdown("##### 반영용 파일 다운로드")
    dl1, dl2 = st.columns(2)
    dl1.download_button("전체 교체용 misharp_miya_db.csv 다운로드", data=new_db_df.to_csv(index=False).encode("utf-8-sig"), file_name="misharp_miya_db.csv", mime="text/csv", use_container_width=True)
    dl2.download_button("신규 상품만 별도 다운로드", data=added_df.to_csv(index=False).encode("utf-8-sig"), file_name="misharp_miya_db_new_only.csv", mime="text/csv", use_container_width=True)

    st.markdown("##### 실무 반영 순서")
    st.markdown(
        "1. 여기서 **전체 교체용 misharp_miya_db.csv**를 다운로드합니다.\n"
        "2. 미야언니 V2 레포의 기존 `misharp_miya_db.csv`를 같은 파일명으로 덮어씁니다.\n"
        "3. GitHub 커밋 후 Streamlit 재배포 또는 재부팅합니다.\n"
        "4. 불안하면 신규 상품 파일로 먼저 점검 후 전체 교체하세요."
    )


def render_stats(log_df: pd.DataFrame, db_df: pd.DataFrame):
    st.subheader("미야언니 V2 통계 분석")
    st.caption("상담 로그를 올리면 상품별 상담량, 임시답변 전환, 에러, 응답속도를 분석합니다.")

    guide1, guide2 = st.columns([1.3, 1])
    with guide1:
        st.markdown("##### 어떤 로그를 올리면 되나요?")
        st.markdown(
            "- 미야언니 V2에서 남긴 상담 이벤트 로그 파일\n"
            "- 한 줄에 한 이벤트가 쌓이는 CSV/JSONL 형태가 가장 좋습니다\n"
            "- 추천 이벤트: user_question, chatbot_answer, safe_llm_fallback, rate_limit_error\n"
            "- 컬럼은 최소한 `timestamp`, `event_type`, `session_id`, `product_no`, `product_name`, `user_text`, `is_fallback`, `error_text`, `latency_ms` 정도가 있으면 좋습니다"
        )
    with guide2:
        template = build_log_template()
        st.download_button("로그 템플릿 다운로드", data=template.to_csv(index=False).encode("utf-8-sig"), file_name="miya_v2_log_template.csv", mime="text/csv", use_container_width=True)
        st.dataframe(template, use_container_width=True, hide_index=True, height=220)

    if log_df is None or log_df.empty:
        st.info("아직 로그 파일이 올라오지 않았습니다. 위 템플릿 구조에 맞춘 파일을 올리면 바로 분석됩니다.")
        return

    m1, m2, m3, m4, m5 = st.columns(5)
    total_logs = len(log_df)
    fallback_cnt = int(log_df["is_fallback"].sum())
    error_cnt = int(log_df["is_error"].sum())
    rate_cnt = int(log_df["is_rate_limit"].sum())
    avg_latency = pd.to_numeric(log_df["latency_ms"], errors="coerce").dropna().mean()
    m1.metric("총 로그", f"{total_logs:,}")
    m2.metric("임시답변 전환", f"{fallback_cnt:,}", f"{(fallback_cnt / total_logs * 100):.1f}%" if total_logs else None)
    m3.metric("에러", f"{error_cnt:,}", f"{(error_cnt / total_logs * 100):.1f}%" if total_logs else None)
    m4.metric("RateLimit", f"{rate_cnt:,}")
    m5.metric("평균 응답(ms)", f"{avg_latency:,.0f}" if pd.notna(avg_latency) else "-")

    if log_df["timestamp"].notna().any():
        st.markdown("##### 일별 로그 추이")
        daily_src = log_df.dropna(subset=["timestamp"]).copy()
        daily = daily_src.groupby(daily_src["timestamp"].dt.date).agg(total=("event_type", "size"), fallback=("is_fallback", "sum"), error=("is_error", "sum"))
        st.line_chart(daily)

    st.markdown("##### 상품별 상담/이상 징후")
    product_stats = log_df.copy()
    if db_df is not None and not db_df.empty and "product_no" in db_df.columns:
        product_map = db_df[["product_no", "product_name"]].drop_duplicates()
        product_stats = product_stats.merge(product_map, on="product_no", how="left", suffixes=("", "_db"))
        product_stats["product_name"] = product_stats["product_name"].replace("", pd.NA).fillna(product_stats.get("product_name_db", ""))

    grouped = product_stats[product_stats["product_no"].astype(str) != ""].groupby(["product_no", "product_name"], dropna=False).agg(
        상담수=("event_type", "size"),
        임시답변전환=("is_fallback", "sum"),
        에러수=("is_error", "sum"),
        평균응답ms=("latency_ms", "mean"),
    ).reset_index().sort_values(["상담수", "임시답변전환"], ascending=[False, False])
    st.dataframe(grouped.head(50), use_container_width=True, height=380)

    st.markdown("##### 자주 나온 질문 원문")
    if "user_text" in log_df.columns:
        questions = log_df[log_df["user_text"].astype(str) != ""]["user_text"].value_counts().head(20).reset_index()
        questions.columns = ["질문", "횟수"]
        st.dataframe(questions, use_container_width=True, hide_index=True)


def main():
    st.title("미야언니 관리프로그램")
    st.caption("자동 상품DB 생성 · 반자동 DB업로드 준비 · 미야언니 V2 통계 분석")

    st.markdown(
        """
        <style>
        div[data-baseweb="tab-list"] button {
            font-size: 15px !important;
            font-weight: 700 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    current_db_df = pd.DataFrame(columns=DB_COLUMNS)
    log_df = pd.DataFrame()
    current_db_df, log_df = render_common_upload_bar(current_db_df, log_df)

    tab1, tab2, tab3, tab4 = st.tabs(["메인 대시보드", "자동 상품DB 생성", "반자동 DB업로드 준비", "미야언니 V2 통계 분석"])
    with tab1:
        render_dashboard(current_db_df, log_df)
    with tab2:
        render_db_generation()
    with tab3:
        render_db_compare(current_db_df)
    with tab4:
        render_stats(log_df, current_db_df)

    st.markdown("---")
    st.caption("made by MISHARP COMPANY, MIYAWA. 2006. All rights reserved.")


if __name__ == "__main__":
    main()
