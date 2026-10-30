"""Local DOM regressions using an isolated, headless Edge test process.

Every document is a data: fixture. These tests never load the school website,
read config.json, reuse a user browser profile, or submit a reservation.
"""

import os
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service

import sport as login


FIXTURE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>
html, body { margin:0; font-family:sans-serif; }
.before { height:1500px; background:linear-gradient(#eef,#fff); }
.card { display:block; width:440px; margin-left:160px; }
.desc { display:block; height:48px; line-height:48px; background:#def; }
.after { height:1000px; }
.uni-tabbar-border { position:fixed; left:0; bottom:0; width:100%; height:90px;
  z-index:10000; background:#ddd; border-top:1px solid #888; }
.modal { position:fixed; inset:0; z-index:20000; background:rgba(0,0,0,.5); }
</style>
<div class="before">本地测试页</div>
<uni-view class="card"><uni-view id="entry" class="desc">预约专区</uni-view></uni-view>
<div class="after"></div>
<div class="uni-tabbar-border">固定底栏</div>
<script>
window.entryClicks=0; window.trustedClicks=[];
document.querySelector('#entry').addEventListener('click', event => {
  window.entryClicks++; window.trustedClicks.push(event.isTrusted);
});
</script></html>"""


def cached_driver():
    root = Path(os.environ.get("USERPROFILE", "")) / ".cache" / "selenium" / "msedgedriver" / "win64"
    matches = list(root.glob("*/msedgedriver.exe"))
    if not matches:
        raise unittest.SkipTest("No cached Edge driver; no automatic downloads in local-only tests")

    def version(path):
        return tuple(int(part) for part in path.parent.name.split("."))

    return max(matches, key=version)


class LocalDomClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1280,960")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        cls.driver = webdriver.Edge(service=Service(str(cached_driver())), options=options)
        cls.driver.set_page_load_timeout(5)
        cls.driver.implicitly_wait(0)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def setUp(self):
        self.driver.get("data:text/html;charset=utf-8," + quote(FIXTURE))
        self.messages = []
        self.stop_event = threading.Event()
        self.nav = login.LoginNavigator(self.driver, log=self.messages.append,
                                        stopped=self.stop_event.is_set)
        self.nav.stage = "打开体育场馆入口"

    def entry_clicks(self):
        return self.driver.execute_script("return window.entryClicks")

    def add_modal(self, remove_after_ms=None):
        self.driver.execute_script("""
            const modal = document.createElement('div');
            modal.id='modal'; modal.className='modal'; modal.textContent='需要人工确认';
            document.body.appendChild(modal);
            if (arguments[0] !== null) setTimeout(() => modal.remove(), arguments[0]);
        """, remove_after_ms)

    def test_fixture_reproduces_default_native_click_interception(self):
        entry = self.driver.find_element(By.ID, "entry")
        with self.assertRaises(ElementClickInterceptedException) as caught:
            entry.click()
        self.assertIn("uni-tabbar-border", str(caught.exception))
        self.assertEqual(self.entry_clicks(), 0)

    def test_project_click_scrolls_entry_above_fixed_bottom_bar(self):
        self.assertTrue(self.nav._click_text("预约专区"))
        self.assertEqual(self.entry_clicks(), 1)
        self.assertEqual(self.driver.execute_script("return window.trustedClicks"), [True])
        rect = self.driver.execute_script("""
            const r=document.querySelector('#entry').getBoundingClientRect();
            const b=document.querySelector('.uni-tabbar-border').getBoundingClientRect();
            return {center:r.top+r.height/2, barTop:b.top};
        """)
        self.assertLess(rect["center"], rect["barTop"])

    def test_visible_modal_is_not_bypassed_with_javascript(self):
        self.add_modal()
        started = time.monotonic()
        with self.assertRaises(login.LoginFlowError):
            self.nav._click_text("预约专区")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.entry_clicks(), 0)
        self.assertTrue(self.driver.find_element(By.ID, "modal").is_displayed())

    def test_empty_modal_cannot_be_bypassed_by_clicking_text_ancestor(self):
        # With no unrelated visible text, body/html also have the exact label.
        # They contain the modal but must never become fallback click targets.
        self.driver.execute_script("""
            document.querySelector('.before').textContent='';
            document.querySelector('.uni-tabbar-border').textContent='';
        """)
        self.add_modal()
        self.driver.execute_script("""
            const modal=document.querySelector('#modal');
            modal.textContent=''; window.modalClicks=0;
            modal.addEventListener('click', () => window.modalClicks++);
        """)
        with self.assertRaises(login.LoginFlowError):
            self.nav._click_text("预约专区")
        self.assertEqual(self.entry_clicks(), 0)
        self.assertEqual(self.driver.execute_script("return window.modalClicks"), 0)

    def test_stop_before_click_does_not_scroll_or_dispatch(self):
        self.stop_event.set()
        started = time.monotonic()
        with self.assertRaises(login.LoginCancelled):
            self.nav._click_text("预约专区")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.entry_clicks(), 0)
        self.assertEqual(self.driver.execute_script("return window.scrollY"), 0)

    def test_stop_during_interception_cancels_bounded_retry(self):
        self.add_modal()
        timer = threading.Timer(0.2, self.stop_event.set)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(login.LoginCancelled):
                self.nav._click_text("预约专区")
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(self.entry_clicks(), 0)

    def test_successful_click_is_dispatched_once(self):
        self.driver.execute_script("document.querySelector('.uni-tabbar-border').remove()")
        self.assertTrue(self.nav._click_text("预约专区"))
        self.assertEqual(self.entry_clicks(), 1)
        self.assertEqual(self.driver.execute_script("return window.trustedClicks"), [True])

    def test_temporary_modal_can_clear_without_duplicate_clicks(self):
        self.add_modal(remove_after_ms=500)
        self.assertTrue(self.nav._click_text("预约专区"))
        self.assertEqual(self.entry_clicks(), 1)
        self.assertEqual(self.driver.execute_script("return window.trustedClicks"), [True])

    def test_partly_covered_fixed_entry_uses_a_visible_pointer_point(self):
        # This entry cannot be scrolled away from the bottom bar. Its center is
        # covered, but the upper quarter is visible and accepts a real pointer.
        self.driver.execute_script("""
            const entry=document.querySelector('#entry');
            Object.assign(entry.style, {position:'fixed', left:'160px', bottom:'40px',
                height:'80px', lineHeight:'80px', width:'440px'});
        """)
        with self.assertRaises(ElementClickInterceptedException):
            self.driver.find_element(By.ID, "entry").click()
        self.assertEqual(self.entry_clicks(), 0)
        self.assertTrue(self.nav._click_text("预约专区"))
        self.assertFalse(self.nav._last_click["center"])
        self.assertEqual(self.entry_clicks(), 1)
        self.assertEqual(self.driver.execute_script("return window.trustedClicks"), [True])


if __name__ == "__main__":
    unittest.main()
