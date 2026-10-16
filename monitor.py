#!/usr/bin/env python3
"""독립형 쪽감시자: Vans 상품 판매 활성화, 공개 상품 목록/영역 변경을 Discord로 알림.

라이선스 서버 및 RADAR 실행 파일이 전혀 필요하지 않습니다. 로그인 쿠키를 요청하거나
외부 서버에 인증정보를 보내지 않습니다. 기본값은 첫 실행 알림 없음입니다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup
import requests

STATE_VERSION = 1
CAPTCHA_PHRASES = (
    "verify you are human", "access denied", "checking your browser",
    "unusual traffic", "captcha required", "cloudflare ray id",
)
MAX_SEEN = 10000
MAX_ITEMS = 500

# 판매 버튼은 반드시 상품 상세 영역 안에 있어야 한다. 전역 메뉴 및 추천상품 오탐 방지.
ADD_TO_CART = re.compile(r"\badd\s+to\s+(?:bag|cart)\b", re.I)
COMING_SOON = re.compile(r"\bcoming\s+soon\b", re.I)
SOLD_OUT = re.compile(r"\b(?:sold\s+out|out\s+of\s+stock|notify\s+me|currently\s+unavailable)\b", re.I)
RECOMMENDATION_HEADINGS = (
    "you might also like", "related products", "recommended for you",
    "similar products", "complete the look", "customers also bought",
    "more to explore", "you may also like", "others also bought",
)
AVAILABILITY_LABELS = {
    "available": "구매 가능 (Add to Cart)",
    "coming_soon": "판매 대기 (Coming Soon)",
    "sold_out": "품절/판매 중단",
}


def normalized(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()


def safe_url(raw: str) -> str:
    """Http(s) only, no URL credentials (no cookies or private data in links)."""
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"허용되지 않은 URL: {raw!r}")
    if parts.hostname.lower() in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("로컬 주소는 감시 대상에 사용할 수 없습니다")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def absolute_url(base: str, href: str) -> str:
    if not href or href.startswith(("javascript:", "data:", "mailto:")):
        return ""
    try:
        return safe_url(urljoin(base, href))
    except ValueError:
        return ""


def normalized_product_url(url: str) -> str:
    """UTM 추적 파라미터는 상품 고유 식별에 사용하지 않는다."""
    if not url:
        return ""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in
            {"fbclid", "gclid", "igshid", "ref", "source"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/",
                       urlencode(sorted(kept)), ""))


def _get_url(node, selector: str, base_url: str) -> str:
    element = node if selector == "self" else node.select_one(selector or "a[href]")
    if element is None:
        return ""
    if element.has_attr("href"):
        return absolute_url(base_url, str(element.get("href") or ""))
    anchor = element.find("a", href=True)
    return absolute_url(base_url, anchor.get("href", "")) if anchor else ""


def extract_items(html: str, monitor: dict) -> list[dict]:
    """DOM 상품 카드의 링크/고유 ID로 식별. 가격이나 카드 순서 변경은 신규로 보지 않음."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in monitor.get("exclude_selectors", []):
        for element in soup.select(selector):
            element.decompose()
    root_selector = monitor.get("item_selector", "")
    if not root_selector:
        raise ValueError("items 모드에 item_selector가 필요합니다")
    nodes = soup.select(root_selector)
    if len(nodes) > MAX_ITEMS * 10:
        raise ValueError("선택한 영역이 지나치게 넓습니다. item_selector를 좁히세요")

    out = []
    seen_in_page = set()
    for node in nodes[:int(monitor.get("max_items", MAX_ITEMS))]:
        tsel = monitor.get("title_selector", "")
        title_node = node.select_one(tsel) if tsel and tsel != "self" else node
        title = normalized(title_node.get_text(" ", strip=True)) if title_node else ""
        if not title:
            continue
        url = _get_url(node, monitor.get("link_selector", "a[href]"), monitor["url"])
        id_attr = monitor.get("id_attribute", "")
        original_id = normalized(node.get(id_attr)) if id_attr else ""
        if original_id:
            base_key = "id:" + original_id
        elif url:
            base_key = "url:" + normalized_product_url(url)
        else:
            base_key = "title:" + title.casefold()
        key = hashlib.sha256(base_key.encode("utf-8")).hexdigest()[:32]
        if key in seen_in_page:
            continue
        seen_in_page.add(key)

        psel = monitor.get("price_selector", "")
        price_node = node.select_one(psel) if psel else None
        price = normalized(price_node.get_text(" ", strip=True)) if price_node else ""
        isel = monitor.get("image_selector", "")
        image_node = node.select_one(isel) if isel else None
        image = ""
        if image_node:
            src = image_node.get("src") or image_node.get("data-src") or ""
            if not src and image_node.get("srcset"):
                src = str(image_node.get("srcset")).split(",")[0].split(" ")[0]
            image = absolute_url(monitor["url"], str(src)) if src else ""
        out.append({"key": key, "title": title[:260], "url": url,
                    "price": price[:80], "image": image})
    return out


