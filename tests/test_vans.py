import copy
from pathlib import Path
import json

import pytest

import monitor

BASE = {
    "id": "vans-release-test",
    "enabled": True,
    "name": "Vans Satoshi shoes",
    "url": "https://www.vans.com/en-us/p/shoes/icons/era-5320/otw-era-95-dst-VN00114ESN0",
    "mode": "availability",
    "render": "browser",
    "expected_sku": "VN00114ESN0",
    "expected_title_contains": "Satoshi Nakamoto Era 95 Gems",
    "notify_on_first_run": True,
}


def page(status_button, *, sku="VN00114ESN0", title="OTW by Vans x Satoshi Nakamoto Era 95 Gems", extra=""):
    return f'''<html><header><a href="/en-us/coming-soon">Coming Soon</a></header><main>
    <section class="product-detail"><div class="product-summary"><div><h1>{title}</h1>
    <div class="product-price">$195</div></div>
    <section><div>Color Satoshi STV Navy</div>{status_button}</section></div>
    <div class="description"><h2>Details</h2><p>Style {sku}</p></div></section>
    {extra}
    </main></html>'''

COMING = page('<div><p>Coming Soon - 10/16</p><button disabled>Coming Soon</button></div>')
ACTIVE = page('<div>Size Select a Size</div><button disabled>Add to Cart</button>')
SOLD_OUT = page('<div><button>Sold Out</button></div>')


def test_coming_soon_not_confused_by_related_add_to_cart():
    html = page('<button>Coming Soon</button>', extra='<aside><h2>You Might Also Like</h2><button>Add to Cart</button></aside>')
    assert monitor.detect_availability(html, BASE)['status'] == 'coming_soon'


def test_only_recommendation_has_buy_button_no_alert():
    html = page('<p>Check back later</p>', extra='<aside><h2>You Might Also Like</h2><button>Add to Cart</button></aside>')
    with pytest.raises(ValueError, match='불명확'):
        monitor.detect_availability(html, BASE)


def test_available_ignores_global_coming_soon_menu():
    status = monitor.detect_availability(ACTIVE, BASE)
    assert status['status'] == 'available'
    assert status['sku'] == 'VN00114ESN0'


def test_sold_out_recognized():
    assert monitor.detect_availability(SOLD_OUT, BASE)['status'] == 'sold_out'


def test_no_buy_control_means_error_not_false_positive():
    html = page('<p>Available soon</p>')
    with pytest.raises(ValueError, match='불명확'):
        monitor.detect_availability(html, BASE)


def test_mismatch_sku_prevents_spurious_alarm():
    with pytest.raises(ValueError, match='상품코드'):
        monitor.detect_availability(page('<button>Add to Cart</button>', sku='VN99999999'), BASE)


def test_different_product_redirect_prevents_spurious_alarm():
    with pytest.raises(ValueError, match='다른 상품'):
        monitor.detect_availability(page('<button>Add to Cart</button>', title='Men Shoes'), BASE)


def test_first_run_coming_soon_and_live_transition_once():
    before, events = monitor.compare(BASE, None, COMING)
    assert before['status'] == 'coming_soon' and not events
    after, events = monitor.compare(BASE, before, ACTIVE)
    assert after['status'] == 'available'
    assert len(events) == 1 and events[0]['kind'] == 'availability'
    assert events[0]['previous_status'] == 'coming_soon'
    again, events = monitor.compare(BASE, after, ACTIVE)
    assert events == []
    assert again == after


def test_first_run_active_can_alert_and_default_can_suppress():
    state, events = monitor.compare(BASE, None, ACTIVE)
    assert state['status'] == 'available' and len(events) == 1
    noalert = dict(BASE, notify_on_first_run=False)
    state, events = monitor.compare(noalert, None, ACTIVE)
    assert events == []


def test_sold_out_to_available_restock_alert():
    before, _ = monitor.compare(BASE, None, SOLD_OUT)
    after, events = monitor.compare(BASE, before, ACTIVE)
    assert len(events) == 1
    assert events[0]['previous_status'] == 'sold_out'


def test_hidden_button_not_interpreted_as_purchase():
    html = page('<button data-pagewatch-visible="0">Add to Cart</button><button>Coming Soon</button>')
    assert monitor.detect_availability(html, BASE)['status'] == 'coming_soon'


def test_current_page_has_correct_mode_and_only_target_enabled():
    path = Path(__file__).resolve().parent.parent / 'config' / 'monitors.json'
    cfg = json.loads(path.read_text(encoding='utf-8'))
    targets = [m for m in cfg['monitors'] if m['enabled']]
    assert len(targets) == 1
    assert targets[0]['expected_sku'] == 'VN00114ESN0'
    for m in cfg['monitors']:
        monitor.validate_monitor(m)


def test_offline_integration_state_saved_and_notification_mocked(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    statefile = tmp_path / 'monitor_state.json'
    config.write_text(json.dumps({'monitors': [{**BASE, 'render': 'http'}]}), encoding='utf-8')
    messages = []
    pages = iter([COMING, ACTIVE, ACTIVE])
    monkeypatch.setattr(monitor, 'fetch_html', lambda *a: next(pages))
    monkeypatch.setattr(monitor, 'send_discord', lambda m, events: messages.extend(events))
    for _ in range(3):
        assert monitor.run(config, statefile) == 0
    assert len(messages) == 1
    assert monitor.load_state(statefile)['monitors'][BASE['id']]['status'] == 'available'
