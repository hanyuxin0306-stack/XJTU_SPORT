"""Offline regression tests for login navigation; no credentials or network used."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sport as login


PORTAL_URL = "https://superapp.xjtu.edu.cn/pages/tab/index/index"
SPORTS_URL = "http://202.117.17.144:8080/web/index.html"


def page_state(url=PORTAL_URL, text="预约专区", handle="main", frames=(), **extra):
    state = {
        "handle": handle,
        "frames": frames,
        "url": url,
        "title": "test page",
        "ready": "complete",
        "text": text,
        "login_visible": False,
        "password_visible": False,
        "sms_visible": False,
        "errors": [],
        "challenge": False,
        "product_links": False,
    }
    state.update(extra)
    return login.classify_state(state)


class FakeSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise RuntimeError("unknown window")
        self.driver.current_window_handle = handle
        self.driver.frames = ()

    def default_content(self):
        self.driver.frames = ()

    def frame(self, frame):
        self.driver.frames += (getattr(frame, "index", frame),)


class FakeDriver:
    """Only models window ownership; it cannot send HTTP requests."""

    def __init__(self):
        self.window_handles = ["main"]
        self.current_window_handle = "main"
        self.current_url = PORTAL_URL
        self.frames = ()
        self.switch_to = FakeSwitch(self)
        self.gotos = []

    def get(self, url):
        self.gotos.append(url)
        self.current_url = url

    def set_page_load_timeout(self, seconds):
        self.page_load_timeout = seconds


class ScenarioNavigator(login.LoginNavigator):
    """Drive real navigation policy with a deterministic fake website."""

    def __init__(self, mode="same_tab", signed_in=False, **options):
        self.fake_driver = FakeDriver()
        self.messages = []
        self.mode = mode
        self.location = "portal_home" if signed_in else "portal_login"
        self.clicks = []
        self.submissions = []
        self.entered = []
        self.pause_count = 0
        super().__init__(
            self.fake_driver,
            log=self.messages.append,
            stopped=options.pop("stopped", lambda: False),
            **options,
        )

    def _scan(self):
        if self.location == "portal_login":
            return [page_state(text="账号密码登录", login_visible=True, password_visible=True)]
        if self.location == "login_error":
            return [page_state(text="账号密码登录 密码错误", login_visible=True,
                               password_visible=True, errors=["密码错误"])]
        if self.location == "human_challenge":
            return [page_state(text="请完成安全验证", login_visible=True,
                               password_visible=True, challenge=True)]
        if self.location == "portal_home":
            return [page_state(text="首页 通知 我的 预约专区")]
        if self.location == "reservation_zone":
            return [page_state(text="体育场馆预约")]
        if self.location == "sports_home":
            if self.mode == "new_tab":
                return [page_state(text="预约专区", handle="main"),
                        page_state(SPORTS_URL, "所有场地 场地13", handle="sports")]
            if self.mode == "iframe":
                return [page_state(text="体育场馆预约"),
                        page_state(SPORTS_URL, "所有场地 场地13", frames=(0,))]
            return [page_state(SPORTS_URL, "所有场地 场地13")]
        raise AssertionError(f"unexpected fake website location: {self.location}")

    def _enter(self, state):
        self.entered.append((state["handle"], state["frames"]))
        self.fake_driver.switch_to.window(state["handle"])
        self.fake_driver.frames = state["frames"]

    def _click_text(self, text, states=None):
        if self.location == "portal_home" and text == "预约专区":
            self.clicks.append(text)
            self.location = "reservation_zone"
            return True
        if self.location == "reservation_zone" and text == "体育场馆预约":
            self.clicks.append(text)
            self.location = "sports_home"
            if self.mode == "new_tab":
                self.fake_driver.window_handles.append("sports")
            return True
        return False

    def _fill_and_submit(self, state, uid, pwd):
        self.submissions.append((uid, pwd))
        self.location = "portal_home"
        return True

    def _pause(self, seconds=0.2):
        self.pause_count += 1
        if self.stopped():
            raise login.LoginCancelled()


class ClassificationTests(unittest.TestCase):
    def test_portal_navigation_is_not_sports_home(self):
        state = page_state(text="首页 订单 通知 我的 预约专区")
        self.assertTrue(state["portal_home"])
        self.assertFalse(state["venue_home"])

    def test_known_sports_origin_with_courts_is_home(self):
        self.assertTrue(page_state(SPORTS_URL, "所有场地 场地13")["venue_home"])

    def test_similar_hostname_cannot_impersonate_sports_origin(self):
        state = page_state("http://202.117.17.144.attacker.invalid:8080/web/index.html", "所有场地")
        self.assertFalse(state["venue_home"])

    def test_wrong_service_port_is_not_booking_service(self):
        state = page_state("http://202.117.17.144:8071/web/index.html", "所有场地")
        self.assertFalse(state["venue_home"])

    def test_login_form_overrides_old_home_navigation(self):
        state = page_state(SPORTS_URL, "所有场地 首页 订单 我的", login_visible=True)
        self.assertFalse(state["venue_home"])

    def test_challenge_prevents_ready_state(self):
        for url, text in [(SPORTS_URL, "所有场地"), (PORTAL_URL, "预约专区")]:
            with self.subTest(url=url):
                state = page_state(url, text, challenge=True)
                self.assertFalse(state["venue_home"])
                self.assertFalse(state["portal_home"])

    def test_invalid_port_is_not_a_home_or_an_exception(self):
        state = page_state("http://202.117.17.144:not-a-port/", "所有场地")
        self.assertFalse(state["venue_home"])

    def test_visible_product_links_can_identify_a_renamed_sports_home(self):
        state = page_state(SPORTS_URL, "欢迎使用", product_links=True)
        self.assertTrue(state["venue_home"])


class NavigationTests(unittest.TestCase):
    def test_same_tab_navigation_succeeds_without_popup(self):
        nav = ScenarioNavigator()
        nav.prepare("test-user", "test-password")
        self.assertEqual(nav.fake_driver.window_handles, ["main"])
        self.assertEqual(nav.submissions, [("test-user", "test-password")])
        self.assertEqual(nav.clicks, ["预约专区", "体育场馆预约"])
        self.assertEqual(nav.entered[-1], ("main", ()))

    def test_new_window_selects_the_actual_sports_window(self):
        nav = ScenarioNavigator(mode="new_tab")
        nav.prepare("test-user", "test-password")
        self.assertEqual(nav.fake_driver.current_window_handle, "sports")
        self.assertEqual(nav.entered[-1], ("sports", ()))
        self.assertEqual(nav.submissions, [("test-user", "test-password")])

    def test_home_inside_frame_is_recognised_and_context_is_restored(self):
        nav = ScenarioNavigator(mode="iframe", signed_in=True)
        nav.prepare("unused-user", "unused-password")
        self.assertEqual(nav.entered[-1], ("main", (0,)))
        self.assertEqual(nav.fake_driver.frames, ())
        self.assertEqual(nav.submissions, [])

    def test_existing_login_skips_password_submission(self):
        nav = ScenarioNavigator(signed_in=True)
        nav.prepare("unused-user", "unused-password")
        self.assertEqual(nav.submissions, [])

    def test_stop_cancels_before_any_login_or_entry_click(self):
        nav = ScenarioNavigator(stopped=lambda: True)
        with self.assertRaises(login.LoginCancelled):
            nav.prepare("unused-user", "unused-password")
        self.assertEqual(nav.clicks, [])
        self.assertEqual(nav.submissions, [])

    def test_bad_password_is_reported_without_repeated_submission(self):
        class RejectedLogin(ScenarioNavigator):
            def _fill_and_submit(self, state, uid, pwd):
                self.submissions.append((uid, pwd))
                self.location = "login_error"
                return True

        nav = RejectedLogin()
        with self.assertRaises(login.LoginFlowError) as caught:
            nav.prepare("test-user", "bad-password")
        self.assertEqual(caught.exception.stage, "门户登录")
        self.assertIn("账号或密码错误", caught.exception.reason)
        self.assertEqual(nav.submissions, [("test-user", "bad-password")])
        self.assertEqual(nav.clicks, [])

    def test_challenge_waits_for_human_completion_without_submitting(self):
        class HumanCompletedLogin(ScenarioNavigator):
            def _pause(self, seconds=0.2):
                super()._pause(seconds)
                if self.location == "human_challenge":
                    self.location = "portal_home"

        nav = HumanCompletedLogin()
        nav.location = "human_challenge"
        nav.prepare("test-user", "test-password")
        self.assertEqual(nav.submissions, [])
        self.assertTrue(any("人工处理" in message for message in nav.messages))
        self.assertEqual(nav.clicks, ["预约专区", "体育场馆预约"])

    def test_challenge_without_any_login_inputs_also_waits_for_human(self):
        class ChallengeOnly(ScenarioNavigator):
            def _scan(self):
                if self.location == "human_challenge":
                    return [page_state(text="安全验证", challenge=True)]
                return super()._scan()

            def _pause(self, seconds=0.2):
                super()._pause(seconds)
                if self.location == "human_challenge":
                    self.location = "portal_home"

        nav = ChallengeOnly()
        nav.location = "human_challenge"
        nav.prepare("test-user", "test-password")
        self.assertEqual(nav.submissions, [])
        self.assertTrue(any("人工处理" in message for message in nav.messages))

    def test_unfinished_login_times_out_without_becoming_success(self):
        class LoginDoesNotProgress(ScenarioNavigator):
            def _fill_and_submit(self, state, uid, pwd):
                self.submissions.append((uid, pwd))
                return True

        class Clock:
            value = 0

            def __call__(self):
                self.value += 1
                return self.value

        nav = LoginDoesNotProgress(manual_wait=1)
        with patch.object(login.time, "monotonic", Clock()):
            with self.assertRaises(login.LoginFlowError) as caught:
                nav.prepare("test-user", "test-password")
        self.assertIn("超时", caught.exception.reason)
        self.assertEqual(nav.submissions, [("test-user", "test-password")])
        self.assertEqual(nav.clicks, [])

    def test_stop_during_human_verification_is_cancellation(self):
        nav = ScenarioNavigator()
        nav.location = "human_challenge"
        nav.stopped = lambda: nav.pause_count > 0
        with self.assertRaises(login.LoginCancelled):
            nav.prepare("unused-user", "unused-password")
        self.assertEqual(nav.submissions, [])
        self.assertEqual(nav.clicks, [])

    def test_missing_entry_identifies_the_failing_stage(self):
        class MissingEntry(ScenarioNavigator):
            def _click_text(self, text, states=None):
                return False

        nav = MissingEntry(signed_in=True)
        with self.assertRaises(login.LoginFlowError) as caught:
            nav.prepare("unused-user", "unused-password")
        self.assertEqual(caught.exception.stage, "打开体育场馆入口")
        self.assertIn("未找到", caught.exception.reason)
        self.assertEqual(nav.submissions, [])

    def test_password_is_never_filled_into_http_or_external_frames(self):
        class UntrustedLogin(ScenarioNavigator):
            def _scan(self):
                if self.location == "portal_login":
                    return [page_state(self.untrusted_url, "账号密码登录", login_visible=True,
                                       password_visible=True)]
                return super()._scan()

            def _pause(self, seconds=0.2):
                super()._pause(seconds)
                if self.location == "portal_login":
                    self.location = "portal_home"

        for url in ("https://external.invalid/login", "http://superapp.xjtu.edu.cn/login",
                    "https://xjtu.edu.cn.attacker.invalid/login",
                    "http://202.117.17.144:8080/login",
                    "https://xjtu.edu.cn@attacker.invalid/login"):
            with self.subTest(url=url):
                nav = UntrustedLogin()
                nav.untrusted_url = url
                nav.prepare("test-user", "test-password")
                self.assertEqual(nav.submissions, [])
                self.assertTrue(any("人工" in message for message in nav.messages))

    def test_static_password_error_help_text_does_not_abort_a_valid_login(self):
        class LoginHelpText(ScenarioNavigator):
            def _scan(self):
                if self.location == "portal_login":
                    return [page_state(text="账号密码登录 密码错误请联系管理员",
                                       login_visible=True, password_visible=True)]
                return super()._scan()

        nav = LoginHelpText()
        nav.prepare("test-user", "test-password")
        self.assertEqual(nav.submissions, [("test-user", "test-password")])


class FakeFrame:
    def __init__(self, index, visible=True):
        self.index = index
        self.visible = visible

    def is_displayed(self):
        return self.visible


class ProbeDriver(FakeDriver):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.window_handles = list(dict.fromkeys(handle for handle, _ in pages))
        self.current_window_handle = self.window_handles[0]
        self.hidden_frames = set()

    def execute_script(self, script):
        if script != login.PROBE_SCRIPT:
            raise AssertionError("unexpected script")
        return {key: value for key, value in self.pages[(self.current_window_handle, self.frames)].items()
                if key not in {"handle", "frames"}}

    def find_elements(self, by, selector):
        if selector != "iframe,frame":
            raise AssertionError("unexpected selector")
        current = self.frames
        children = [frames[-1] for handle, frames in self.pages
                    if handle == self.current_window_handle
                    and len(frames) == len(current) + 1 and frames[:-1] == current]
        return [FakeFrame(index, (self.current_window_handle, current + (index,)) not in self.hidden_frames)
                for index in sorted(set(children))]


class ProbeAndDiagnosticsTests(unittest.TestCase):
    def test_real_scanner_checks_iframe_and_restores_original_context(self):
        driver = ProbeDriver({
            ("main", ()): page_state(text="体育场馆预约"),
            ("main", (0,)): page_state(SPORTS_URL, "所有场地"),
            ("other", ()): page_state(text="预约专区"),
        })
        driver.current_window_handle = "other"
        nav = login.LoginNavigator(driver, log=lambda _: None)
        states = nav._scan()
        self.assertEqual(len(states), 3)
        homes = [state for state in states if state["venue_home"]]
        self.assertEqual([(state["handle"], state["frames"]) for state in homes], [("main", (0,))])
        self.assertEqual(driver.current_window_handle, "other")
        self.assertEqual(driver.frames, ())

    def test_hidden_frame_cannot_falsely_identify_an_authenticated_home(self):
        driver = ProbeDriver({
            ("main", ()): page_state(text="账号密码登录", login_visible=True, password_visible=True),
            ("main", (0,)): page_state(SPORTS_URL, "所有场地"),
        })
        driver.hidden_frames.add(("main", (0,)))
        states = login.LoginNavigator(driver, log=lambda _: None)._scan()
        self.assertEqual(len(states), 1)
        self.assertFalse(any(state["venue_home"] for state in states))

    def test_saved_diagnostics_omit_authentication_and_dom_data(self):
        uid, password, ticket, token = "secret-user-77", "secret-password-99", "ST-private-ticket", "private-token"
        cookie_value, field_value = "private-cookie-value", "private-field-value"
        state = page_state(
            f"https://{uid}:{password}@superapp.xjtu.edu.cn/index?ticket={ticket}#access_token={token}",
            text=f"DOM PRIVATE {cookie_value} {field_value}",
            title=f"Welcome {uid}",
            errors=[f"密码错误 {password}"],
            cookies={"SESSION": cookie_value},
            input_values=[field_value],
        )
        with tempfile.TemporaryDirectory() as folder:
            nav = login.LoginNavigator(FakeDriver(), log=lambda _: None, base_dir=folder, secrets=(uid, password))
            with patch.object(nav, "_scan", return_value=[state]):
                path = Path(nav.save_diagnostics(f"门户登录 {uid}", RuntimeError(f"error {password}")))
            self.assertEqual(path.parent, Path(folder) / "login_diagnostics")
            raw = path.read_text(encoding="utf-8")
            record = json.loads(raw)
        for secret in (uid, password, ticket, token, cookie_value, field_value):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, raw)
        self.assertNotIn("text", record["contexts"][0])
        self.assertNotIn("cookies", record["contexts"][0])
        self.assertNotIn("input_values", record["contexts"][0])
        self.assertIn(record["contexts"][0]["url"],
                      {"https://superapp.xjtu.edu.cn/index", "[网址已隐藏]"})
        self.assertEqual(record["contexts"][0]["error_category"], "账号或密码错误")

    def test_diagnostics_preserve_origin_and_path_but_strip_sso_query_and_fragment(self):
        state = page_state("https://superapp.xjtu.edu.cn/index?ticket=PRIVATE_TICKET#access_token=PRIVATE_TOKEN")
        with tempfile.TemporaryDirectory() as folder:
            nav = login.LoginNavigator(FakeDriver(), base_dir=folder)
            with patch.object(nav, "_scan", return_value=[state]):
                path = Path(nav.save_diagnostics("门户登录"))
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["contexts"][0]["url"], "https://superapp.xjtu.edu.cn/index")

    def test_diagnostics_remain_available_after_user_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            nav = login.LoginNavigator(FakeDriver(), stopped=lambda: True, base_dir=folder)
            path = Path(nav.save_diagnostics("用户停止", login.LoginCancelled("stopped")))
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["diagnostic_error"], "LoginCancelled")
        self.assertEqual(record["contexts"], [])


class FakeElement:
    def __init__(self, tag="input", text="", children=(), attrs=None, visible=True, enabled=True):
        self.tag_name = tag
        self.text = text
        self.children = list(children)
        self.attrs = attrs or {}
        self.visible = visible
        self.enabled = enabled
        self.click_count = 0
        self.values = []

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.attrs.get(name)

    def find_elements(self, by, selector):
        if selector == ".//*":
            return self.children
        if selector == "ancestor::form[1]":
            return []
        raise AssertionError(f"unexpected element selector: {selector}")

    def click(self):
        self.click_count += 1

    def clear(self):
        self.values.clear()

    def send_keys(self, value):
        self.values.append(value)


class ElementDriver(FakeDriver):
    def __init__(self, elements):
        super().__init__()
        self.elements = elements

    def find_elements(self, by, selector):
        return self.elements.get(selector, [])


class SelectionOnlyNavigator(login.LoginNavigator):
    """Keep selector unit tests separate from the real DOM click tests."""

    def _scroll_and_click(self, element):
        self._check_stop()
        element.click()
        return True


class ElementInteractionTests(unittest.TestCase):
    def test_native_click_prefers_link_over_same_text_ancestor(self):
        link = FakeElement(tag="a", text="体育场馆预约")
        wrapper = FakeElement(tag="div", text="体育场馆预约", children=[link])
        driver = ElementDriver({"//*[contains(normalize-space(.), '体育场馆预约')]": [wrapper, link]})
        nav = SelectionOnlyNavigator(driver, log=lambda _: None)
        self.assertTrue(nav._click_text("体育场馆预约", [page_state()]))
        self.assertEqual(link.click_count, 1)
        self.assertEqual(wrapper.click_count, 0)

    def test_account_field_wins_over_search_captcha_and_unrelated_input(self):
        search = FakeElement(attrs={"type": "text", "placeholder": "搜索"})
        captcha = FakeElement(attrs={"type": "text", "name": "captcha"})
        unrelated = FakeElement(attrs={"type": "text", "name": "other-field"})
        account = FakeElement(attrs={"type": "text", "autocomplete": "username"})
        password = FakeElement(attrs={"type": "password"})
        disabled = FakeElement(tag="button", text="登录", enabled=False)
        button = FakeElement(tag="button", text="登 录")
        driver = ElementDriver({
            "input[type='password']": [password],
            "input": [search, captcha, unrelated, account, password],
            "button,input[type='submit']": [disabled, button],
        })
        nav = SelectionOnlyNavigator(driver, log=lambda _: None)
        self.assertTrue(nav._fill_and_submit(page_state(), "test-user", "test-password"))
        self.assertEqual(account.values, ["test-user"])
        self.assertEqual(password.values, ["test-password"])
        self.assertEqual(button.click_count, 1)
        self.assertEqual(disabled.click_count, 0)
        self.assertEqual(search.values + captcha.values + unrelated.values, [])


if __name__ == "__main__":
    unittest.main()