def extract_regions(html: str, monitor: dict) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for selector in monitor.get("exclude_selectors", []):
        for element in soup.select(selector):
            element.decompose()
    results = []
    for selector in monitor.get("region_selectors", []):
        elements = soup.select(selector)
        if not elements:
            raise ValueError(f"페이지 영역을 찾지 못했습니다: {selector}")
        value = normalized(" ".join(e.get_text(" ", strip=True) for e in elements))
        if len(value) < int(monitor.get("minimum_text_length", 3)):
            raise ValueError(f"영역이 비어 있거나 너무 짧습니다: {selector}")
        results.append(value[:20000])
    if not results:
        raise ValueError("region 모드에는 region_selectors가 필요합니다")
    return results


def _visible_enough(node) -> bool:
    """명백히 숨긴 버튼/요소를 무시. 브라우저 렌더링 후 추출한 HTML에서도 사용."""
    cursor = node
    for _ in range(5):
        if cursor is None or not hasattr(cursor, "attrs"):
            break
        if cursor.has_attr("hidden") or cursor.get("aria-hidden") == "true":
            return False
        style = re.sub(r"\s+", "", str(cursor.get("style", "")).lower())
        if "display:none" in style or "visibility:hidden" in style:
            return False
        if cursor.get("data-pagewatch-visible") == "0":
            return False
        cursor = cursor.parent
    return True


def _status_in_panel(panel) -> tuple[str, str] | None:
    """상품 헤더 가까이에 위치한 CTA 위주로 판단. 본문 및 다른 상품은 제외."""
    controls = panel.select("button, [role='button'], input[type='submit'], input[type='button']")
    labels = []
    for el in controls:
        if not _visible_enough(el):
            continue
        label = normalized(el.get("value", "") if el.name == "input" else el.get_text(" ", strip=True))
        if not label:
            label = normalized(el.get("aria-label", ""))
        if label:
            labels.append(label)
    # 실제 'Coming Soon' 버튼/표시가 있으면 이를 우선한다.
    for label in labels:
        if COMING_SOON.search(label):
            return "coming_soon", f"버튼: {label[:90]}"
    # 버튼이 아니라 출고 날짜 문구가 표시되는 경우도 있다.
    # 여기서는 'Coming Soon' 텍스트가 페이지 상세 영역에 실제 표시되어야 한다.
    for el in panel.find_all(["span", "p", "div", "strong", "label"], limit=90):
        if not _visible_enough(el):
            continue
        if len(el.find_all(["span", "p", "div", "strong", "label"])) > 8:
            continue
        value = normalized(el.get_text(" ", strip=True))
        if len(value) <= 90 and COMING_SOON.search(value):
            return "coming_soon", f"표시: {value[:90]}"
    for label in labels:
        if SOLD_OUT.search(label):
            return "sold_out", f"버튼: {label[:90]}"
    for label in labels:
        if ADD_TO_CART.search(label):
            return "available", f"버튼: {label[:90]}"
    # 판매 버튼은 숨긴 템플릿/관련 상품 때문에 전체 body를 검색하지 않는다.
    for el in panel.find_all(["span", "p", "strong"], limit=90):
        if not _visible_enough(el):
            continue
        value = normalized(el.get_text(" ", strip=True))
        if len(value) <= 90 and SOLD_OUT.search(value):
            return "sold_out", f"표시: {value[:90]}"
    return None


