import copy
from pathlib import Path
import json

import pytest

import monitor

SAMPLE = (Path(__file__).resolve().parent.parent / "examples" / "sample_catalog.html").read_text(encoding="utf-8")
CONFIG = {
    "id": "test",
    "name": "테스트 샵",
    "url": "https://example.com/catalog",
    "mode": "items",
    "render": "http",
    "item_selector": ".product-card",
    "title_selector": ".product-name",
    "link_selector": "a",
    "price_selector": ".price",
    "image_selector": "img",
    "id_attribute": "data-product-id",
}


def test_extract_items_links_and_price():
    items = monitor.extract_items(SAMPLE, CONFIG)
    assert len(items) == 2
    assert items[0]["title"] == "샘플 운동화 A"
    assert items[0]["url"] == "https://example.com/products/1001"
    assert items[0]["price"] == "129,000원"
    assert items[0]["image"] == "https://example.com/images/item1.png"


def test_first_run_baseline_without_alert():
    state, events = monitor.compare(CONFIG, None, SAMPLE)
    assert not events
    assert len(state["seen"]) == 2
    assert state["initialized"]


def test_new_product_triggers_once_even_when_reordered():
    baseline, _ = monitor.compare(CONFIG, None, SAMPLE)
    # 한 제품이 새로 등장
    expanded = SAMPLE.replace('</section>', '''
      <article class="product-card" data-product-id="S003"><a href="/products/1003">
      <span class="product-name">한정판 C</span></a><span class="price">30,000원</span></article>
      </section>''')
    updated, events = monitor.compare(CONFIG, baseline, expanded)
    assert len(events) == 1
    assert events[0]["title"] == "한정판 C"
    _, again = monitor.compare(CONFIG, updated, expanded)
    assert not again
    # 상품 순서만 바뀐 경우 알림 없음
    reversed_html = SAMPLE.replace('S001', 'TEMP').replace('S002', 'S001').replace('TEMP', 'S002')
    _, reorder_events = monitor.compare(CONFIG, updated, reversed_html)
    assert not reorder_events


def test_price_change_not_counted_as_new_item():
    state, _ = monitor.compare(CONFIG, None, SAMPLE)
    _, alerts = monitor.compare(CONFIG, state, SAMPLE.replace("129,000원", "99,000원"))
    assert alerts == []


def test_region_change_only_once():
    m = {"id": "r", "mode": "region", "url": "https://example.com",
         "region_selectors": ["#new-products"], "minimum_text_length": 5}
    a, first = monitor.compare(m, None, SAMPLE)
    assert not first
    changed = SAMPLE.replace("샘플 자켓 B", "샘플 자켓 C")
    b, events = monitor.compare(m, a, changed)
    assert len(events) == 1 and events[0]["kind"] == "region"
    _, repeat = monitor.compare(m, b, changed)
    assert not repeat


def test_missing_region_keeps_baseline():
    m = {"mode": "region", "url": "https://example.com", "region_selectors": [".missing"]}
    with pytest.raises(ValueError, match="찾지 못했습니다"):
        monitor.compare(m, None, SAMPLE)


def test_zero_items_is_error_by_default():
    with pytest.raises(ValueError, match="0개"):
        monitor.compare(CONFIG, None, "<html><p>nothing</p></html>")


def test_tracking_queries_do_not_create_new_item():
    html = '''<div class="product-card"><a href="/products/20?utm_source=ref&gclid=123">
              <span class="product-name">가방</span></a></div>'''
    html2 = html.replace('utm_source=ref&gclid=123', 'utm_source=xyz&gclid=456')
    assert monitor.extract_items(html, CONFIG)[0]["key"] == monitor.extract_items(html2, CONFIG)[0]["key"]


def test_validate_rejects_bad_url_and_id():
    with pytest.raises(ValueError):
        monitor.validate_monitor({**CONFIG, "id": "bad/id"})
    with pytest.raises(ValueError):
        monitor.validate_monitor({**CONFIG, "url": "file:///etc/passwd"})


def test_run_with_offline_monkeypatch_persists_state(tmp_path, monkeypatch):
    config_path = tmp_path / 'monitors.json'
    state_path = tmp_path / 'state.json'
    config_path.write_text(json.dumps({"monitors": [{**CONFIG, "enabled": True}]}), encoding='utf-8')
    monkeypatch.setattr(monitor, "fetch_html", lambda *_: SAMPLE)
    assert monitor.run(config_path, state_path) == 0
    saved = monitor.load_state(state_path)
    assert len(saved["monitors"]["test"]["seen"]) == 2
    assert monitor.run(config_path, state_path) == 0


def test_no_alert_if_bootstrap_missing_discord(monkeypatch):
    monkeypatch.delenv('DISCORD_WEBHOOK_URL', raising=False)
    state, events = monitor.compare(CONFIG, None, SAMPLE)
    assert not events
    assert state["initialized"]


def test_own_item_anchor_is_link():
    m = {**CONFIG, "item_selector": "a.product-card", "title_selector": "self",
         "link_selector": "self", "id_attribute": ""}
    html = '<a class="product-card" href="/shoe/123">신상품 신발</a>'
    assert monitor.extract_items(html, m)[0]["url"] == "https://example.com/shoe/123"


def test_no_new_items_means_no_state_rewrite(tmp_path, monkeypatch):
    config_path = tmp_path / 'cfg.json'
    state_path = tmp_path / 'state.json'
    config_path.write_text(json.dumps({"monitors": [{**CONFIG, "enabled": True}]}), encoding='utf-8')
    monkeypatch.setattr(monitor, 'fetch_html', lambda *_: SAMPLE)
    assert monitor.run(config_path, state_path) == 0
    old_bytes = state_path.read_bytes()
    assert monitor.run(config_path, state_path) == 0
    assert state_path.read_bytes() == old_bytes


def test_discord_url_validation_does_not_allow_offsite(monkeypatch):
    monkeypatch.setenv('DISCORD_WEBHOOK_URL', 'https://scammer.example/api/webhooks/123/fake')
    with pytest.raises(ValueError, match='Discord 웹훅'):
        monitor._discord_url()
