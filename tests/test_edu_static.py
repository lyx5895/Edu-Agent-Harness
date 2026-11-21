"""The page script must at least parse: a syntax error leaves teachers with an empty page and no other test notices.

Compiles edu_app/static/app.js with ``new Function`` in headless Chromium (no code runs). Skipped where Playwright
or its browser is not installed.
"""

from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / 'edu_app' / 'static' / 'app.js'


def test_app_js_parses():
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=['--no-sandbox'])
        except Exception as e:  # no browser installed here
            pytest.skip(f'Chromium not available: {e}')
        try:
            page = browser.new_page()
            error = page.evaluate(
                '(src) => { try { new Function(src); return null; } catch (e) { return String(e); } }',
                APP_JS.read_text(encoding='utf-8'),
            )
        finally:
            browser.close()
    assert error is None, f'app.js does not parse: {error}'