def detect_availability(html: str, monitor: dict) -> dict:
    """개별 Vans 상품 상세에서 재고/판매 CTA를 분류. 모르면 실패로 처리한다.

    브라우저 방문 성공/상품 설명 유무만으로 '판매 중'이라고 단정하지 않는다.
    사이트가 구조를 바꾸었으면 unknown 대신 예외를 내서 과거 정상 상태를 보존한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("script, style, noscript, template, nav, header, footer"):
        el.decompose()
    h1 = soup.select_one("main h1") or soup.select_one("h1")
    if h1 is None:
        raise ValueError("상품 제목 h1을 찾지 못했습니다. 차단 또는 페이지 구조 변경 가능성")
    title = normalized(h1.get_text(" ", strip=True))
    expected_title = normalized(monitor.get("expected_title_contains", ""))
    if expected_title and expected_title.casefold() not in title.casefold():
        raise ValueError(f"다른 상품 페이지로 이동했습니다. 실제 제목: {title[:100]}")
    sku = normalized(monitor.get("expected_sku", ""))
    content = normalized(soup.get_text(" ", strip=True))
    if sku and not re.search(r"\b(?:Style|SKU|Item\s*(?:No\.?|#)|Product\s*Code)\s*:?\s*" + re.escape(sku) + r"\b", content, re.I):
        raise ValueError(f"정확한 상품코드 {sku}를 확인하지 못했습니다. 차단/다른 상품/구조 변경 가능성")

    # h1이 포함된 최소 조상부터 탐색해서 첫 번째로 발견되는 구매 UI를 사용.
    # body/html까지 올라가면 헤더 'Coming Soon'이나 추천상품 CTA가 섞일 수 있어 제외.
    panel = h1.parent
    for _ in range(10):
        if panel is None or getattr(panel, "name", "") in ("body", "html"):
            break
        # 다른 주요 상품 타이틀까지 포함하는 지나치게 큰 영역은 건너뛴다.
        has_recommendations = any(
            normalized(h.get_text(" ", strip=True)).casefold().startswith(RECOMMENDATION_HEADINGS)
            for h in panel.select("h2, h3, h4")
        )
        if len(panel.select("h1")) == 1 and not has_recommendations:
            panel_text = normalized(panel.get_text(" ", strip=True))
            if len(panel_text) <= int(monitor.get("max_panel_chars", 6000)):
                finding = _status_in_panel(panel)
                if finding:
                    status, evidence = finding
                    return {"status": status, "title": title[:200], "sku": sku,
                            "url": monitor["url"], "evidence": evidence}
        panel = panel.parent
    raise ValueError("구매 상태가 불명확합니다. Coming Soon/Add to Cart/품절 신호를 찾지 못했습니다")


def validate_monitor(m: dict) -> None:
    if not isinstance(m, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(m.get("id", ""))):
        raise ValueError("monitor id는 영문/숫자/_/- 문자 1~64자로 지정하세요")
    m["url"] = safe_url(str(m.get("url", "")))
    if m.get("mode", "items") not in ("items", "region", "availability"):
        raise ValueError(f"{m['id']}: mode는 items, region 또는 availability")
    if m.get("render", "http") not in ("http", "browser"):
        raise ValueError(f"{m['id']}: render는 http 또는 browser")
    if m.get("mode", "items") == "items" and not m.get("item_selector"):
        raise ValueError(f"{m['id']}: item_selector를 지정하세요")
    if m.get("mode") == "region" and not m.get("region_selectors"):
        raise ValueError(f"{m['id']}: region_selectors를 지정하세요")
    if m.get("mode") == "availability":
        if not re.fullmatch(r"[A-Za-z0-9]{6,30}", str(m.get("expected_sku", ""))):
            raise ValueError("availability에는 정확한 expected_sku가 필요합니다")
        if not m.get("expected_title_contains"):
            raise ValueError("availability에는 expected_title_contains가 필요합니다")
    if not 1 <= int(m.get("max_items", MAX_ITEMS)) <= MAX_ITEMS:
        raise ValueError("max_items 범위: 1~500")


def fetch_html(m: dict, browser=None) -> str:
    url = m["url"]
    if m.get("render", "http") == "http":
        resp = requests.get(url, timeout=25, headers={"User-Agent": "PageWatch/1.0 (read-only personal monitor)"})
        resp.raise_for_status()
        html = resp.text
    else:
        if browser is None:
            raise RuntimeError("browser 감시에 Playwright Chromium이 필요합니다")
        context = browser.new_context(locale="en-US", viewport={"width": 1440, "height": 900})
        try:
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
            if response is not None and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            selector = m.get("wait_selector") or (m.get("item_selector") if m.get("mode", "items") == "items"
                           else m.get("region_selectors", [""])[0])
            if selector:
                page.locator(selector).first.wait_for(state="visible", timeout=20000)
            page.wait_for_timeout(min(int(m.get("settle_ms", 1200)), 5000))
            if m.get("mode") == "availability":
                # 실제 CSS로 감춰진 버튼은 판단에 사용하지 않는다.
                page.evaluate("""() => {
                  for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]')) {
                    const css = getComputedStyle(el);
                    const visible = css.display !== 'none' && css.visibility !== 'hidden' && el.getClientRects().length > 0;
                    el.setAttribute('data-pagewatch-visible', visible ? '1' : '0');
                  }
                }""")
            html = page.content()
        finally:
            context.close()
    plain = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    if any(phrase in plain[:3000] for phrase in CAPTCHA_PHRASES):
        raise RuntimeError("접근 차단/캡차 페이지입니다. 우회하지 않고 감시를 중단합니다")
    return html


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "monitors": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION or not isinstance(state.get("monitors"), dict):
        raise ValueError("알 수 없는 상태 파일 형식 — 백업 후 수동 확인하세요")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _when() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compare(m: dict, previous: dict | None, html: str) -> tuple[dict, list[dict]]:
    """(새 상태, 전송할 신규 이벤트). 부트스트랩 시 기본적으로 전송하지 않음."""
    mode = m.get("mode", "items")
    new = dict(previous or {})
    new["mode"] = mode
    boot = previous is None or previous.get("mode") != mode or not previous.get("initialized", False)
    alert_on_first = bool(m.get("notify_on_first_run", False))
    events = []
    if mode == "items":
        items = extract_items(html, m)
        if not items and not bool(m.get("allow_empty", False)):
            raise ValueError("상품이 0개입니다. CSS 선택자/캡차/로그인 여부를 확인하세요")
        saved = dict(previous.get("seen", {}) if previous and not boot else {})
        newcomers = [item for item in items if item["key"] not in saved]
        for item in items:
            if item["key"] not in saved:
                saved[item["key"]] = {"title": item["title"], "url": item["url"]}
        if len(saved) > MAX_SEEN:
            saved = dict(list(saved.items())[-MAX_SEEN:])
        new["seen"] = saved
        # 조회할 때마다 커밋하는 일을 방지: 실제 감시 이력이 변할 때만 상태 갱신
        if boot or saved != (previous or {}).get("seen", {}):
            new["last_count"] = len(items)
            new["updated_at"] = _when()
        if not boot or alert_on_first:
            events = [{"kind": "item", **item} for item in newcomers]
    elif mode == "availability":
        result = detect_availability(html, m)
        status = result["status"]
        old_status = previous.get("status") if previous and not boot else None
        new["status"] = status
        new["title"] = result["title"]
        new["sku"] = result["sku"]
        if boot or status != old_status:
            new["updated_at"] = _when()
        # 첫 실행이 이미 구매 가능하면 알림(옵션). Coming Soon에는 보내지 않는다.
        if status == "available" and (old_status != "available") and (not boot or alert_on_first):
            events = [{"kind": "availability", "status": status, "previous_status": old_status,
                       "title": result["title"], "sku": result["sku"], "url": result["url"],
                       "evidence": result["evidence"]}]
        elif status != "available" and bool(m.get("notify_on_unavailable", False)) and \
                old_status != status and (not boot or alert_on_first):
            events = [{"kind": "availability", "status": status, "previous_status": old_status,
                       "title": result["title"], "sku": result["sku"], "url": result["url"],
                       "evidence": result["evidence"]}]
    else:
        regions = extract_regions(html, m)
        old_regions = previous.get("regions", []) if previous and not boot else []
        new["regions"] = regions
        if boot or regions != old_regions:
            new["updated_at"] = _when()
        if not boot or alert_on_first:
            for index, after in enumerate(regions):
                before = old_regions[index] if index < len(old_regions) else ""
                if before != after:
                    events.append({"kind": "region", "selector": m["region_selectors"][index],
                                   "before": before[:350], "after": after[:550]})
    new["initialized"] = True
    return new, events


def _discord_url() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in ("discord.com", "discordapp.com") or not re.fullmatch(
        r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+/?", p.path
    ) or p.query or p.fragment:
        raise ValueError("DISCORD_WEBHOOK_URL에 정상적인 Discord 웹훅 URL을 GitHub Secret으로 설정하세요")
    return url


def send_discord(m: dict, events: list[dict]) -> None:
    if not events:
        return
    url = _discord_url()
    mention_id = os.getenv("DISCORD_USER_ID", "").strip()
    mentions = {"parse": []}
    mention = ""
    if mention_id:
        if not re.fullmatch(r"\d{15,22}", mention_id):
            raise ValueError("DISCORD_USER_ID는 숫자로 된 Discord 사용자 ID만 지원합니다")
        mentions = {"parse": [], "users": [mention_id]}
        mention = f"<@{mention_id}> "
    limit = min(15, max(1, int(m.get("notify_limit", 8))))
    for start in range(0, len(events), limit):
        batch = events[start:start + limit]
        descriptions = []
        for e in batch:
            if e["kind"] == "item":
                title = e["title"].replace("`", "′").replace("[", "［").replace("]", "］")[:170]
                product_url = e.get("url", "")
                desc = f"• **{title}**"
                if product_url:
                    desc += f" — {product_url}"
                if e.get("price"):
                    desc += f"\n  가격: {e['price']}"
            elif e["kind"] == "availability":
                status_text = AVAILABILITY_LABELS.get(e["status"], e["status"])
                prev = AVAILABILITY_LABELS.get(e.get("previous_status"), "최초 확인")
                title = e["title"].replace("`", "′").replace("[", "［").replace("]", "］")[:160]
                desc = (f"• **{title}**\n  상품코드: `{e['sku']}`\n"
                        f"  상태 변화: {prev} → **{status_text}**\n"
                        f"  근거: {e['evidence']}\n  {e['url']}")
            else:
                desc = f"• **{e['selector']}** 영역 변경\n  변경 전: {e['before'][:250]}\n  변경 후: {e['after'][:400]}"
            descriptions.append(desc[:850])
        remainder = len(events) - start - len(batch)
        if remainder and remainder < 30 and start + limit >= len(events):
            descriptions.append(f"추가 감지: {remainder}건")
        payload = {
            "content": (mention + f"🔔 {m.get('name', m['id'])}: 상태 변경/신규 물품 {len(batch)}건 감지")[:1900],
            "embeds": [{"title": f"{m.get('name', m['id'])} 변경 알림"[:240],
                        "description": "\n\n".join(descriptions)[:3900],
                        "url": m["url"], "color": 0x3698DB}],
            "allowed_mentions": mentions,
        }
        for retry in range(4):
            r = requests.post(url, json=payload, timeout=20, allow_redirects=False)
            if r.status_code in (200, 204):
                break
            if r.status_code == 429 and retry < 3:
                try:
                    wait = min(15, max(1.0, float(r.json().get("retry_after", 2))))
                except (ValueError, AttributeError, TypeError):
                    wait = 2
                time.sleep(wait)
                continue
            if r.status_code >= 500 and retry < 3:
                time.sleep(retry + 1)
                continue
            raise RuntimeError(f"Discord 전송 실패: HTTP {r.status_code}")
        else:
            raise RuntimeError("Discord 요청 한도 초과")


def run(config_path: Path, state_path: Path, *, dry_run: bool = False) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    monitors = config.get("monitors", [])
    if not isinstance(monitors, list):
        raise ValueError("config의 monitors는 배열이어야 합니다")
    active = [m for m in monitors if m.get("enabled", False)]
    if not active:
        print("[설정 필요] enabled=true인 감시 대상이 없습니다. config/monitors.json을 편집하세요.")
        return 2
    ids = [m.get("id") for m in active]
    if len(set(ids)) != len(ids):
        raise ValueError("같은 monitor id를 두 번 사용할 수 없습니다")
    for m in active:
        validate_monitor(m)
    state = load_state(state_path)
    errors = 0
    counts = {"events": 0, "pages": 0}

    def check_all(browser=None):
        nonlocal errors
        for m in active:
            try:
                html = fetch_html(m, browser)
                previous = state["monitors"].get(m["id"])
                current, events = compare(m, previous, html)
                if events and not dry_run:
                    send_discord(m, events)
                if not dry_run and current != previous:
                    state["monitors"][m["id"]] = current
                    save_state(state_path, state)
                counts["pages"] += 1
                counts["events"] += len(events)
                summary = (f"판매 상태={current.get('status')}" if m.get("mode") == "availability"
                           else f"{current.get('last_count',len(current.get('regions', [])))}개 확인")
                print(f"[OK] {m['id']}: {summary} / 새 알림 {len(events)}건"
                      + (" (dry run, 미전송)" if dry_run else ""))
            except Exception as exc:
                errors += 1
                # 특히 웹훅 토큰은 네트워크 예외 메시지에 섞일 수 있으므로 로그에서 제거.
                msg = str(exc)
                webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
                if webhook:
                    msg = msg.replace(webhook, "<redacted-webhook>")
                    path = urlsplit(webhook).path
                    if path:
                        msg = msg.replace(path, "<redacted-webhook-path>")
                msg = re.sub(r"(/api(?:/v\d+)?/webhooks/\d+/)[A-Za-z0-9_-]+", r"\1<redacted>", msg)
                print(f"[ERROR] {m.get('id', '?')}: {type(exc).__name__}: {msg[:180]}", file=sys.stderr)

    if any(m.get("render", "http") == "browser" for m in active):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    check_all(browser)
                finally:
                    browser.close()
        except Exception as exc:
            print(f"[ERROR] 브라우저 시작 실패: {type(exc).__name__}: {str(exc)[:180]}", file=sys.stderr)
            return 1
    else:
        check_all()
    print(f"총 {counts['pages']}개 페이지 확인, {counts['events']}건 감지, 실패 {errors}개")
    return 1 if errors else 0



def git_checkpoint(state_path: Path) -> bool:
    """Commit and push monitor state immediately after a change, across workflow runs.

    Designed for a GitHub Actions checkout with contents:write and
    persist-credentials:true. A failed push raises so next loop retries.
    """
    if not Path(".git").exists():
        raise RuntimeError("git state sync는 GitHub Actions checkout에서만 사용할 수 있습니다")
    try:
        repo_path = str(state_path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError as exc:
        raise ValueError("state 파일은 저장소 내부에 있어야 합니다") from exc
    branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not branch or not re.fullmatch(r"[A-Za-z0-9._/-]{1,180}", branch) or branch.startswith("-"):
        raise ValueError("GITHUB_REF_NAME에 유효한 브랜치 이름이 필요합니다")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], text=True, capture_output=True, check=True,
                              timeout=45)

    # Commit only actual state changes, but also retry previously failed pushes.
    changed = git("status", "--porcelain", "--", repo_path).stdout.strip()
    if changed:
        git("add", "--", repo_path)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", repo_path])
        if staged.returncode not in (0, 1):
            raise RuntimeError("git diff 실패")
        if staged.returncode == 1:
            git("-c", "user.name=github-actions[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-m", "chore: save monitor state [skip ci]", "--", repo_path)

    # git pull and push only when there are local commits to upload.
    ahead = git("rev-list", "--count", "HEAD", "--not", f"origin/{branch}").stdout.strip()
    if int(ahead or "0") == 0:
        return bool(changed)
    git("pull", "--rebase", "origin", branch)
    git("push", "origin", f"HEAD:refs/heads/{branch}")
    print("[STATE] 감시 상태를 GitHub 저장소에 저장했습니다.", flush=True)
    return True


def run_loop(
    config_path: Path,
    state_path: Path,
    *,
    interval_seconds: int,
    duration_seconds: int,
    git_sync: bool = False,
    dry_run: bool = False,
    check=None,
    checkpoint=None,
    clock=None,
    sleep=None,
) -> int:
    """One long-running job; checks every ~interval_seconds until its deadline.

    Unlike the reference KCA in-memory baseline, this stores state on disk and
    optionally pushes changed state, retaining alerts across job replacements.
    Test seams permit simulated time and requests without hitting external sites.
    """
    if interval_seconds < 1 or duration_seconds < 1:
        raise ValueError("감시 주기/실행 시간은 양수여야 합니다")
    if git_sync and dry_run:
        raise ValueError("--dry-run 및 --git-sync는 함께 사용할 수 없습니다")
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    check = check or (lambda: run(config_path, state_path, dry_run=dry_run))
    checkpoint = checkpoint or (lambda: git_checkpoint(state_path))
    started = clock()
    deadline = started + duration_seconds
    iteration = failures = successes = 0
    print(f"[LOOP] 감시 시작: {interval_seconds}초 간격, 최대 {duration_seconds}초, "
          f"Git 동기화={'사용' if git_sync else '미사용'}", flush=True)
    while clock() < deadline:
        iteration += 1
        tick_start = clock()
        try:
            status = check()
            if status == 0:
                successes += 1
            else:
                failures += 1
                print(f"[LOOP] #{iteration} 일부 감시 오류(코드 {status}) — 다음 주기 재시도", flush=True)
        except Exception as exc:
            failures += 1
            print(f"[LOOP] #{iteration} 감시 오류: {type(exc).__name__}: {str(exc)[:180]}",
                  file=sys.stderr, flush=True)
        # Push state even after partial monitor errors: other monitors may have succeeded.
        if git_sync:
            try:
                checkpoint()
            except Exception as exc:
                # subprocess output intentionally omitted; avoid logging access tokens.
                print(f"[STATE] 저장소 동기화 실패 ({type(exc).__name__}). 다음 주기에 재시도합니다.",
                      file=sys.stderr, flush=True)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        next_tick = tick_start + interval_seconds
        pause = min(remaining, max(0.0, next_tick - clock()))
        if pause > 0:
            sleep(pause)
    print(f"[LOOP] 종료: 확인 {iteration}회, 성공 {successes}회, 오류 {failures}회", flush=True)
    return 0 if successes else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="개인용 쪽감시자 — 새 상품/웹페이지 변경 Discord 알림")
    parser.add_argument("--config", default="config/monitors.json")
    parser.add_argument("--state", default="state/monitor_state.json")
    parser.add_argument("--dry-run", action="store_true", help="전송/저장하지 않고 확인만 함")
    parser.add_argument("--test-webhook", action="store_true", help="Discord 웹훅 연결 점검")
    parser.add_argument("--loop", action="store_true", help="GitHub Actions 실행 중 반복 감시")
    parser.add_argument("--git-sync", action="store_true", help="상태 변경 즉시 GitHub에 커밋/푸시(Actions 전용)")
    parser.add_argument("--interval-seconds", type=int,
                        default=int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
    parser.add_argument("--duration-seconds", type=int,
                        default=int(os.getenv("RUN_DURATION_SECONDS", "21480")))
    args = parser.parse_args()
    if args.test_webhook:
        _discord_url()
        m = {"id": "test", "name": "쪽감시자 연결 테스트", "url": "https://example.com", "notify_limit": 1}
        send_discord(m, [{"kind": "region", "selector": "테스트", "before": "", "after": "알림 수신 성공"}])
        print("Discord 연결 테스트를 전송했습니다.")
        return 0
    if args.loop:
        return run_loop(Path(args.config), Path(args.state),
                        interval_seconds=args.interval_seconds,
                        duration_seconds=args.duration_seconds,
                        git_sync=args.git_sync, dry_run=args.dry_run)
    return run(Path(args.config), Path(args.state), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
