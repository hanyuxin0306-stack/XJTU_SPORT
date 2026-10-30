import json
import time
import datetime
import random
import cv2
import numpy as np
import base64
import requests
import sys
import os
import threading 
import traceback
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox
from requests.adapters import HTTPAdapter
from selenium import webdriver
import re
import uuid
from urllib.parse import urlsplit, urlunsplit
from selenium.common.exceptions import (
    ElementClickInterceptedException, ElementNotInteractableException,
    StaleElementReferenceException, TimeoutException, WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
# ==========================================
# 登录导航：与界面、预约逻辑共用本文件
# ==========================================
SUPERAPP_URL = "https://superapp.xjtu.edu.cn/pages/tab/index/index"
SPORTS_HOST = "202.117.17.144"
MAX_WINDOWS = 6
MAX_CONTEXTS = 16
MAX_FRAME_DEPTH = 2
NORMAL_WAIT = 30
NAV_CLICK_TIMEOUT = 4.0
NAV_CLICK_ATTEMPTS = 4


class LoginFlowError(RuntimeError):
    def __init__(self, stage, reason):
        self.stage = str(stage)
        self.reason = str(reason)
        super().__init__(f"{self.stage}：{self.reason}")


class LoginCancelled(RuntimeError):
    """A user stop is distinct from a login failure."""


def classify_state(state):
    """Classify visible DOM evidence together with the actual frame origin."""
    result = dict(state)
    try:
        address = urlsplit(str(state.get("url", "")))
        sports_origin = address.hostname == SPORTS_HOST and address.port == 8080
        portal_origin = address.hostname == "superapp.xjtu.edu.cn"
    except ValueError:
        sports_origin = portal_origin = False
    text = str(state.get("text", ""))
    blocked = bool(state.get("login_visible") or state.get("challenge"))
    venue_markers = ("关注场地", "所有场地", "场馆列表", "场地列表", "选择场馆")
    result["venue_home"] = bool(
        sports_origin and not blocked
        and (any(marker in text for marker in venue_markers) or state.get("product_links"))
    )
    result["portal_home"] = bool(
        portal_origin and not blocked
        and ("预约专区" in text or "体育场馆预约" in text)
    )
    return result


# No input values, cookies, local storage or HTML are returned by this probe.
PROBE_SCRIPT = r"""
/* SPORT_LOGIN_PROBE_V1 */
function visible(el) {
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
        && style.visibility !== 'hidden';
}
const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim();
const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
const password = inputs.some(el => el.type === 'password');
const sms = /手机动态码登录|短信登录|发送动态码|发送验证码/.test(text);
const captchaInput = inputs.some(el => /captcha|verifycode|verification|验证码/i.test(
    [el.name, el.id, el.placeholder].join(' ')));
const challenge = captchaInput || /拖动滑块|拖动.*拼图|安全验证|请完成验证|扫码确认|二次认证/.test(text)
    || Array.from(document.querySelectorAll('iframe')).some(el => visible(el)
        && /captcha|recaptcha|hcaptcha/i.test(el.src || ''));
const errors = Array.from(document.querySelectorAll(
    '[role="alert"],.el-message,.el-notification,.error,.error-msg,.error-message,' +
    '.login-error,.sw-login-error,.el-form-item__error,#errorMsg,#errorMessage'
)).filter(visible).map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 8);
const loginRoots = inputs.filter(el => el.type === 'password').map(el =>
    el.closest('form') || el.closest('[role="form"]'));
const loginText = loginRoots.filter(Boolean).map(el => el.innerText || '').join(' ');
return {url: location.href, title: document.title, ready: document.readyState,
    text: text.slice(0, 12000), password_visible: password, sms_visible: sms,
    login_visible: password || (sms && /登录|动态码/.test(text)), challenge: challenge,
    errors: errors, login_text: loginText.slice(0, 3000),
    product_links: Array.from(document.querySelectorAll('a[href]'))
        .some(el => visible(el) && /\/web\/product\/show\.html/.test(el.getAttribute('href') || ''))};
"""


SCROLL_TARGET_SCRIPT = """
arguments[0].scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
"""

CLICK_POINT_SCRIPT = """
const target = arguments[0], rect = target.getBoundingClientRect();
const width = document.documentElement.clientWidth || innerWidth;
const height = document.documentElement.clientHeight || innerHeight;
const left = Math.max(0, rect.left), right = Math.min(width, rect.right);
const top = Math.max(0, rect.top), bottom = Math.min(height, rect.bottom);
const describe = el => el ? (el.tagName.toLowerCase() +
    Array.from(el.classList || []).slice(0, 4).map(name => '.' + name).join('')) : 'none';
if (right <= left || bottom <= top) return {clear: false, blocker: 'outside viewport'};
const centerX = (left + right) / 2, centerY = (top + bottom) / 2;
let blocker = 'none';
for (const [fx, fy] of [[.5,.5], [.5,.25], [.5,.75], [.25,.5], [.75,.5],
                       [.25,.25], [.75,.25], [.25,.75], [.75,.75]]) {
    const x = left + (right-left)*fx, y = top + (bottom-top)*fy;
    const hit = document.elementFromPoint(x, y);
    if (hit === target || target.contains(hit)) {
        return {clear: true, center: fx === .5 && fy === .5,
                offset_x: Math.round(x-centerX), offset_y: Math.round(y-centerY),
                target: describe(target), x: Math.round(x), y: Math.round(y)};
    }
    if (fx === .5 && fy === .5) blocker = describe(hit);
}
return {clear: false, blocker: blocker, target: describe(target)};
"""


class LoginNavigator:
    def __init__(self, driver, log=print, stopped=lambda: False, base_dir=".",
                 secrets=(), manual_wait=120):
        self.driver = driver
        self.log = log
        self.stopped = stopped
        self.base_dir = os.path.abspath(base_dir)
        self.secrets = tuple(str(value) for value in secrets if value)
        self.manual_wait = max(0.0, float(manual_wait))
        self.stage = "尚未开始"
        self._last_probe_error = ""
        self._last_click = {}

    def _redact(self, value):
        value = str(value)
        def clean_url(match):
            try:
                parsed = urlsplit(match.group(0))
                authority = parsed.hostname or ""
                if parsed.port:
                    authority += ":" + str(parsed.port)
                return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
            except ValueError:
                return "[网址已隐藏]"
        value = re.sub(r"https?://[^\s<>\"']+", clean_url, value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[已隐藏]")
        return value

    def _log(self, message):
        self.log(self._redact(message))

    def _check_stop(self):
        if self.stopped():
            raise LoginCancelled("用户已停止登录检查")

    def _pause(self, seconds=0.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self._check_stop()
            time.sleep(min(0.05, max(0, until - time.monotonic())))
        self._check_stop()

    def _enter(self, state):
        self._check_stop()
        self.driver.switch_to.window(state["handle"])
        self.driver.switch_to.default_content()
        for index in state.get("frames", ()):
            self._check_stop()
            frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            self.driver.switch_to.frame(frames[index])

    def _scan(self):
        self._check_stop()
        result = []
        try:
            original = self.driver.current_window_handle
        except WebDriverException:
            original = None  # SSO may close its own login popup after success.
        try:
            handles = list(self.driver.window_handles)[:MAX_WINDOWS]
        except WebDriverException as exc:
            raise LoginFlowError(self.stage, "无法读取浏览器窗口（" + type(exc).__name__ + "）") from exc
        if not handles:
            raise LoginFlowError(self.stage, "浏览器窗口已关闭。")
        try:
            for handle in handles:
                pending = [()]
                visited = 0
                while pending and visited < MAX_CONTEXTS:
                    self._check_stop()
                    frames = pending.pop(0)
                    state = {"handle": handle, "frames": frames}
                    visited += 1
                    try:
                        self._enter(state)
                        details = self.driver.execute_script(PROBE_SCRIPT)
                        state.update(details or {})
                        result.append(classify_state(state))
                        if len(frames) < MAX_FRAME_DEPTH:
                            children = self.driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
                            pending.extend(frames + (index,) for index, element in enumerate(children[:8])
                                           if element.is_displayed())
                    except LoginCancelled:
                        raise
                    except (WebDriverException, IndexError) as exc:
                        self._last_probe_error = type(exc).__name__
        finally:
            try:
                if original in self.driver.window_handles:
                    self.driver.switch_to.window(original)
                self.driver.switch_to.default_content()
            except WebDriverException:
                pass
        return result

    @staticmethod
    def _error_category(state):
        text = " ".join(str(item) for item in state.get("errors", []))
        text += " " + str(state.get("login_text", ""))
        categories = (
            ("账号或密码错误", ("账号或密码错误", "用户名或密码错误", "用户名或密码不正确", "密码错误", "密码不正确")),
            ("账号不存在", ("账号不存在", "账户不存在", "用户名不存在")),
            ("账号已锁定", ("账号被锁", "账户被锁", "账号已锁", "账户已锁", "账号被禁用")),
            ("登录访问受限", ("访问过于频繁", "登录次数过多", "尝试次数过多", "账号已被冻结")),
        )
        for category, phrases in categories:
            if any(phrase in text for phrase in phrases):
                return category
        return ""

    @staticmethod
    def _credential_origin_ok(state):
        try:
            address = urlsplit(str(state.get("url", "")))
            host = address.hostname or ""
            return address.scheme == "https" and (host == "xjtu.edu.cn" or host.endswith(".xjtu.edu.cn"))
        except ValueError:
            return False

    def _scroll_and_click(self, element):
        """Use a verified visible point; never dispatch a click through an overlay."""
        self._check_stop()
        # scrollIntoView also scrolls uni-app's nested scroll-view containers.
        self.driver.execute_script(SCROLL_TARGET_SCRIPT, element)
        self._pause(0.1)
        point = self.driver.execute_script(CLICK_POINT_SCRIPT, element)
        self._last_click = {"label": self._last_click.get("label", ""), **(point or {})}
        if not point or not point.get("clear"):
            return False
        self._check_stop()
        if point.get("center"):
            element.click()
        else:
            # Keep a real pointer gesture for window.open and other navigation.
            ActionChains(self.driver).move_to_element_with_offset(
                element, point["offset_x"], point["offset_y"]
            ).click().perform()
        return True

    def _click_text(self, text, states=None):
        deadline = time.monotonic() + NAV_CLICK_TIMEOUT
        states = self._scan() if states is None else states
        saw_target = False
        warned = False
        last_error = None
        self._last_click = {"label": text}
        retryable = (ElementClickInterceptedException, ElementNotInteractableException,
                     StaleElementReferenceException)
        for attempt in range(NAV_CLICK_ATTEMPTS):
            self._check_stop()
            for state in states:
                self._check_stop()
                try:
                    self._enter(state)
                    candidates = self.driver.find_elements(
                        By.XPATH, "//*[contains(normalize-space(.), '" + text + "')]"
                    )
                    visible = []
                    for element in candidates:
                        self._check_stop()
                        if element.is_displayed() and element.is_enabled():
                            label = " ".join(element.text.split())
                            if label == text or (text in label and len(label) <= len(text) + 10):
                                tag = element.tag_name.lower()
                                if tag in {"html", "body"}:
                                    continue
                                priority = 0 if tag in {"a", "button", "li"} else 1
                                children = element.find_elements(By.XPATH, ".//*")
                                visible.append((len(label), priority, len(children), element, children))
                    # A broad same-text ancestor may also contain an overlay.
                    # Only a semantic control or the deepest label is a target.
                    visible = [item for item in visible if item[1] == 0 or not any(
                        other[3] != item[3] and other[3] in item[4] for other in visible
                    )]
                    visible.sort(key=lambda item: item[:3])
                    for _, _, _, element, _ in visible[:8]:
                        saw_target = True
                        self._check_stop()
                        if time.monotonic() >= deadline:
                            break
                        try:
                            if self._scroll_and_click(element):
                                return True  # A successful click must never be replayed.
                        except retryable as exc:
                            last_error = exc  # These failures occur before click dispatch.
                except LoginCancelled:
                    raise
                except retryable as exc:
                    saw_target = True
                    last_error = exc
                except WebDriverException as exc:
                    # An unknown error may occur after the browser sends the event.
                    # Do not risk opening a second popup by replaying that click.
                    raise LoginFlowError(self.stage, "点击“" + text + "”失败（" + type(exc).__name__ + "）") from exc
            if not saw_target:
                return False
            if not warned:
                warned = True
                self._log("[登录检查] “" + text + "”的点击位置被遮挡或页面正在更新，正在调整位置。")
            if time.monotonic() >= deadline or attempt == NAV_CLICK_ATTEMPTS - 1:
                break
            self._pause(0.15)
            states = self._scan()  # Re-locate after layout changes or stale elements.
        blocker = self._last_click.get("blocker")
        reason = ("点击“" + text + "”仍被页面遮挡（" + blocker + "）；请在浏览器查看遮挡内容。"
                  if blocker else "点击“" + text + "”未完成，页面元素仍在变化。")
        raise LoginFlowError(self.stage, reason) from last_error

    def _fill_and_submit(self, state, uid, pwd):
        if not self._credential_origin_ok(state):
            return False
        self._enter(state)
        passwords = [element for element in self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                     if element.is_displayed() and element.is_enabled()]
        if not passwords:
            return False
        password = passwords[0]
        forms = password.find_elements(By.XPATH, "ancestor::form[1]")
        scope = forms[0] if forms else self.driver
        candidates = []
        for element in scope.find_elements(By.CSS_SELECTOR, "input"):
            self._check_stop()
            kind = (element.get_attribute("type") or "text").lower()
            if kind not in {"text", "email", "tel", "number"} or not element.is_displayed() or not element.is_enabled():
                continue
            identity = " ".join(element.get_attribute(key) or "" for key in ("name", "id", "placeholder", "autocomplete"))
            if re.search(r"captcha|verify|验证码|动态码|搜索|search", identity, re.I):
                continue
            score = bool(re.search(r"username|userid|user_name|account|loginname|账号|用户名|学号|工号", identity, re.I))
            candidates.append((score, element))
        if not candidates:
            return False
        candidates.sort(key=lambda item: item[0], reverse=True)
        user = candidates[0][1]
        self._check_stop()
        user.clear()
        user.send_keys(uid)
        password.clear()
        password.send_keys(pwd)
        for button in scope.find_elements(By.CSS_SELECTOR, "button,input[type='submit']"):
            self._check_stop()
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = re.sub(r"\s+", "", button.text or button.get_attribute("value") or "")
            if label in {"登录", "立即登录", "登錄", "Login", "LoginNow", "Signin"}:
                button.click()
                return True
        return False

    def _wait_for_ready(self, stage, target, uid, pwd):
        self.stage = stage
        self._log("[登录检查] " + stage + "：等待页面加载。")
        deadline = time.monotonic() + NORMAL_WAIT
        manual_deadline = None
        submitted = False
        switched = False
        submitted_at = None
        saw_login = False
        while True:
            self._check_stop()
            states = self._scan()
            home = next((state for state in states if state.get("venue_home")), None)
            if home is not None:
                return home
            login_states = [state for state in states
                            if state.get("login_visible") or state.get("challenge")]
            for state in login_states:
                category = self._error_category(state)
                if category:
                    raise LoginFlowError(stage, category + "；请检查浏览器提示后再运行。")
            if target == "portal" and not login_states:
                portal = next((state for state in states if state.get("portal_home")), None)
                if portal is not None:
                    return portal
            if login_states:
                saw_login = True
                state = login_states[0]
                if not state.get("password_visible") and not switched and not submitted:
                    switched = True
                    if self._click_text("账号密码登录", [state]):
                        self._log("[登录检查] 已切换账号密码登录。")
                        self._pause()
                        continue
                needs_manual = bool(state.get("challenge") or (state.get("sms_visible") and not state.get("password_visible")))
                if state.get("password_visible") and not self._credential_origin_ok(state):
                    needs_manual = True
                    if manual_deadline is None:
                        self._log("[登录检查] 当前登录页面不属于 HTTPS 校园认证域，请在浏览器中人工确认和登录。")
                if not submitted and state.get("password_visible") and not needs_manual and uid and pwd:
                    # Mark before clicking so transient navigation cannot cause another submit.
                    submitted = True
                    submitted_at = time.monotonic()
                    try:
                        clicked = self._fill_and_submit(state, uid, pwd)
                    except WebDriverException as exc:
                        raise LoginFlowError(stage, "填写或提交登录失败（" + type(exc).__name__ + "）") from exc
                    self._log("[登录检查] 已提交账号密码，等待真实登录结果。" if clicked else
                              "[登录检查] 无法确认登录按钮，请在浏览器中人工完成登录。")
                    if not clicked:
                        needs_manual = True
                if submitted_at is not None and time.monotonic() - submitted_at >= 3:
                    needs_manual = True
                if not uid or not pwd:
                    needs_manual = True
                if needs_manual and manual_deadline is None:
                    manual_deadline = time.monotonic() + self.manual_wait
                    self._log(f"[登录检查] 需要人工处理：请在浏览器完成验证码、短信或登录确认，最多等待 {self.manual_wait:g} 秒；程序不会重复提交密码。")
            now = time.monotonic()
            effective_deadline = manual_deadline if manual_deadline is not None else deadline
            if now >= effective_deadline:
                if manual_deadline is not None:
                    reason = "等待人工完成登录或验证超时"
                elif saw_login:
                    reason = "登录尚未完成；未识别到成功跳转"
                else:
                    reason = "未识别到预约服务首页" if target == "sports" else "未识别到门户首页或登录表单"
                if self._last_probe_error:
                    reason += "（页面检测：" + self._last_probe_error + "）"
                raise LoginFlowError(stage, reason)
            self._pause()

    def prepare(self, uid, pwd):
        self.secrets = tuple(set(self.secrets + tuple(str(value) for value in (uid, pwd) if value)))
        self._check_stop()
        self.stage = "打开校园门户"
        self._log("[登录检查] 打开校园门户。")
        self.driver.set_page_load_timeout(NORMAL_WAIT)
        try:
            self.driver.get(SUPERAPP_URL)
        except TimeoutException:
            self._log("[登录检查] 门户加载较慢，继续检查已加载页面。")
        except WebDriverException as exc:
            raise LoginFlowError(self.stage, "打开门户失败（" + type(exc).__name__ + "）") from exc
        state = self._wait_for_ready("门户登录", "portal", uid, pwd)
        if not state.get("venue_home"):
            self.stage = "打开体育场馆入口"
            self._log("[登录检查] 门户已就绪，查找体育场馆预约入口。")
            states = self._scan()
            clicked = self._click_text("体育场馆预约", states)
            if not clicked:
                if not self._click_text("预约专区", states):
                    raise LoginFlowError(self.stage, "未找到预约专区或体育场馆预约入口；可能是门户页面结构变化。")
                self._log("[登录检查] 已点击预约专区，等待体育场馆预约入口。")
                deadline = time.monotonic() + NORMAL_WAIT
                while time.monotonic() < deadline:
                    self._check_stop()
                    states = self._scan()
                    state = next((item for item in states if item.get("venue_home")), None)
                    if state is not None:
                        break
                    if self._click_text("体育场馆预约", states):
                        clicked = True
                        break
                    self._pause()
                if state is None and not clicked:
                    raise LoginFlowError(self.stage, "点击预约专区后未找到体育场馆预约入口。")
            if state is None or not state.get("venue_home"):
                self._log("[登录检查] 已点击体育场馆预约；同时检查原页面、新窗口和内嵌页面。")
                state = self._wait_for_ready("场馆登录", "sports", uid, pwd)
        self._check_stop()
        self._enter(state)
        self.driver.switch_to.default_content()
        self.stage = "登录检查完成"
        self._log("[登录检查] 已确认进入体育场馆预约服务首页。")

    def save_diagnostics(self, stage, exc=None):
        """Save classified state only; omit DOM text, images and authentication data."""
        folder = os.path.join(self.base_dir, "login_diagnostics")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(folder, "login_" + stamp + "_" + uuid.uuid4().hex[:8] + ".json")
        record = {"stage": self._redact(stage), "time": datetime.datetime.now().isoformat(timespec="seconds"),
                  "contexts": [], "exception": None,
                  "last_click": {key: self._redact(value) if isinstance(value, str) else value
                                 for key, value in self._last_click.items()}}
        if exc is not None:
            record["exception"] = {
                "type": type(exc).__name__, "message": self._redact(exc),
                "traceback": self._redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
            }
        try:
            states = self._scan()
            handles = {}
            for state in states:
                handle = handles.setdefault(state["handle"], "window_" + str(len(handles) + 1))
                record["contexts"].append({
                    "window": handle, "frames": list(state.get("frames", ())),
                    "url": self._redact(state.get("url", "")),
                    "title": self._redact(state.get("title", ""))[:150],
                    "ready": state.get("ready", ""),
                    "login_visible": bool(state.get("login_visible")),
                    "challenge": bool(state.get("challenge")),
                    "portal_home": bool(state.get("portal_home")),
                    "venue_home": bool(state.get("venue_home")),
                    "error_category": self._error_category(state),
                })
        except (WebDriverException, LoginCancelled, LoginFlowError) as scan_exc:
            record["diagnostic_error"] = type(scan_exc).__name__
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        return path

# 经典界面与预约引擎
# ==========================================
# 🗺️ 终极武器库 3.0：场馆、场地、时间全映射字典
# ==========================================
MORNING_TIMES = ["08:00-09:00", "09:01-10:00", "10:01-11:00", "11:01-12:00"]
SUMMER_AFTERNOON_TIMES = [
    "14:30-15:30", "15:31-16:30", "16:31-17:30", "17:31-18:30",
    "18:31-19:30", "19:31-20:30", "20:31-21:30"
]
WINTER_AFTERNOON_TIMES = [
    "14:01-15:00", "15:01-16:00", "16:01-17:00", "17:01-18:00",
    "18:01-19:00", "19:01-20:00", "20:01-21:00"
]
SCHEDULE_MODE_SUMMER = "夏季作息"
SCHEDULE_MODE_WINTER = "冬季作息"
SCHEDULE_MODES = (SCHEDULE_MODE_SUMMER, SCHEDULE_MODE_WINTER)
AFTERNOON_TIMES_BY_MODE = {
    SCHEDULE_MODE_SUMMER: SUMMER_AFTERNOON_TIMES,
    SCHEDULE_MODE_WINTER: WINTER_AFTERNOON_TIMES,
}
TIME_MODE_EQUIVALENTS = {
    **{winter: summer for winter, summer in zip(WINTER_AFTERNOON_TIMES, SUMMER_AFTERNOON_TIMES)},
    **{summer: winter for winter, summer in zip(WINTER_AFTERNOON_TIMES, SUMMER_AFTERNOON_TIMES)},
}

VENUE_MAP = {
    "创新港三号巨构乒乓球台": {
        "id": "105",
        "courts": ["场地13", "场地14", "场地15", "场地16", "场地17", "场地18"],
        "times": MORNING_TIMES + SUMMER_AFTERNOON_TIMES
    },
    "创新港一号巨构羽毛球场": {
        "id": "101",
        "courts": ["场地1", "场地2", "场地3"],
        "times": MORNING_TIMES + SUMMER_AFTERNOON_TIMES
    },
    "创新港二号巨构羽毛球场": {
        "id": "103",
        "courts": ["场地1", "场地2", "场地3","场地4","场地5","场地6"],
        "times": MORNING_TIMES + SUMMER_AFTERNOON_TIMES
    },
    "创新港三号巨构羽毛球场": {
        "id": "104",
        "courts": ["场地1", "场地2", "场地3"],
        "times": MORNING_TIMES + SUMMER_AFTERNOON_TIMES
    },
    "高等工程教育博物馆负一楼羽毛球场": {
        "id": "321",
        "courts": ["场地1", "场地2", "场地3"],
        "times": MORNING_TIMES + SUMMER_AFTERNOON_TIMES
    }
}

APP_VERSION = "1030 正式版"
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
app_config = {"venues": {}}
current_loaded_venue = None 
RUN_SUMMARY_KEYS = ("成功", "已被预订", "名额已满", "验证码有误", "Cookie失效", "未知返回")
run_state_lock = threading.Lock()
log_buffer_lock = threading.Lock()
pending_log_lines = []
log_flush_scheduled = False
JSON_TRUE_RESULT_MARKERS = (b'"result":"1"', b'"result":1')
EMPTY_BODY_MARKERS = (b"502",)
AREA_BLOCK_MARKERS = tuple(text.encode("utf-8") for text in ("异常", "频繁", "封禁"))
SUBMIT_MESSAGE_MARKERS = {
    "LIMIT_REACHED": "本天预订数量超过限制".encode("utf-8"),
    "TIME_NOT_OPEN": "请到8:40-21:40再来".encode("utf-8"),
    "DATE_NOT_OPEN": "未到该日期的预订时间".encode("utf-8"),
    "NOT_BOOKABLE": "不在可预订".encode("utf-8"),
    "BAD_CAPTCHA": "验证码有误".encode("utf-8"),
    "BAD_DATA": "数据有误".encode("utf-8"),
    "ALREADY_BOOKED": "已被预订".encode("utf-8"),
    "NOT_LOGIN": "USERNOTLOGINYET".encode("utf-8"),
}

DEFAULT_SPEED_PROFILE = {
    "mode_label": "稳妥模式",
    "track_target_min_ms": 200,
    "track_target_max_ms": 300,
    "track_down_min_ms": 30,
    "track_down_max_ms": 80,
    "track_end_pause_min_ms": 30,
    "track_end_pause_max_ms": 80,
    "min_slide_time_ms": 100,
    "submit_floor_min_s": 0.65,
    "submit_floor_max_s": 0.70,
    "inventory_retry_min_s": 0.30,
    "inventory_retry_max_s": 0.60,
    "inventory_retry_step_s": 0.10,
    "inventory_error_retry_min_s": 0.40,
    "inventory_error_retry_max_s": 0.70,
    "gate_probe_sleep_s": 0.10,
    "gate_retry_sleep_s": 0.20,
    "recovery_cookie_sleep_s": 0.20,
    "recovery_error_sleep_s": 0.10,
    "initial_cookie_poll_sleep_s": 0.50,
    "log_flush_delay_ms": 20,
}

SPRINT_SPEED_PROFILE = {
    "mode_label": "极限竞速模式",
    "track_target_min_ms": 150,
    "track_target_max_ms": 220,
    "track_down_min_ms": 18,
    "track_down_max_ms": 45,
    "track_end_pause_min_ms": 18,
    "track_end_pause_max_ms": 45,
    "min_slide_time_ms": 80,
    "submit_floor_min_s": 0.50,
    "submit_floor_max_s": 0.56,
    "inventory_retry_min_s": 0.18,
    "inventory_retry_max_s": 0.30,
    "inventory_retry_step_s": 0.05,
    "inventory_error_retry_min_s": 0.22,
    "inventory_error_retry_max_s": 0.35,
    "gate_probe_sleep_s": 0.05,
    "gate_retry_sleep_s": 0.12,
    "recovery_cookie_sleep_s": 0.12,
    "recovery_error_sleep_s": 0.06,
    "initial_cookie_poll_sleep_s": 0.25,
    "log_flush_delay_ms": 12,
}


def create_empty_run_state():
    return {
        "started_at": None,
        "target_date": "",
        "venue_name": "",
        "group_count": 0,
        "status_text": "待启动",
        "last_event": "",
        "last_success_event": "",
        "live_log_enabled": True,
        "timing_diag_enabled": True,
        "export_log_enabled": False,
        "log_flush_delay_ms": DEFAULT_SPEED_PROFILE["log_flush_delay_ms"],
        "status_counts": {key: 0 for key in RUN_SUMMARY_KEYS},
        "stage_costs": {},
        "log_lines": [],
        "export_path": "",
    }


current_run_state = create_empty_run_state()
task_control_lock = threading.Lock()
task_stop_event = threading.Event()
active_run_thread = None
active_driver = None
task_runtime_state = {"running": False, "manual_stop": False}

def resource_path(relative_path):
    try: base_path = sys._MEIPASS
    except Exception: base_path = APP_DIR
    return os.path.join(base_path, relative_path)


def get_app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    if "__file__" in globals():
        return os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(".")

def stop_requested():
    return task_stop_event.is_set()

def manual_stop_requested():
    with task_control_lock:
        return bool(task_runtime_state.get("manual_stop"))

def task_running():
    with task_control_lock:
        return bool(task_runtime_state.get("running"))

def reset_task_control_for_start():
    global active_run_thread
    task_stop_event.clear()
    with task_control_lock:
        task_runtime_state["running"] = True
        task_runtime_state["manual_stop"] = False
        active_run_thread = None

def mark_task_idle():
    global active_run_thread, active_driver
    with task_control_lock:
        task_runtime_state["running"] = False
        active_run_thread = None
        active_driver = None

def set_active_run_thread(thread):
    global active_run_thread
    with task_control_lock:
        active_run_thread = thread

def set_active_driver(driver):
    global active_driver
    with task_control_lock:
        active_driver = driver

def clear_active_driver(driver=None):
    global active_driver
    with task_control_lock:
        if driver is None or active_driver is driver:
            active_driver = None

def sleep_with_stop(seconds, interval=0.1):
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if task_stop_event.wait(min(interval, remaining)):
            return True
        remaining -= min(interval, remaining)
    return stop_requested()


def export_log_enabled():
    if task_running():
        with run_state_lock:
            return bool(current_run_state.get("export_log_enabled", False))
    try:
        return bool(export_log_var.get())
    except Exception:
        return False


def current_log_flush_delay_ms():
    if task_running():
        with run_state_lock:
            return int(current_run_state.get("log_flush_delay_ms", DEFAULT_SPEED_PROFILE["log_flush_delay_ms"]))
    return DEFAULT_SPEED_PROFILE["log_flush_delay_ms"]


def sanitize_filename(text, fallback="run"):
    cleaned = []
    for char in str(text or ""):
        if char in '<>:"/\\|?*' or ord(char) < 32:
            cleaned.append("_")
        elif char.isspace():
            cleaned.append("_")
        else:
            cleaned.append(char)
    result = "".join(cleaned).strip("._")
    return result or fallback


def resolve_log_export_dir():
    return get_app_base_dir()


def format_top3_costs(stage_costs):
    ordered = sorted(
        ((name, cost) for name, cost in (stage_costs or {}).items() if cost > 0),
        key=lambda item: item[1],
        reverse=True
    )[:3]
    if not ordered:
        return "暂无"
    return " | ".join(f"{name}={cost:.3f}s" for name, cost in ordered)


def snapshot_run_state():
    with run_state_lock:
        return {
            "started_at": current_run_state.get("started_at"),
            "target_date": current_run_state.get("target_date", ""),
            "venue_name": current_run_state.get("venue_name", ""),
            "group_count": current_run_state.get("group_count", 0),
            "status_text": current_run_state.get("status_text", "待启动"),
            "last_event": current_run_state.get("last_event", ""),
            "last_success_event": current_run_state.get("last_success_event", ""),
            "live_log_enabled": current_run_state.get("live_log_enabled", True),
            "timing_diag_enabled": current_run_state.get("timing_diag_enabled", True),
            "export_log_enabled": current_run_state.get("export_log_enabled", False),
            "log_flush_delay_ms": current_run_state.get("log_flush_delay_ms", DEFAULT_SPEED_PROFILE["log_flush_delay_ms"]),
            "status_counts": dict(current_run_state.get("status_counts", {})),
            "stage_costs": dict(current_run_state.get("stage_costs", {})),
            "log_lines": list(current_run_state.get("log_lines", [])),
            "export_path": current_run_state.get("export_path", ""),
        }


def build_run_summary_text(state):
    counts = state.get("status_counts", {})
    timing_text = (
        format_top3_costs(state.get("stage_costs", {}))
        if state.get("timing_diag_enabled", True)
        else "已关闭"
    )
    lines = [
        f"状态：{state.get('status_text', '待启动')}",
        f"目标：{state.get('target_date') or '-'} | {state.get('venue_name') or '-'}",
        f"计划：{state.get('group_count', 0)} 组",
        "统计："
        f"成功 {counts.get('成功', 0)}  "
        f"已被预订 {counts.get('已被预订', 0)}  "
        f"名额已满 {counts.get('名额已满', 0)}",
        "      "
        f"验证码有误 {counts.get('验证码有误', 0)}  "
        f"Cookie失效 {counts.get('Cookie失效', 0)}  "
        f"未知返回 {counts.get('未知返回', 0)}",
        f"耗时Top3：{timing_text}",
    ]
    if state.get("last_success_event"):
        lines.append(f"最近成功：{state['last_success_event']}")
    if state.get("last_event"):
        lines.append(f"最近事件：{state['last_event']}")
    return "\n".join(lines)


def refresh_run_summary_ui():
    if "root" not in globals():
        return
    if "run_summary_var" not in globals() or "run_export_var" not in globals():
        return
    state = snapshot_run_state()
    summary_text = build_run_summary_text(state)
    if not export_log_enabled():
        export_text = "自动导出：未启用"
    else:
        export_text = state.get("export_path") or "本次任务结束后会在程序同级目录生成日志文件"

    def update_ui():
        run_summary_var.set(summary_text)
        run_export_var.set(export_text)

    root.after(0, update_ui)


def begin_run_record(target_date, venue_name, group_count, runtime_options=None):
    with run_state_lock:
        current_run_state.clear()
        current_run_state.update(create_empty_run_state())
        current_run_state["started_at"] = datetime.datetime.now()
        current_run_state["target_date"] = target_date
        current_run_state["venue_name"] = venue_name
        current_run_state["group_count"] = group_count
        current_run_state["status_text"] = "运行中"
        if runtime_options:
            current_run_state["live_log_enabled"] = bool(runtime_options.get("live_log", True))
            current_run_state["timing_diag_enabled"] = bool(runtime_options.get("timing_diag", True))
            current_run_state["export_log_enabled"] = bool(runtime_options.get("export_log", False))
            current_run_state["log_flush_delay_ms"] = int(runtime_options.get("log_flush_delay_ms", DEFAULT_SPEED_PROFILE["log_flush_delay_ms"]))
    refresh_run_summary_ui()


def append_run_log_line(formatted_line):
    with run_state_lock:
        if current_run_state.get("started_at"):
            current_run_state["log_lines"].append(formatted_line)


def record_run_event(category, detail=""):
    with run_state_lock:
        key = category if category in current_run_state["status_counts"] else "未知返回"
        current_run_state["status_counts"][key] += 1
        if detail:
            current_run_state["last_event"] = detail
            if key == "成功":
                current_run_state["last_success_event"] = detail
    refresh_run_summary_ui()


def record_stage_costs(stage_costs):
    with run_state_lock:
        merged = current_run_state["stage_costs"]
        for name, cost in (stage_costs or {}).items():
            merged[name] = merged.get(name, 0.0) + float(cost)
    refresh_run_summary_ui()

def record_attempt_snapshot(stage_costs=None, category=None, detail=""):
    with run_state_lock:
        merged = current_run_state["stage_costs"]
        if stage_costs:
            for name, cost in stage_costs.items():
                merged[name] = merged.get(name, 0.0) + float(cost)
        if category:
            key = category if category in current_run_state["status_counts"] else "未知返回"
            current_run_state["status_counts"][key] += 1
            if detail:
                current_run_state["last_event"] = detail
                if key == "成功":
                    current_run_state["last_success_event"] = detail
    refresh_run_summary_ui()


def export_run_log_snapshot(state):
    started_at = state.get("started_at")
    if not started_at:
        return ""
    if not state.get("export_log_enabled", False):
        return ""

    export_dir = resolve_log_export_dir()
    filename = "抢场日志_{stamp}_{date}_{venue}.log".format(
        stamp=started_at.strftime("%Y-%m-%d_%H-%M-%S"),
        date=sanitize_filename(state.get("target_date") or "unknown-date", "unknown-date"),
        venue=sanitize_filename((state.get("venue_name") or "unknown-venue")[:18], "unknown-venue")
    )
    export_path = os.path.join(export_dir, filename)

    summary_lines = [
        "==== 运行总结 ====",
        f"开始时间: {started_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"目标日期: {state.get('target_date') or '-'}",
        f"目标场馆: {state.get('venue_name') or '-'}",
        f"计划数量: {state.get('group_count', 0)}",
        f"当前状态: {state.get('status_text', '待启动')}",
        "状态统计: " + " | ".join(
            f"{key}={state.get('status_counts', {}).get(key, 0)}" for key in RUN_SUMMARY_KEYS
        ),
        "耗时Top3: " + (
            format_top3_costs(state.get('stage_costs', {}))
            if state.get("timing_diag_enabled", True)
            else "已关闭"
        ),
    ]
    if state.get("last_event"):
        summary_lines.append(f"最近事件: {state['last_event']}")
    summary_lines.extend(["", "==== 完整日志 ===="])

    try:
        with open(export_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(summary_lines))
            if state.get("log_lines"):
                handle.write("\n")
                handle.write("\n".join(state["log_lines"]))
                handle.write("\n")
        return export_path
    except Exception as exc:
        return f"导出失败：{exc}"


def finalize_run_record(status_text):
    state = snapshot_run_state()
    if not state.get("started_at"):
        return None

    state["status_text"] = status_text
    export_path = export_run_log_snapshot(state)
    state["export_path"] = export_path

    with run_state_lock:
        current_run_state["status_text"] = status_text
        current_run_state["export_path"] = export_path
    refresh_run_summary_ui()
    return state


def open_log_export_dir():
    folder = resolve_log_export_dir()
    try:
        os.startfile(folder)
    except Exception as exc:
        messagebox.showwarning("提示", f"打开日志目录失败：{exc}")

def flush_pending_log_lines():
    global pending_log_lines, log_flush_scheduled
    if "log_box" not in globals():
        return
    with log_buffer_lock:
        lines = pending_log_lines
        pending_log_lines = []
        log_flush_scheduled = False
    if not lines:
        return
    log_box.config(state=tk.NORMAL)
    log_box.insert(tk.END, "\n".join(lines) + "\n")
    log_box.see(tk.END)
    log_box.config(state=tk.DISABLED)

def log_msg(msg, force=False):
    global log_flush_scheduled
    if not force and not live_log_enabled():
        return
    time_str = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
    formatted_line = f"[{time_str}] {msg}"
    append_run_log_line(formatted_line)
    if "root" not in globals():
        return
    with log_buffer_lock:
        pending_log_lines.append(formatted_line)
        should_schedule = not log_flush_scheduled
        if should_schedule:
            log_flush_scheduled = True
    if should_schedule:
        try:
            flush_delay = current_log_flush_delay_ms()
            root.after(flush_delay, flush_pending_log_lines)
        except Exception:
            with log_buffer_lock:
                log_flush_scheduled = False

def live_log_enabled(runtime_options=None):
    if runtime_options is not None:
        return bool(runtime_options.get("live_log", True))
    if task_running():
        with run_state_lock:
            return bool(current_run_state.get("live_log_enabled", True))
    try:
        return bool(live_log_var.get())
    except Exception:
        return True

def timing_diag_enabled(runtime_options=None):
    if runtime_options is not None:
        return bool(runtime_options.get("timing_diag", True))
    try:
        return bool(timing_diag_var.get())
    except Exception:
        return True

def sprint_mode_enabled(runtime_options=None):
    if runtime_options is not None:
        return bool(runtime_options.get("sprint_mode", False))
    try:
        return bool(sprint_mode_var.get())
    except Exception:
        return False

def auto_next_day_enabled(runtime_options=None):
    if runtime_options is not None:
        return bool(runtime_options.get("auto_next_day", False))
    try:
        return bool(auto_next_day_var.get())
    except Exception:
        return False

def calculate_next_date_text(date_text):
    current_date = datetime.datetime.strptime((date_text or "").strip(), "%Y-%m-%d").date()
    return (current_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

def get_speed_profile(runtime_options=None):
    if sprint_mode_enabled(runtime_options):
        return dict(SPRINT_SPEED_PROFILE)
    return dict(DEFAULT_SPEED_PROFILE)

def build_runtime_options():
    profile = get_speed_profile()
    return {
        "sprint_mode": sprint_mode_enabled(),
        "auto_next_day": auto_next_day_enabled(),
        "live_log": live_log_enabled(),
        "timing_diag": timing_diag_enabled(),
        "export_log": export_log_enabled(),
        "hot_verbose": live_log_enabled(),
        "speed_profile": profile,
        "log_flush_delay_ms": profile["log_flush_delay_ms"],
        "mode_label": profile["mode_label"],
    }

def clone_runtime_options(runtime_options=None):
    cloned = dict(runtime_options or {})
    if "speed_profile" in cloned and isinstance(cloned.get("speed_profile"), dict):
        cloned["speed_profile"] = dict(cloned["speed_profile"])
    return cloned

def normalize_groups(groups):
    normalized = []
    for group in groups or []:
        if not group:
            continue
        court = str(group[0] if len(group) > 0 else "").strip()
        time1 = str(group[1] if len(group) > 1 else "").strip()
        time2 = str(group[2] if len(group) > 2 else "").strip()
        if court and court != "无" and time1 and time1 != "无":
            normalized.append((court, time1, time2))
    return normalized

def collect_groups_from_ui():
    return normalize_groups([
        (combo_court1.get(), combo_time1_1.get(), combo_time1_2.get()),
        (combo_court2.get(), combo_b1_t1.get(), combo_b1_t2.get()),
        (combo_court3.get(), combo_b2_t1.get(), combo_b2_t2.get()),
        (combo_court4.get(), combo_b3_t1.get(), combo_b3_t2.get()),
        (combo_court5.get(), combo_b4_t1.get(), combo_b4_t2.get()),
    ])

def clone_task_payload(task_payload=None):
    payload = dict(task_payload or {})
    return {
        "uid": str(payload.get("uid", "")).strip(),
        "pwd": str(payload.get("pwd", "")).strip(),
        "date_str": str(payload.get("date_str", "")).strip(),
        "venue_name": str(payload.get("venue_name", "")).strip(),
        "exec_day_str": str(payload.get("exec_day_str", "")).strip(),
        "exec_time_str": str(payload.get("exec_time_str", "")).strip(),
        "groups": normalize_groups(payload.get("groups", [])),
        "runtime_options": clone_runtime_options(payload.get("runtime_options")),
    }

def build_task_payload_from_ui(exec_day_override=None, runtime_options_override=None):
    runtime_options = clone_runtime_options(runtime_options_override or build_runtime_options())
    return {
        "uid": entry_user.get().strip(),
        "pwd": entry_pass.get().strip(),
        "date_str": entry_date.get().strip(),
        "venue_name": combo_venue.get().strip(),
        "exec_day_str": exec_day_override or combo_exec_day.get(),
        "exec_time_str": combo_exec.get().strip(),
        "groups": collect_groups_from_ui(),
        "runtime_options": runtime_options,
    }

def validate_task_payload(task_payload, suppress_validation_popups=False):
    uid = task_payload.get("uid", "")
    pwd = task_payload.get("pwd", "")
    groups = task_payload.get("groups", [])
    if not uid or not pwd:
        if suppress_validation_popups:
            log_msg("⚠️ 自动续跑未能启动：账号或密码为空。", force=True)
        else:
            messagebox.showwarning("警告", "账号和密码不能为空！")
        return False
    if not groups:
        if suppress_validation_popups:
            log_msg("⚠️ 自动续跑未能启动：至少需要设置一组有效的主战场地和时间段。", force=True)
        else:
            messagebox.showwarning("警告", "至少需要设置一组有效的主战场地和时间段！")
        return False
    return True

def set_entry_text(widget, value):
    widget.delete(0, tk.END)
    widget.insert(0, value or "")

def sync_task_payload_to_ui(task_payload):
    payload = clone_task_payload(task_payload)
    set_entry_text(entry_user, payload.get("uid", ""))
    set_entry_text(entry_pass, payload.get("pwd", ""))
    set_entry_text(entry_date, payload.get("date_str", ""))
    combo_exec_day.set(payload.get("exec_day_str", "") or "今天")
    combo_exec.set(payload.get("exec_time_str", "") or "立即执行")
    if payload.get("venue_name"):
        combo_venue.set(payload["venue_name"])
        update_combos()

    group_widgets = [
        (combo_court1, combo_time1_1, combo_time1_2),
        (combo_court2, combo_b1_t1, combo_b1_t2),
        (combo_court3, combo_b2_t1, combo_b2_t2),
        (combo_court4, combo_b3_t1, combo_b3_t2),
        (combo_court5, combo_b4_t1, combo_b4_t2),
    ]
    groups = payload.get("groups", [])
    for idx, widgets in enumerate(group_widgets):
        court_widget, time1_widget, time2_widget = widgets
        if idx < len(groups):
            court, time1, time2 = groups[idx]
            court_widget.set(court or "无")
            time1_widget.set(time1 or "无")
            time2_widget.set(time2 or "无")
        else:
            court_widget.set("无")
            time1_widget.set("无")
            time2_widget.set("无")

    runtime_options = clone_runtime_options(payload.get("runtime_options"))
    if runtime_options:
        sprint_mode_var.set(bool(runtime_options.get("sprint_mode", False)))
        auto_next_day_var.set(bool(runtime_options.get("auto_next_day", False)))
        live_log_var.set(bool(runtime_options.get("live_log", True)))
        timing_diag_var.set(bool(runtime_options.get("timing_diag", True)))
        export_log_var.set(bool(runtime_options.get("export_log", False)))
        refresh_run_summary_ui()

def hot_verbose_enabled(runtime_options=None):
    if runtime_options is not None:
        return bool(runtime_options.get("hot_verbose", True))
    return live_log_enabled()

def handle_auto_next_day_after_run(finished_date_str, exec_time_str, status_text, runtime_options=None, continuation_payload=None):
    auto_info = {
        "enabled": False,
        "scheduled": False,
        "next_date": "",
        "exec_time": exec_time_str or "",
        "reason": "",
    }
    if not auto_next_day_enabled(runtime_options) or manual_stop_requested():
        return auto_info
    try:
        next_date_text = calculate_next_date_text(finished_date_str)
    except Exception:
        log_msg(
            f"⚠️ 自动顺延失败：当前抢购日期【{finished_date_str}】不是 YYYY-MM-DD，已停止自动续跑。",
            force=True
        )
        auto_info["reason"] = "bad_date"
        return auto_info

    scheduled = exec_time_str != "立即执行"
    next_task_payload = clone_task_payload(continuation_payload)
    if (
        not next_task_payload.get("uid")
        or not next_task_payload.get("pwd")
        or not next_task_payload.get("venue_name")
        or not next_task_payload.get("groups")
    ):
        auto_info["reason"] = "missing_payload"
        log_msg("⚠️ 自动顺延失败：未能构建下一轮任务快照。", force=True)
        return auto_info
    next_task_payload["date_str"] = next_date_text
    next_task_payload["runtime_options"] = clone_runtime_options(runtime_options or next_task_payload.get("runtime_options"))
    if scheduled:
        next_task_payload["exec_day_str"] = "明天"

    auto_info.update({
        "enabled": True,
        "scheduled": False,
        "next_date": next_date_text,
        "reason": "pending",
    })
    dispatch_done = threading.Event()
    dispatch_cancelled = threading.Event()
    dispatch_result = {"started": False, "reason": ""}

    def continue_on_ui():
        try:
            if dispatch_cancelled.is_set():
                dispatch_result["reason"] = "cancelled"
                return

            sync_task_payload_to_ui(next_task_payload)
            save_config()

            if not scheduled:
                log_msg(
                    f"🤖 自动顺延已开启：日期已更新为 {next_date_text}，"
                    "但当前启动时间为“立即执行”，本轮只顺延日期，不会自动续跑。",
                    force=True
                )
                dispatch_result["reason"] = "immediate_only"
                return

            if dispatch_cancelled.is_set():
                dispatch_result["reason"] = "cancelled"
                return

            log_msg(
                f"🤖 自动顺延已开启：本轮{status_text}后，抢购日期已更新为 {next_date_text}，"
                f"下一轮将于明天 {exec_time_str} 自动继续。",
                force=True
            )
            started = bool(start_program(suppress_validation_popups=True, auto_mode=True, task_payload=next_task_payload))
            dispatch_result["started"] = started
            dispatch_result["reason"] = "scheduled" if started else "start_rejected"
        except Exception as exc:
            dispatch_result["reason"] = "ui_error"
            log_msg(f"⚠️ 自动顺延启动失败：{exc}", force=True)
        finally:
            dispatch_done.set()

    try:
        root.after(0, continue_on_ui)
    except Exception:
        auto_info["reason"] = "dispatch_failed"
        log_msg("⚠️ 自动顺延派发失败：无法将续跑任务提交到界面线程。", force=True)
        return auto_info

    if not dispatch_done.wait(timeout=5):
        dispatch_cancelled.set()
        auto_info["reason"] = "ui_timeout"
        log_msg("⚠️ 自动顺延启动超时：UI 线程未及时响应，本轮不会自动续跑。", force=True)
        return auto_info

    auto_info["scheduled"] = bool(dispatch_result["started"])
    auto_info["reason"] = dispatch_result["reason"] or ("scheduled" if auto_info["scheduled"] else auto_info["reason"])
    return auto_info

def add_stage_cost(stage_costs, name, cost):
    if stage_costs is None:
        return
    stage_costs[name] = stage_costs.get(name, 0.0) + float(cost)

def response_preview_from_bytes(body_bytes, preview_len=120):
    preview = (body_bytes or b"")[:preview_len].decode("utf-8", errors="ignore")
    return preview.replace("\n", " ").replace("\r", " ")

def body_contains_text(body_bytes, text):
    return text.encode("utf-8") in (body_bytes or b"")

def body_contains_any(body_bytes, markers):
    haystack = body_bytes or b""
    return any(marker in haystack for marker in markers)

def log_diag(msg):
    if live_log_enabled():
        log_msg(msg)

def get_current_schedule_mode():
    try:
        mode = schedule_mode_var.get()
    except Exception:
        mode = app_config.get("schedule_mode", SCHEDULE_MODE_SUMMER)
    return mode if mode in SCHEDULE_MODES else SCHEDULE_MODE_SUMMER

def get_schedule_times():
    return MORNING_TIMES + list(AFTERNOON_TIMES_BY_MODE[get_current_schedule_mode()])

def normalize_time_for_schedule(value, valid_values):
    value = value or ""
    if value in valid_values:
        return value
    mapped_value = TIME_MODE_EQUIVALENTS.get(value)
    if mapped_value in valid_values:
        return mapped_value
    return ""

def set_combo_from_saved_time(widget, value, fallback=""):
    normalized_value = normalize_time_for_schedule(value, list(widget["values"]))
    if normalized_value:
        widget.set(normalized_value)
    elif fallback:
        widget.set(fallback)

def on_schedule_mode_changed(*args):
    save_current_venue_to_memory()
    update_combos()
    save_config()

def save_current_venue_to_memory():
    global app_config, current_loaded_venue
    if current_loaded_venue:
        if "venues" not in app_config: app_config["venues"] = {}
        app_config["venues"][current_loaded_venue] = {
            "court1": combo_court1.get(), "time1_1": combo_time1_1.get(), "time1_2": combo_time1_2.get(),
            "court2": combo_court2.get(), "b1_t1": combo_b1_t1.get(), "b1_t2": combo_b1_t2.get(),
            "court3": combo_court3.get(), "b2_t1": combo_b2_t1.get(), "b2_t2": combo_b2_t2.get(),
            "court4": combo_court4.get(), "b3_t1": combo_b3_t1.get(), "b3_t2": combo_b3_t2.get(),
            "court5": combo_court5.get(), "b4_t1": combo_b4_t1.get(), "b4_t2": combo_b4_t2.get()
        }

def update_combos(*args):
    global app_config, current_loaded_venue
    venue = combo_venue.get()
    
    if current_loaded_venue and current_loaded_venue != venue:
        save_current_venue_to_memory()
        
    current_loaded_venue = venue
    
    if venue in VENUE_MAP:
        courts = VENUE_MAP[venue]['courts']
        times = get_schedule_times()
        
        combo_court1['values'] = courts
        combo_court2['values'] = ["无"] + courts
        combo_court3['values'] = ["无"] + courts
        combo_court4['values'] = ["无"] + courts
        combo_court5['values'] = ["无"] + courts
        
        combo_time1_1['values'] = times; combo_time1_2['values'] = ["无"] + times 
        combo_b1_t1['values'] = ["无"] + times; combo_b1_t2['values'] = ["无"] + times
        combo_b2_t1['values'] = ["无"] + times; combo_b2_t2['values'] = ["无"] + times
        combo_b3_t1['values'] = ["无"] + times; combo_b3_t2['values'] = ["无"] + times
        combo_b4_t1['values'] = ["无"] + times; combo_b4_t2['values'] = ["无"] + times
        
        if courts: combo_court1.current(0); combo_court2.current(0); combo_court3.current(0); combo_court4.current(0); combo_court5.current(0)
        if times:
            combo_time1_1.current(0); combo_time1_2.current(0)
            combo_b1_t1.current(0); combo_b1_t2.current(0)
            combo_b2_t1.current(0); combo_b2_t2.current(0)
            combo_b3_t1.current(0); combo_b3_t2.current(0)
            combo_b4_t1.current(0); combo_b4_t2.current(0)
            
        if "venues" in app_config and venue in app_config["venues"]:
            v_conf = app_config["venues"][venue]
            if v_conf.get("court1") in combo_court1['values']: combo_court1.set(v_conf.get("court1"))
            set_combo_from_saved_time(combo_time1_1, v_conf.get("time1_1"))
            set_combo_from_saved_time(combo_time1_2, v_conf.get("time1_2"), fallback="无")
            
            if v_conf.get("court2") in combo_court2['values']: combo_court2.set(v_conf.get("court2"))
            set_combo_from_saved_time(combo_b1_t1, v_conf.get("b1_t1"), fallback="无")
            set_combo_from_saved_time(combo_b1_t2, v_conf.get("b1_t2"), fallback="无")
            
            if v_conf.get("court3") in combo_court3['values']: combo_court3.set(v_conf.get("court3"))
            set_combo_from_saved_time(combo_b2_t1, v_conf.get("b2_t1"), fallback="无")
            set_combo_from_saved_time(combo_b2_t2, v_conf.get("b2_t2"), fallback="无")

            if v_conf.get("court4") in combo_court4['values']: combo_court4.set(v_conf.get("court4"))
            set_combo_from_saved_time(combo_b3_t1, v_conf.get("b3_t1"), fallback="无")
            set_combo_from_saved_time(combo_b3_t2, v_conf.get("b3_t2"), fallback="无")

            if v_conf.get("court5") in combo_court5['values']: combo_court5.set(v_conf.get("court5"))
            set_combo_from_saved_time(combo_b4_t1, v_conf.get("b4_t1"), fallback="无")
            set_combo_from_saved_time(combo_b4_t2, v_conf.get("b4_t2"), fallback="无")
        
        if "sync_optional_plan_cards" in globals():
            sync_optional_plan_cards()

# ==========================================
# 🛠️ 核心算法引擎
# ==========================================
def base64_to_cv2(b64_string):
    if not b64_string or ',' not in b64_string: img_b64 = b64_string
    else: img_b64 = b64_string.split(',')[1] 
    img_data = base64.b64decode(img_b64)
    nparr = np.frombuffer(img_data, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

def get_slide_distance(bg_b64, slider_b64):
    bg_img = base64_to_cv2(bg_b64)
    slider_img = base64_to_cv2(slider_b64)
    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    slider_gray = cv2.cvtColor(slider_img, cv2.COLOR_BGR2GRAY)
    bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
    slider_blur = cv2.GaussianBlur(slider_gray, (5, 5), 0)
    bg_edges = cv2.Canny(bg_blur, 100, 200)
    slider_edges = cv2.Canny(slider_blur, 100, 200)
    y_pts, x_pts = np.where(slider_edges > 0)
    if len(x_pts) > 0 and len(y_pts) > 0:
        x_min, x_max = np.min(x_pts), np.max(x_pts)
        y_min, y_max = np.min(y_pts), np.max(y_pts)
        slider_edges = slider_edges[y_min:y_max+1, x_min:x_max+1]
    else: x_min = 0
    res = cv2.matchTemplate(bg_edges, slider_edges, cv2.TM_CCOEFF_NORMED)
    _, _, _, max_loc = cv2.minMaxLoc(res)
    return max_loc[0] - x_min

def generate_human_tracks(distance, speed_profile=None):
    speed_profile = speed_profile or get_speed_profile()
    tracks = []
    target_t = random.randint(speed_profile["track_target_min_ms"], speed_profile["track_target_max_ms"])
    
    current_t = random.randint(speed_profile["track_down_min_ms"], speed_profile["track_down_max_ms"])
    tracks.append({"x": 0, "y": 0, "type": "down", "t": current_t})
    
    end_pause = random.randint(speed_profile["track_end_pause_min_ms"], speed_profile["track_end_pause_max_ms"])
    
    slide_time_allowed = target_t - current_t - end_pause
    if slide_time_allowed < speed_profile["min_slide_time_ms"]:
        slide_time_allowed = speed_profile["min_slide_time_ms"]
    
    if distance < 85:
        estimated_steps = distance / 1.5 
    else:
        estimated_steps = distance / 3.0 

    if estimated_steps < 1: estimated_steps = 1
    time_per_step = slide_time_allowed / estimated_steps
    
    current_x = 0
    current_y = 0
    
    while current_x < distance:
        step_t = max(2, int(random.gauss(time_per_step, time_per_step / 2)))
        current_t += step_t
        
        if distance < 85:
            step_x = random.choice([1, 1, 2]) 
        else:
            step_x = random.choice([1, 2, 3, 4]) 
            
        current_x += step_x
        if current_x > distance: current_x = distance
        
        if random.random() < 0.2: 
            current_y += random.choice([-1, 1, 0]) 
            
        if current_y > 5: current_y = 5
        if current_y < -5: current_y = -5
        
        tracks.append({"x": current_x, "y": current_y, "type": "move", "t": current_t})
        
    if current_t < target_t - end_pause:
        current_t = target_t - end_pause
        tracks.append({"x": current_x, "y": current_y, "type": "move", "t": current_t})
        
    current_t += end_pause
    tracks.append({"x": int(distance), "y": current_y, "type": "up", "t": current_t})
    
    return tracks, current_t

def extract_time_slots(time1, time2):
    slots = []
    if time1 and time1 != "无":
        slots.append(time1)
    if time2 and time2 != "无":
        slots.append(time2)
    return slots

def find_next_single_slot_group(groups, start_idx, booked_slots):
    for idx in range(start_idx + 1, len(groups)):
        _, next_t1, next_t2 = groups[idx]
        next_slots = extract_time_slots(next_t1, next_t2)
        if len(next_slots) != 1:
            continue
        if next_slots[0] in booked_slots:
            continue
        return idx
    return None

def summarize_http_response(resp, preview_len=120, body_bytes=None, payload=None):
    content_type = resp.headers.get("Content-Type", "")
    body_bytes = resp.content if body_bytes is None else body_bytes
    summary = f"ctype={content_type or '未知'}, bytes={len(body_bytes or b'')}"
    try:
        if payload is None:
            payload = json.loads(body_bytes or b"{}")
        obj = payload.get("object")
        if isinstance(obj, list):
            obj_desc = f"list[{len(obj)}]"
        elif isinstance(obj, dict):
            obj_desc = f"dict_keys={list(obj.keys())[:5]}"
        elif obj is None:
            obj_desc = "null"
        else:
            obj_desc = type(obj).__name__
        return f"{summary}, result={payload.get('result')}, message={payload.get('message')}, object={obj_desc}"
    except Exception:
        preview = response_preview_from_bytes(body_bytes, preview_len)
        return f"{summary}, preview={preview}"

def summarize_stage_costs(stage_costs, runtime_options=None):
    if not stage_costs or not timing_diag_enabled(runtime_options):
        return
    if not hot_verbose_enabled(runtime_options):
        return
    ordered = [(name, cost) for name, cost in stage_costs.items() if cost > 0]
    if not ordered:
        return
    detail_line = " | ".join(f"{name}={cost:.3f}s" for name, cost in ordered)
    top_line = ", ".join(f"{name}={cost:.3f}s" for name, cost in sorted(ordered, key=lambda item: item[1], reverse=True)[:3])
    log_diag(f"🧪 [DEBUG-耗时拆解] {detail_line}")
    log_diag(f"🧪 [DEBUG-耗时Top3] {top_line}")

#只要交大不踢我，我就死皮赖脸连着不断。如果交大踢了我（断线了），我认栽，我花 50ms 重新连一次,和之前代码一样。但我连上之后，接下来的验证码和发单，我全都要“白嫖” 0 毫秒的极速通道！
def ensure_api_runtime_context(driver, venue_id, api_context=None, measure_costs=True):
    if api_context and api_context.get("session") and api_context.get("venue_id") == str(venue_id):
        return api_context, 0.0

    ua_start = time.time() if measure_costs else None
    real_user_agent = driver.execute_script("return navigator.userAgent;")
    ua_cost = (time.time() - ua_start) if measure_costs else 0.0

    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return {
        "session": session,
        "venue_id": str(venue_id),
        "user_agent": real_user_agent,
        "base_headers": {
            "User-Agent": real_user_agent,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "http://202.117.17.144:8080",
            "Referer": f"http://202.117.17.144:8080/web/product/show.html?id={venue_id}",
        },
        "cookie_fingerprint": None,
    }, ua_cost

def sync_session_cookies(api_context, saved_cookies, measure_costs=True):
    session = api_context["session"]
    cookie_fingerprint = tuple(
        (
            cookie.get("name", ""),
            cookie.get("value", ""),
            cookie.get("domain", ""),
            cookie.get("path", "/"),
        )
        for cookie in (saved_cookies or [])
        if cookie.get("name")
    )
    if api_context.get("cookie_fingerprint") == cookie_fingerprint:
        return len(cookie_fingerprint), 0.0

    cookie_load_start = time.time() if measure_costs else None
    for name, value, domain, path in cookie_fingerprint:
        cookie_kwargs = {"path": path or "/"}
        if domain:
            cookie_kwargs["domain"] = domain
        session.cookies.set(name, value, **cookie_kwargs)
    api_context["cookie_fingerprint"] = cookie_fingerprint
    return len(cookie_fingerprint), ((time.time() - cookie_load_start) if measure_costs else 0.0)

# ==========================================
# 🌟 API 引擎：智能风控防线 + 人类拟态发包
# ==========================================
def pure_api_booking(driver, target_date, target_court, time1, time2, venue_id, saved_cookies, api_context=None, runtime_options=None):
    start_time_stopwatch = time.time()  
    hot_verbose = hot_verbose_enabled(runtime_options)
    diag_enabled = timing_diag_enabled(runtime_options)
    if hot_verbose:
        log_msg(f"⚡ 载入 [ID:{venue_id}] 场地【{target_court}】战斗数据...")
    speed_profile = get_speed_profile(runtime_options)
    stage_costs = ({
        "UA获取": 0.0,
        "Cookie装载": 0.0,
        "库存查询网络": 0.0,
        "库存解析匹配": 0.0,
        "库存等待重试": 0.0,
        "验证码请求": 0.0,
        "验证码解析": 0.0,
        "缺口识别": 0.0,
        "轨迹生成": 0.0,
        "参数组包": 0.0,
        "拟态等待": 0.0,
        "发包补时": 0.0,
        "订单提交": 0.0,
    } if diag_enabled else None)
    
    def finish_attempt(result_code, summary_category=None, detail=""):
        record_attempt_snapshot(stage_costs, summary_category, detail)
        return result_code

    if stop_requested():
        return finish_attempt("STOPPED", None, "手动停止")

    api_context, ua_cost = ensure_api_runtime_context(driver, venue_id, api_context, measure_costs=diag_enabled)#保证当前会话在线
    add_stage_cost(stage_costs, "UA获取", ua_cost)
    session = api_context["session"]
    headers_json = dict(api_context["base_headers"])
    cookie_count, cookie_cost = sync_session_cookies(api_context, saved_cookies, measure_costs=diag_enabled)#判断cookie是否重复 并装在cookie
    add_stage_cost(stage_costs, "Cookie装载", cookie_cost)
    
    if cookie_count == 0:
        log_msg("❌ [致命拦截] 手环数量为 0！瞬间切断，立刻呼叫重载！", force=True)
        return finish_attempt("COOKIE_INVALID", "Cookie失效", "未拿到有效 Cookie")

    # 步骤 1: 监控库存 
    if hot_verbose:
        log_msg(f"👉 [战术扫描] 探测场地【{target_court}】库存暗号 ({time1} & {time2})...")
    find_url = f"http://202.117.17.144:8080/web/product/findOkArea.html?s_date={target_date}&serviceid={venue_id}"
    
    is_double_booking = (time2 != "无" and time2 != "")
    required_count = 2 if is_double_booking else 1
    stock_dict = {} 
    
    for attempt in range(8):
        if stop_requested():
            summarize_stage_costs(stage_costs, runtime_options)
            return finish_attempt("STOPPED", None, "手动停止")
        try:
            find_start = time.time() if diag_enabled else None
            resp_area = session.get(find_url, headers=headers_json, timeout=3, allow_redirects=True)
            if diag_enabled:
                add_stage_cost(stage_costs, "库存查询网络", time.time() - find_start)
            area_body = resp_area.content or b""
            if hot_verbose:
                log_msg(f"🔍 [DEBUG-找库存] 状态码: {resp_area.status_code}")
                log_diag(
                    f"🧪 [DEBUG-找库存详情] 响应头: {dict(resp_area.headers)}, "
                    f"前100字符: {response_preview_from_bytes(area_body, 100)}"
                )
            
            if body_contains_any(area_body, AREA_BLOCK_MARKERS):
                log_msg(f"🚨 [最高警报] 触发交大防火墙！账号异常！强行终止本轮避免死封！", force=True)
                return finish_attempt("BLOCKED", "未知返回", "库存查询阶段触发异常拦截") 

            parse_start = time.time() if diag_enabled else None
            area_data = json.loads(area_body)
            stock_dict.clear() 
            target_candidates = [] if hot_verbose else None
            
            for item in area_data.get('object', []):
                if item.get('sname') == target_court:
                    current_t_no = item.get('stock', {}).get('time_no')
                    if hot_verbose:
                        target_candidates.append({
                            "time_no": current_t_no,
                            "stockid": str(item.get('stockid')),
                            "id": str(item.get('id'))
                        })
                    if current_t_no == time1:
                        stock_dict[str(item.get('stockid'))] = str(item.get('id'))
                    elif is_double_booking and current_t_no == time2:
                        stock_dict[str(item.get('stockid'))] = str(item.get('id'))
            if diag_enabled:
                add_stage_cost(stage_costs, "库存解析匹配", time.time() - parse_start)

            if hot_verbose:
                target_candidates.sort(key=lambda item: str(item.get("time_no")))
                candidate_preview = target_candidates[:6]
                log_diag(
                    f"🧪 [DEBUG-库存画像] attempt={attempt+1}, object_count={len(area_data.get('object') or [])}, "
                    f"target_rows={len(target_candidates)}, 候选={candidate_preview}"
                )
            
            if len(stock_dict) == required_count:
                if hot_verbose:
                    log_msg(f"   🎯 【{target_court}】暗号捕获成功！")
                    log_msg(f"🔍 [DEBUG-暗号详情] {stock_dict}")
                    log_diag(
                        f"🧪 [DEBUG-暗号判断] 目标时段={extract_time_slots(time1, time2)}, "
                        f"命中的stock_dict={stock_dict}"
                    )
                break
            else:
                sleep_time = random.uniform(
                    speed_profile["inventory_retry_min_s"],
                    speed_profile["inventory_retry_max_s"]
                ) + (attempt * speed_profile["inventory_retry_step_s"])
                add_stage_cost(stage_costs, "库存等待重试", sleep_time)
                if sleep_with_stop(sleep_time):
                    summarize_stage_costs(stage_costs, runtime_options)
                    return finish_attempt("STOPPED", None, "手动停止")
        except Exception as e: 
            if hot_verbose:
                log_msg(f"🔍 [DEBUG-找库存] 解析失败或超时: {e}")
            retry_sleep = random.uniform(
                speed_profile["inventory_error_retry_min_s"],
                speed_profile["inventory_error_retry_max_s"]
            )
            add_stage_cost(stage_costs, "库存等待重试", retry_sleep)
            if sleep_with_stop(retry_sleep):
                summarize_stage_costs(stage_costs, runtime_options)
                return finish_attempt("STOPPED", None, "手动停止")

    if len(stock_dict) != required_count:
        log_msg(f"❌ 场地【{target_court}】此时间未放号或已被抢空！切下一个！", force=True)
        summarize_stage_costs(stage_costs, runtime_options)
        return finish_attempt("NEXT_GROUP", "已被预订", f"{target_court} 未放号或已被抢空")

    # 步骤 2: 请求验证码
    gen_headers = headers_json.copy()
    gen_headers["Referer"] = "http://202.117.17.144:8080/yzm/slider.html"
    gen_url = "http://202.117.17.144:8080/gen"
    
    try:
        if stop_requested():
            summarize_stage_costs(stage_costs, runtime_options)
            return finish_attempt("STOPPED", None, "手动停止")
        gen_start = time.time() if diag_enabled else None
        resp_gen = session.get(gen_url, headers=gen_headers, timeout=5)
        if diag_enabled:
            add_stage_cost(stage_costs, "验证码请求", time.time() - gen_start)
        gen_body = resp_gen.content or b""
        if hot_verbose:
            log_msg(f"🔍 [DEBUG-验证码API] 状态码: {resp_gen.status_code}")
        gen_parse_start = time.time() if diag_enabled else None
        gen_data = json.loads(gen_body)
        if hot_verbose:
            log_diag(f"🧪 [DEBUG-gen画像] {summarize_http_response(resp_gen, body_bytes=gen_body, payload=gen_data)}")
        captcha_id = gen_data.get('id')
        if hot_verbose:
            log_diag(f"🔍 [DEBUG-验证码ID] {captcha_id}")
        captcha_node = gen_data.get('captcha') or gen_data.get('captchha', {})
        if diag_enabled:
            add_stage_cost(stage_costs, "验证码解析", time.time() - gen_parse_start)
        gap_start = time.time() if diag_enabled else None
        distance = get_slide_distance(captcha_node.get('backgroundImage'), captcha_node.get('sliderImage'))
        actual_d = int(distance * (260 / captcha_node.get('backgroundImageWidth', 590)))
        if diag_enabled:
            add_stage_cost(stage_costs, "缺口识别", time.time() - gap_start)
    except Exception as e:
        log_msg(f"⚠️ [意外防御] 验证码获取异常，死守手环，瞬间重试API！报错: {e}", force=True)
        summarize_stage_costs(stage_costs, runtime_options)
        return finish_attempt("API_RETRY", "未知返回", f"验证码阶段异常: {e}") 

    track_start = time.time() if diag_enabled else None
    tracks, final_t = generate_human_tracks(actual_d, speed_profile)
    if diag_enabled:
        add_stage_cost(stage_costs, "轨迹生成", time.time() - track_start)
    add_stage_cost(stage_costs, "拟态等待", final_t / 1000.0)
    if hot_verbose:
        log_msg(f"🧩 缺口计算完成: 需滑动 {actual_d}px，拟态休眠注入: {final_t/1000.0:.3f} 秒")
    pack_start = time.time() if diag_enabled else None
    now_utc = datetime.datetime.utcnow()
    end_iso = now_utc.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    start_iso = (now_utc - datetime.timedelta(milliseconds=final_t)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    param_payload = {"stockdetail": stock_dict, "venueReason": "", "fileUrl": "", "address": str(venue_id)}
    yzm_payload = {"bgImageWidth": 260, "bgImageHeight": 0, "sliderImageWidth": 0, "sliderImageHeight": 159, "startSlidingTime": start_iso, "entSlidingTime": end_iso, "trackList": tracks}
    
    post_data = {
        "param": json.dumps(param_payload, separators=(',', ':')),
        "yzm": json.dumps(yzm_payload, separators=(',', ':')) + f"synjones{captcha_id}synjoneshttp://202.117.17.144:8071",
        "json": "true"
    }
    if diag_enabled:
        add_stage_cost(stage_costs, "参数组包", time.time() - pack_start)
    
    if hot_verbose:
        log_diag(f"🔍 [DEBUG-发包准备] param_payload: {param_payload}")
        log_diag(f"🔍 [DEBUG-发包准备] 轨迹点数量: {len(tracks)}")
    
    if sleep_with_stop(final_t / 1000.0, interval=0.05):
        summarize_stage_costs(stage_costs, runtime_options)
        return finish_attempt("STOPPED", None, "手动停止")

    # 步骤 3: 提交订单
    submit_url = "http://202.117.17.144:8080/web/order/tobook.html"
    headers_json["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    current_cost = time.time() - start_time_stopwatch
    if hot_verbose:
        log_msg(f"发包前的时间: {current_cost:.2f} 秒！")
    if hot_verbose and stage_costs:
        pre_submit_accounted = sum(cost for name, cost in stage_costs.items() if name != "订单提交")
        log_diag(
            f"🧪 [DEBUG-发包前差额] 已统计={pre_submit_accounted:.3f}s, "
            f"未归类={max(0.0, current_cost - pre_submit_accounted):.3f}s"
        )
    submit_floor = random.uniform(speed_profile["submit_floor_min_s"], speed_profile["submit_floor_max_s"])
    if current_cost < submit_floor:
        wait_before_submit = submit_floor - current_cost
        add_stage_cost(stage_costs, "发包补时", wait_before_submit)
        if sleep_with_stop(wait_before_submit, interval=0.05):
            summarize_stage_costs(stage_costs, runtime_options)
            return finish_attempt("STOPPED", None, "手动停止")

    submit_start_time = time.time()
    try:
        if stop_requested():
            summarize_stage_costs(stage_costs, runtime_options)
            return finish_attempt("STOPPED", None, "手动停止")
        resp_submit = session.post(submit_url, data=post_data, headers=headers_json, timeout=8, allow_redirects=False)
        submit_cost = time.time() - submit_start_time
        add_stage_cost(stage_costs, "订单提交", submit_cost)
        submit_body = resp_submit.content or b""
        
        if hot_verbose:
            log_msg(f"📡 订单提交网络延迟 (交大服务器响应耗时): {submit_cost:.3f} 秒") 
        if hot_verbose:
            log_msg(f"🔍 [DEBUG-最终开奖] HTTP 状态码: {resp_submit.status_code}")
            log_msg(f"🔍 [DEBUG-最终开奖] 摘要: {summarize_http_response(resp_submit, preview_len=150, body_bytes=submit_body)}")
            log_diag(f"🔍 [DEBUG-最终开奖] 响应头: {dict(resp_submit.headers)}")
            log_diag(f"🔍 [DEBUG-最终开奖] 响应体前300字: {response_preview_from_bytes(submit_body, 300).strip()}")
        
        if resp_submit.status_code == 302:
            loc = resp_submit.headers.get('Location', '')
            if 'login' in loc or 'cas' in loc or 'index' in loc:
                log_msg("❌ [死因确诊] Cookie 彻底失效被踢出！立即重载！", force=True)
                return finish_attempt("COOKIE_INVALID", "Cookie失效", "提交时被重定向到登录页")
            else:
                log_msg("🚨 服务器拥堵触发异常 302 跳转！死守手环，瞬间重砸 API！", force=True)
                return finish_attempt("API_RETRY", "未知返回", "提交时触发异常 302 跳转")
                
        if resp_submit.status_code >= 500 or not submit_body.strip() or body_contains_any(submit_body, EMPTY_BODY_MARKERS):
            log_msg("🚨 服务器被挤爆了！返回宕机空包！死守手环，瞬间重砸 API！", force=True)
            return finish_attempt("API_RETRY", "未知返回", "提交阶段服务端异常或空响应")

    except Exception as e:
        log_msg(f"❌ 订单发送网络超时，死守手环，瞬间重试 API！报错: {e}", force=True)
        summarize_stage_costs(stage_costs, runtime_options)
        return finish_attempt("API_RETRY", "未知返回", f"提交阶段网络异常: {e}")
        
    total_cost = time.time() - start_time_stopwatch
    log_msg(f"⏱️ 战斗耗时: {total_cost:.2f} 秒！", force=True)
    summarize_stage_costs(stage_costs, runtime_options)
    if hot_verbose and stage_costs:
        total_accounted = sum(stage_costs.values())
        log_diag(
            f"🧪 [DEBUG-总差额] 已统计={total_accounted:.3f}s, "
            f"未归类={max(0.0, total_cost - total_accounted):.3f}s"
        )
    
    if body_contains_any(submit_body, JSON_TRUE_RESULT_MARKERS):
         log_msg("🎉🎉🎉 神级秒杀成功！场地已到手！", force=True)
         return finish_attempt("SUCCESS", "成功", f"{target_court} {time1} 预订成功")
    elif SUBMIT_MESSAGE_MARKERS["LIMIT_REACHED"] in submit_body:
         log_msg("🎉 逻辑执行完美！(今日名额已满)", force=True)
         return finish_attempt("LIMIT_REACHED", "名额已满", "本天预订数量超过限制") 
         
    elif (
        SUBMIT_MESSAGE_MARKERS["TIME_NOT_OPEN"] in submit_body
        or SUBMIT_MESSAGE_MARKERS["DATE_NOT_OPEN"] in submit_body
        or SUBMIT_MESSAGE_MARKERS["NOT_BOOKABLE"] in submit_body
    ):
         log_msg("⚠️ 交大后台服务器时间慢半拍！提示未到时间，全速 API 重冲！", force=True)
         return finish_attempt("API_RETRY", "未知返回", "服务端返回未到可预约时间") 
    elif SUBMIT_MESSAGE_MARKERS["BAD_CAPTCHA"] in submit_body:
         log_msg("⚠️ 图像识别偶发失误，死守手环，极速重拿新图！", force=True)
         return finish_attempt("API_RETRY", "验证码有误", "验证码校验失败")  
         
    elif SUBMIT_MESSAGE_MARKERS["BAD_DATA"] in submit_body:
         log_msg(f"❌ 靠，慢了半步，【{target_court}】被别人锁定了！立刻切枪！", force=True)
         return finish_attempt("NEXT_GROUP", "已被预订", f"{target_court} 已被他人锁定") 
    elif SUBMIT_MESSAGE_MARKERS["ALREADY_BOOKED"] in submit_body:
         log_msg(f"❌ 靠，【{target_court}】被抢先了！立刻切枪！", force=True)
         return finish_attempt("NEXT_GROUP", "已被预订", f"{target_court} 已被预订") 
         
    elif SUBMIT_MESSAGE_MARKERS["NOT_LOGIN"] in submit_body:
         log_msg("❌ [死因确诊] Cookie 明确失效！开启网页重载！", force=True)
         return finish_attempt("COOKIE_INVALID", "Cookie失效", "服务端判定未登录")
         
    else:
         raw_text = response_preview_from_bytes(submit_body, 150).strip()
         if not raw_text:
             log_msg("⚠️ 服务器返回了空气 (空字符串)！死守手环，极速重冲 API！", force=True)
         else:
             log_msg(f"⚠️ 未知拦截！原话是: {raw_text} -> 死守手环重冲 API！", force=True)
         return finish_attempt("API_RETRY", "未知返回", raw_text or "空响应")

# ==========================================
# ⚙️ 守护进程：WAF规避 + B计划无缝切换 
# ==========================================
def automation_task(uid, pwd, date_str, venue_name, groups, exec_day_str, exec_time_str, runtime_options):
    driver = None
    api_context = None
    navigator = None
    run_status_text = "已完成"
    success_obtained = False
    try:
        hot_verbose = hot_verbose_enabled(runtime_options)
        speed_profile = get_speed_profile(runtime_options)
        target_time = None
        if exec_time_str != "立即执行":
            try:
                now = datetime.datetime.now()
                days_offset = {"今天": 0, "明天": 1, "后天": 2}.get(exec_day_str, 0)
                target_date = now.date() + datetime.timedelta(days=days_offset)
                
                if exec_time_str.count(':') == 1:
                    parsed_time = datetime.datetime.strptime(exec_time_str, "%H:%M").time()
                else:
                    parsed_time = datetime.datetime.strptime(exec_time_str, "%H:%M:%S").time()
                
                target_time = datetime.datetime.combine(target_date, parsed_time)
                pre_start_time = target_time - datetime.timedelta(seconds=20)
                
                if now < pre_start_time:
                    log_msg(f"💤 守护进程进入静默期，将于 {pre_start_time.strftime('%H:%M:%S')} 唤醒...", force=True)
                    while datetime.datetime.now() < pre_start_time:
                        if sleep_with_stop(1):
                            run_status_text = "已手动停止"
                            return
                log_msg(f"⚡ 极速点火！启动浏览器...", force=True)
            except Exception as e:
                log_msg(f"⚠️ 定时格式错误，将立即执行！", force=True)

        if stop_requested():
            run_status_text = "已手动停止"
            return
                
        venue_id = VENUE_MAP[venue_name]["id"]
        
        options = webdriver.EdgeOptions()
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.page_load_strategy = 'eager'

        driver = webdriver.Edge(options=options)
        set_active_driver(driver)
        driver.maximize_window()
        
        def login_log(message):
            log_msg(message, force=True)
            if "人工" in message:
                root.after(0, restore_from_tray)

        driver.set_page_load_timeout(30)
        driver.set_script_timeout(10)
        navigator = LoginNavigator(
            driver, log=login_log, stopped=stop_requested,
            base_dir=get_app_base_dir(), secrets=(uid, pwd), manual_wait=120
        )
        login_started = time.perf_counter()
        navigator.prepare(uid, pwd)
        record_stage_costs({"登录与场馆入口": time.perf_counter() - login_started})
        if stop_requested():
            raise LoginCancelled()
        log_msg("✅ 已确认体育场馆预约首页，登录流程完成。", force=True)
        log_msg(">>> 进入目标场馆...", force=True)

        target_venue_url = f"http://202.117.17.144:8080/web/product/show.html?id={venue_id}"
        driver.get(target_venue_url)
        
        for _ in range(4): 
            if stop_requested():
                run_status_text = "已手动停止"
                return
            saved_cookies = driver.get_cookies()
            if any(c['name'] == 'SESSION' for c in saved_cookies):
                log_msg("✅ 首次进场：手环获取成功，准备就绪！", force=True)
                if hot_verbose:
                    log_msg(f"🔍 [DEBUG-初次Cookie数量] {len(saved_cookies)}")
                break 
            if sleep_with_stop(speed_profile["initial_cookie_poll_sleep_s"]):
                run_status_text = "已手动停止"
                return
        else:
            log_msg("⚠️ 警告：进场未拿到 SESSION，8:40 可能会触发应急重载...", force=True)

        # 🌟 终极防线：【死守 8:40 准点】
        if target_time and datetime.datetime.now() < target_time:
            log_msg(f"🎯 潜伏确认！死守 {target_time.strftime('%H:%M:%S')} 准点爆发...", force=True)
            
            while datetime.datetime.now() < target_time:
                if sleep_with_stop(0.05, interval=0.05):
                    run_status_text = "已手动停止"
                    return
            
            log_msg("🔄 [战术执行] 跨过 8:40 警戒线，开始执行破门突入！", force=True)
            
            for attempt in range(15):
                if stop_requested():
                    run_status_text = "已手动停止"
                    return
                driver.get(target_venue_url) 
                if sleep_with_stop(speed_profile["gate_probe_sleep_s"]):
                    run_status_text = "已手动停止"
                    return
                
                current_url = driver.current_url
                page_text = driver.page_source
                
                if hot_verbose:
                    log_msg(f"🔍 [DEBUG-302突击] 尝试第 {attempt+1} 次, 当前URL: {current_url}")
                
                if "errorpage" in current_url or "8:40-21:40再来" in page_text or "未到该日期" in page_text or "不在可预订" in page_text:
                    log_msg(
                        f"⚠️ 第 {attempt+1} 次突入失败！交大服务器仍未放行 (302拦截)，"
                        f"{speed_profile['gate_retry_sleep_s']:.2f}秒后重冲..."
                    )
                    if sleep_with_stop(speed_profile["gate_retry_sleep_s"]):
                        run_status_text = "已手动停止"
                        return
                    continue 
                
                try:
                    alert = driver.switch_to.alert
                    if "8:40" in alert.text or "未到" in alert.text or "时间" in alert.text:
                        alert.accept()
                        log_msg("⚠️ 斩断时间拦截弹窗！继续强攻...")
                        if sleep_with_stop(speed_profile["gate_retry_sleep_s"]):
                            run_status_text = "已手动停止"
                            return
                        continue
                except:
                    pass 

                log_msg("✅ 成功突破 302 重定向封锁！稳稳扎根主阵地！", force=True)
                break 
            
            saved_cookies = driver.get_cookies()
            if len(saved_cookies) == 0:
                log_msg("⚠️ 警告：准点突入未拿到手环，将交由应急重载系统处理...", force=True)
        api_context, _ = ensure_api_runtime_context(
            driver, venue_id, api_context, measure_costs=timing_diag_enabled(runtime_options)
        )
        if runtime_options.get("sprint_mode"):
            log_msg("⚙️ 已开启冲刺模式：连接复用与更激进的等待参数已生效。", force=True)
        else:
            log_msg("⚙️ 连接复用已开启，抢场热路径加速。", force=True)

        # 🌟 瀑布流火控系统
        MAX_RETRIES = 15 
        attempt = 0
        group_idx = 0
        need_recovery = False
        booked_time_slots = set()

        while attempt < MAX_RETRIES and group_idx < len(groups):
            if stop_requested():
                run_status_text = "已手动停止"
                break
            cur_court, cur_t1, cur_t2 = groups[group_idx]
            cur_slots = extract_time_slots(cur_t1, cur_t2)
            
            if need_recovery:
                log_msg(f"🔄 [应急重载] 识别到 Cookie 彻底失效，准备夺回控制权...")
                for fast_retry in range(5):
                    if stop_requested():
                        run_status_text = "已手动停止"
                        break
                    try:
                        driver.get(target_venue_url)
                        if hot_verbose:
                            log_msg(f"🔍 [DEBUG-重载雷达] 请求网址: {driver.current_url}")
                        found_cookie = False
                        for _ in range(5): 
                            if stop_requested():
                                run_status_text = "已手动停止"
                                break
                            saved_cookies = driver.get_cookies()
                            if any(cookie['name'] == 'SESSION' for cookie in saved_cookies):
                                found_cookie = True
                                break 
                            if sleep_with_stop(speed_profile["recovery_cookie_sleep_s"]):
                                run_status_text = "已手动停止"
                                break 
                        
                        if run_status_text == "已手动停止":
                            break
                        if found_cookie:
                            log_msg("✅ [极速重载成功] 已重新获取手环，恢复交火！", force=True)
                            need_recovery = False 
                            break 
                        else:
                            log_msg(f"   ⚠️ 第 {fast_retry+1} 次重载未获手环，继续强攻...")
                            
                    except Exception as e:
                        if hot_verbose:
                            log_msg(f"🔍 [DEBUG-重载异常] {e}")
                        if sleep_with_stop(speed_profile["recovery_error_sleep_s"]):
                            run_status_text = "已手动停止"
                            break
                if run_status_text == "已手动停止":
                    break
                else:
                    log_msg("❌ 抢救失败：连续 5 次重载强刷彻底掉线，停止攻击避免封号。", force=True)
                    break 

            if attempt == 0:
                log_msg(f"🚀 发起第 {group_idx+1} 组总攻: 【{cur_court}】 {cur_t1} & {cur_t2}", force=True)
            
            battle_result = pure_api_booking(
                driver, date_str, cur_court, cur_t1, cur_t2, venue_id, saved_cookies,
                api_context=api_context, runtime_options=runtime_options
            )

            if battle_result == "STOPPED":
                run_status_text = "已手动停止"
                break
            
            # 🎯 指挥中心的终极判决 🎯
            if battle_result == "SUCCESS":
                success_obtained = True
                booked_time_slots.update(cur_slots)
                if len(cur_slots) != 1 or len(booked_time_slots) >= 2:
                    break

                next_idx = find_next_single_slot_group(groups, group_idx, booked_time_slots)
                if next_idx is None:
                    log_msg("✅ 已拿下 1 个单时段，后续没有不重复的单时段备选，任务收束。", force=True)
                    break

                log_msg(f"✅ 已拿下单时段【{cur_t1}】！继续补抢后续不重复的单时段...", force=True)
                group_idx = next_idx
                need_recovery = False
                attempt = 0
                continue
                
            elif battle_result == "LIMIT_REACHED":
                break 
                
            elif battle_result == "COOKIE_INVALID":
                need_recovery = True 
                attempt += 1
                
            elif battle_result == "API_RETRY":
                need_recovery = False 
                attempt += 1
                
            elif battle_result == "NEXT_GROUP":
                need_recovery = False 
                attempt = 0 
                if booked_time_slots:
                    next_idx = find_next_single_slot_group(groups, group_idx, booked_time_slots)
                else:
                    next_idx = group_idx + 1 if group_idx + 1 < len(groups) else None

                if next_idx is not None:
                    group_idx = next_idx
                    log_msg(f"⚠️ 启动备用计划！瞬间切枪至第 {group_idx+1} 编队...", force=True)
                elif booked_time_slots:
                    log_msg("⚠️ 已有一个成功单时段，但后续没有可继续补抢的不重复单时段计划。", force=True)
                    break
                else:
                    log_msg("❌ 所有备用计划均已战败，防线彻底沦陷！", force=True)
                    break
                    
            elif battle_result == "BLOCKED":
                log_msg("🛑 任务已被系统级熔断，请稍后手动登录解除限制！", force=True)
                run_status_text = "已中止"
                break
                
            elif battle_result == "FAILED":
                break 

        if success_obtained:
            run_status_text = "已完成（有成功）"
            if auto_next_day_enabled(runtime_options):
                log_msg("✅ 本轮已有成功结果，因已开启自动顺延，将直接准备下一天任务。", force=True)

        log_msg(">>> 任务完成，清理战场...", force=True)
        
    except LoginCancelled:
        run_status_text = "已手动停止"
        log_msg("🛑 登录或页面跳转已按停止指令结束。", force=True)
    except LoginFlowError as exc:
        run_status_text = "已手动停止" if stop_requested() else "登录未完成"
        log_msg(f"⚠️ 登录流程停止：{exc}", force=True)
        with run_state_lock:
            current_run_state["last_event"] = str(exc)
        if navigator is not None:
            try:
                diagnostic_path = navigator.save_diagnostics(exc.stage, exc)
                log_msg(f"🧪 脱敏诊断已保存：{diagnostic_path}", force=True)
            except Exception as diagnostic_exc:
                log_msg(f"⚠️ 诊断文件保存失败：{type(diagnostic_exc).__name__}", force=True)
    except Exception as e:
        run_status_text = "已手动停止" if stop_requested() else "异常结束"
        log_msg("❌ 运行发生错误:", force=True)
        if navigator is not None:
            log_msg(f"{type(e).__name__}（详细页面状态见登录诊断文件）", force=True)
            try:
                diagnostic_path = navigator.save_diagnostics("运行异常", e)
                log_msg(f"🧪 脱敏诊断已保存：{diagnostic_path}", force=True)
            except Exception:
                pass
        else:
            log_msg(traceback.format_exc(), force=True)
    finally:
        try: 
            if driver: driver.quit()
        except: pass
        clear_active_driver(driver)
        try:
            if api_context and api_context.get("session"):
                api_context["session"].close()
        except:
            pass
        final_run_state = finalize_run_record(run_status_text)
        mark_task_idle()
        auto_next_day_info = {
            "enabled": False,
            "scheduled": False,
            "next_date": "",
            "exec_time": "",
            "reason": "",
        }
        if run_status_text not in {"已手动停止", "登录未完成"}:
            auto_next_day_info = handle_auto_next_day_after_run(
                date_str,
                exec_time_str,
                run_status_text,
                runtime_options=runtime_options,
                continuation_payload={
                    "uid": uid,
                    "pwd": pwd,
                    "date_str": date_str,
                    "venue_name": venue_name,
                    "exec_day_str": exec_day_str,
                    "exec_time_str": exec_time_str,
                    "groups": groups,
                    "runtime_options": runtime_options,
                }
            )
        notify_task_outcome(
            date_str,
            venue_name,
            run_status_text,
            success_obtained=success_obtained,
            auto_next_day_info=auto_next_day_info,
            final_state=final_run_state
        )
        if not auto_next_day_info.get("scheduled", False):
            try:
                root.after(0, lambda: update_task_buttons(False))
            except Exception:
                update_task_buttons(False)

def start_program(exec_day_override=None, suppress_validation_popups=False, auto_mode=False, task_payload=None):
    if task_running():
        log_msg("⚠️ 当前已有任务在运行，请先停止或等待本轮结束。", force=True)
        return False
    if task_payload is None:
        task_payload = build_task_payload_from_ui(exec_day_override=exec_day_override)
    else:
        task_payload = clone_task_payload(task_payload)
        if exec_day_override:
            task_payload["exec_day_str"] = exec_day_override

    if not validate_task_payload(task_payload, suppress_validation_popups=suppress_validation_popups):
        return False

    uid = task_payload["uid"]
    pwd = task_payload["pwd"]
    date_str = task_payload["date_str"]
    venue_name = task_payload["venue_name"]
    exec_day_str = task_payload["exec_day_str"]
    exec_time_str = task_payload["exec_time_str"]
    groups = normalize_groups(task_payload.get("groups", []))
    runtime_options = clone_runtime_options(task_payload.get("runtime_options") or build_runtime_options())
    task_payload["runtime_options"] = runtime_options

    if task_payload is not None and auto_mode:
        sync_task_payload_to_ui(task_payload)

    reset_task_control_for_start()
    save_config()
    begin_run_record(date_str, venue_name, len(groups), runtime_options)
    
    update_task_buttons(True)
    if auto_mode:
        log_msg("🤖 自动续跑已接管当前任务。", force=True)
    log_msg(f"程序版本：{APP_VERSION}", force=True)
    log_msg(f"任务锁定！日期:{date_str} | {venue_name}", force=True)
    log_msg(f"已装载 {len(groups)} 组独立火力编队。", force=True)
    log_msg(
        f"当前设置：详细日志={'开' if runtime_options['live_log'] else '关'} | "
        f"耗时统计={'开' if runtime_options['timing_diag'] else '关'} | "
        f"冲刺模式={'开' if runtime_options['sprint_mode'] else '关'} | "
        f"自动顺延={'开' if runtime_options['auto_next_day'] else '关'}",
        force=True
    )
    run_thread = threading.Thread(
        target=automation_task,
        args=(uid, pwd, date_str, venue_name, groups, exec_day_str, exec_time_str, runtime_options),
        daemon=True
    )
    set_active_run_thread(run_thread)
    run_thread.start()
    root.after(250, minimize_to_tray)
    return True

def stop_program():
    if not task_running():
        update_task_buttons(False)
        log_msg("ℹ️ 当前没有正在执行的任务。", force=True)
        return

    with task_control_lock:
        if task_runtime_state.get("manual_stop"):
            return
        task_runtime_state["manual_stop"] = True

    task_stop_event.set()
    update_task_buttons(True, stop_pending=True)
    log_msg("🛑 已发送停止指令，程序将在当前步骤安全退出，请稍候...", force=True)

def save_config():
    global app_config
    if "venues" not in app_config: app_config["venues"] = {}
    
    if remember_var.get(): encoded_pwd = base64.b64encode(entry_pass.get().encode('utf-8')).decode('utf-8')
    else: encoded_pwd = "" 
    
    app_config["user_id"] = entry_user.get() if remember_var.get() else ""
    app_config["password"] = encoded_pwd
    app_config["remember"] = remember_var.get()
    app_config["live_log"] = live_log_var.get()
    app_config["timing_diag"] = timing_diag_var.get()
    app_config["sprint_mode"] = sprint_mode_var.get()
    app_config["auto_next_day"] = auto_next_day_var.get()
    app_config["export_log"] = export_log_var.get()
    app_config["date"] = entry_date.get()
    app_config["last_venue"] = combo_venue.get()
    app_config["exec_day"] = combo_exec_day.get()
    app_config["exec_time"] = combo_exec.get()
    app_config["schedule_mode"] = get_current_schedule_mode()
    
    save_current_venue_to_memory()
    
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(app_config, f, ensure_ascii=False, indent=4)
    except: pass

def load_config():
    global app_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                app_config = json.load(f)
                
                remember_var.set(app_config.get("remember", False))
                if app_config.get("remember", False):
                    entry_user.insert(0, app_config.get("user_id", ""))
                    if app_config.get("password", ""):
                        entry_pass.insert(0, base64.b64decode(app_config.get("password", "").encode('utf-8')).decode('utf-8'))
                
                date_val = app_config.get("date", "")
                if not date_val: date_val = datetime.datetime.now().strftime("%Y-%m-%d")
                entry_date.insert(0, date_val)
                
                last_venue = app_config.get("last_venue")
                if last_venue in VENUE_MAP:
                    combo_venue.set(last_venue)
                elif list(VENUE_MAP.keys()):
                    combo_venue.current(0)
                
                combo_exec_day.set(app_config.get("exec_day", "今天"))
                combo_exec.set(app_config.get("exec_time", "立即执行"))
                schedule_mode = app_config.get("schedule_mode", SCHEDULE_MODE_SUMMER)
                schedule_mode_var.set(schedule_mode if schedule_mode in SCHEDULE_MODES else SCHEDULE_MODE_SUMMER)
                if "compact_log" in app_config:
                    live_log_var.set(bool(app_config.get("live_log", True)) and (not bool(app_config.get("compact_log", True))))
                else:
                    live_log_var.set(app_config.get("live_log", True))
                timing_diag_var.set(app_config.get("timing_diag", True))
                sprint_mode_var.set(app_config.get("sprint_mode", False))
                auto_next_day_var.set(app_config.get("auto_next_day", False))
                export_log_var.set(app_config.get("export_log", False))
        except: pass
    else:
        if list(VENUE_MAP.keys()):
            combo_venue.current(0)
        entry_date.insert(0, datetime.datetime.now().strftime("%Y-%m-%d"))
    
    update_combos()

# ==========================================
# 🎨 GUI 界面构建
# ==========================================
APP_BG = "#eef3f8"
PANEL_BG = "#ffffff"
SECTION_BG = "#ffffff"
ENTRY_BG = "#f8fafc"
LINE_COLOR = "#d6deea"
TITLE_COLOR = "#122033"
TEXT_COLOR = "#334155"
MUTED_COLOR = "#64748b"
ACCENT_COLOR = "#1f6feb"
SUCCESS_COLOR = "#1d8f5a"
LOG_BG = "#ffffff"
LOG_FG = "#111827"
NO_WHEEL_BINDTAG = "NoMouseWheel"

def swallow_mousewheel(event):
    return "break"

def protect_widget_from_mousewheel(widget):
    bindtags = widget.bindtags()
    if NO_WHEEL_BINDTAG not in bindtags:
        widget.bindtags((bindtags[0], NO_WHEEL_BINDTAG, *bindtags[1:]))
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(sequence, swallow_mousewheel, add="+")
    return widget

def make_entry(parent, show=None):
    kwargs = {
        "bg": ENTRY_BG,
        "fg": TITLE_COLOR,
        "insertbackground": TITLE_COLOR,
        "relief": "flat",
        "highlightthickness": 1,
        "highlightbackground": LINE_COLOR,
        "highlightcolor": ACCENT_COLOR,
        "font": ("微软雅黑", 10),
        "width": 24,
    }
    if show is not None:
        kwargs["show"] = show
    return protect_widget_from_mousewheel(tk.Entry(parent, **kwargs))

def make_combo(parent, values=(), state="readonly", width=22):
    return protect_widget_from_mousewheel(
        ttk.Combobox(parent, values=list(values), width=width, state=state, style="App.TCombobox")
    )

def add_form_row(parent, row, label_text, widget, bg_color=SECTION_BG):
    tk.Label(
        parent,
        text=label_text,
        bg=bg_color,
        fg=TEXT_COLOR,
        font=("微软雅黑", 10, "bold"),
        width=11,
        anchor="e"
    ).grid(
        row=row, column=0, sticky="e", pady=6, padx=(0, 12)
    )
    widget.grid(row=row, column=1, sticky="ew", pady=6)

def create_section(parent, title, subtitle):
    shell = tk.Frame(parent, bg=SECTION_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    tk.Label(shell, text=title, bg=SECTION_BG, fg=TITLE_COLOR, font=("微软雅黑", 12, "bold")).pack(
        anchor="w", padx=16, pady=(14, 0)
    )
    tk.Label(shell, text=subtitle, bg=SECTION_BG, fg=MUTED_COLOR, font=("微软雅黑", 9)).pack(
        anchor="w", padx=16, pady=(2, 10)
    )
    body = tk.Frame(shell, bg=SECTION_BG)
    body.pack(fill=tk.X, padx=16, pady=(0, 14))
    body.grid_columnconfigure(1, weight=1)
    return shell, body

def create_plan_card(parent, title, badge_text, badge_bg):
    shell = tk.Frame(parent, bg=SECTION_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    header = tk.Frame(shell, bg=SECTION_BG)
    header.pack(fill=tk.X, padx=14, pady=(12, 8))
    tk.Label(header, text=title, bg=SECTION_BG, fg=TITLE_COLOR, font=("微软雅黑", 11, "bold")).pack(side=tk.LEFT)
    tk.Label(
        header,
        text=badge_text,
        bg=badge_bg,
        fg="white",
        font=("微软雅黑", 8, "bold"),
        padx=8,
        pady=2
    ).pack(side=tk.RIGHT)
    body = tk.Frame(shell, bg=SECTION_BG)
    body.pack(fill=tk.X, padx=14, pady=(0, 12))
    body.grid_columnconfigure(1, weight=1)
    return shell, body

def equalize_card_heights(*frames):
    root.update_idletasks()
    max_height = max(frame.winfo_reqheight() for frame in frames)
    for frame in frames:
        frame.configure(height=max_height)
        frame.pack_propagate(False)

if os.name == "nt":
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_USER = 0x0400
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_NULL = 0x0000
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NIIF_NONE = 0x00000000
    NIIF_INFO = 0x00000001
    NIIF_WARNING = 0x00000002
    NIIF_ERROR = 0x00000003
    TPM_LEFTALIGN = 0x0000
    TPM_BOTTOMALIGN = 0x0020
    TPM_RETURNCMD = 0x0100
    TPM_NONOTIFY = 0x0080
    MF_STRING = 0x0000
    IDI_APPLICATION = 32512
    TRAY_CALLBACK_MESSAGE = WM_USER + 1
    TRAY_SHOW_MESSAGE = WM_USER + 2
    TRAY_HIDE_MESSAGE = WM_USER + 3
    TRAY_EXIT_MESSAGE = WM_USER + 4
    TRAY_BALLOON_MESSAGE = WM_USER + 5
    TRAY_TIP_MESSAGE = WM_USER + 6

    ATOM = getattr(wintypes, "ATOM", wintypes.WORD)
    LRESULT = wintypes.LPARAM
    HICON = wintypes.HANDLE
    HCURSOR = wintypes.HANDLE
    HBRUSH = wintypes.HANDLE
    HMENU = wintypes.HANDLE
    UINT_PTR = ctypes.c_size_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", POINT),
        ]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", HICON),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.RegisterClassW.restype = ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, HMENU, wintypes.HINSTANCE, wintypes.LPVOID
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.CreatePopupMenu.restype = HMENU
    user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, UINT_PTR, wintypes.LPCWSTR]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = HICON
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

    class WindowsTrayController:
        MENU_SHOW = 1001
        MENU_EXIT = 1002

        def __init__(self, root_window, icon_path):
            self.root = root_window
            self.icon_path = icon_path
            self.thread = None
            self.hwnd = None
            self.hicon = None
            self.icon_visible = False
            self.class_name = f"XJTUBookingTray_{os.getpid()}"
            self._wndproc = WNDPROC(self._window_proc)
            self._ready = threading.Event()
            self.pending_balloon = ("抢场无忧", "", NIIF_INFO)
            self.tip_text = "抢场无忧正在后台待命"
            self.balloon_timeout_ms = 30000

        def start(self):
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._message_loop, daemon=True)
            self.thread.start()
            self._ready.wait(timeout=3)

        def is_ready(self):
            return self._ready.is_set() and bool(self.hwnd)

        def show_icon(self):
            if self.is_ready():
                user32.PostMessageW(self.hwnd, TRAY_SHOW_MESSAGE, 0, 0)

        def hide_icon(self):
            if self.is_ready():
                user32.PostMessageW(self.hwnd, TRAY_HIDE_MESSAGE, 0, 0)

        def shutdown(self):
            if self.is_ready():
                user32.PostMessageW(self.hwnd, TRAY_EXIT_MESSAGE, 0, 0)

        def show_balloon(self, title, message, info_flag=NIIF_INFO):
            if not self.is_ready():
                return
            safe_title = str(title or "抢场无忧")[:63]
            safe_message = str(message or "").strip()[:255]
            if not safe_message:
                return
            self.pending_balloon = (safe_title, safe_message, info_flag)
            self.tip_text = self._compose_tip_text(safe_title, safe_message)
            user32.PostMessageW(self.hwnd, TRAY_BALLOON_MESSAGE, 0, 0)

        def set_status_tip(self, title="", message=""):
            self.tip_text = self._compose_tip_text(title, message)
            if self.is_ready():
                user32.PostMessageW(self.hwnd, TRAY_TIP_MESSAGE, 0, 0)

        def reset_status_tip(self):
            self.tip_text = "抢场无忧正在后台待命"
            if self.is_ready():
                user32.PostMessageW(self.hwnd, TRAY_TIP_MESSAGE, 0, 0)

        def _compose_tip_text(self, title="", message=""):
            parts = []
            title = str(title or "").strip()
            message = str(message or "").strip()
            if title:
                parts.append(title)
            if message:
                parts.append(message)
            tip = " | ".join(parts) if parts else "抢场无忧正在后台待命"
            tip = " ".join(tip.split())
            return tip[:127]

        def _load_icon_handle(self):
            if self.hicon:
                return self.hicon
            if self.icon_path and os.path.exists(self.icon_path):
                self.hicon = user32.LoadImageW(
                    None,
                    self.icon_path,
                    IMAGE_ICON,
                    0,
                    0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE
                )
            if not self.hicon:
                self.hicon = user32.LoadIconW(None, IDI_APPLICATION)
            return self.hicon

        def _build_notify_data(self):
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = TRAY_CALLBACK_MESSAGE
            nid.hIcon = self._load_icon_handle()
            nid.szTip = self.tip_text
            return nid

        def _message_loop(self):
            hinstance = kernel32.GetModuleHandleW(None)
            wndclass = WNDCLASS()
            wndclass.lpfnWndProc = self._wndproc
            wndclass.hInstance = hinstance
            wndclass.lpszClassName = self.class_name
            user32.RegisterClassW(ctypes.byref(wndclass))
            self.hwnd = user32.CreateWindowExW(
                0,
                self.class_name,
                self.class_name,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None
            )
            self._ready.set()

            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        def _show_popup_menu(self, hwnd):
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, self.MENU_SHOW, "显示主窗口")
            user32.AppendMenuW(menu, MF_STRING, self.MENU_EXIT, "退出程序")
            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                menu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x,
                point.y,
                0,
                hwnd,
                None
            )
            user32.DestroyMenu(menu)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)

            if cmd == self.MENU_SHOW:
                self.root.after(0, restore_from_tray)
            elif cmd == self.MENU_EXIT:
                self.root.after(0, exit_application)

        def _window_proc(self, hwnd, msg, wparam, lparam):
            if msg == TRAY_CALLBACK_MESSAGE:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.root.after(0, restore_from_tray)
                    return 0
                if lparam == WM_RBUTTONUP:
                    self._show_popup_menu(hwnd)
                    return 0
            elif msg == TRAY_SHOW_MESSAGE:
                if not self.icon_visible:
                    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._build_notify_data()))
                    self.icon_visible = True
                return 0
            elif msg == TRAY_HIDE_MESSAGE:
                if self.icon_visible:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._build_notify_data()))
                    self.icon_visible = False
                return 0
            elif msg == TRAY_EXIT_MESSAGE:
                if self.icon_visible:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._build_notify_data()))
                    self.icon_visible = False
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == TRAY_BALLOON_MESSAGE:
                if self.icon_visible:
                    balloon = self._build_notify_data()
                    balloon.uFlags = NIF_INFO | NIF_TIP
                    balloon.szInfoTitle = self.pending_balloon[0]
                    balloon.szInfo = self.pending_balloon[1]
                    balloon.dwInfoFlags = self.pending_balloon[2]
                    balloon.uTimeoutOrVersion = self.balloon_timeout_ms
                    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(balloon))
                return 0
            elif msg == TRAY_TIP_MESSAGE:
                if self.icon_visible:
                    tooltip = self._build_notify_data()
                    tooltip.uFlags = NIF_TIP
                    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(tooltip))
                return 0
            elif msg == WM_DESTROY:
                if self.icon_visible:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._build_notify_data()))
                    self.icon_visible = False
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
else:
    WindowsTrayController = None

if __name__ == "__main__":
    root = tk.Tk()
    root.title(f"抢场无忧 - {APP_VERSION} - Yushin")
    try: root.iconbitmap(resource_path("logo.ico"))
    except: pass
    root.geometry("1360x860+120+30")
    root.minsize(1180, 760)
    root.resizable(True, True)
    root.configure(bg=APP_BG)

    tray_hidden_state = {"hidden": False, "app_exiting": False}
    tray_controller = None

    def minimize_to_tray():
        if tray_hidden_state["app_exiting"] or tray_hidden_state["hidden"]:
            return
        if tray_controller is None or not tray_controller.is_ready():
            root.iconify()
            return
        tray_hidden_state["hidden"] = True
        tray_controller.show_icon()
        root.withdraw()

    def restore_from_tray():
        if tray_controller is not None:
            tray_controller.hide_icon()
        if tray_hidden_state["app_exiting"]:
            return
        tray_hidden_state["hidden"] = False
        root.deiconify()
        root.lift()
        try:
            root.focus_force()
        except Exception:
            pass

    def exit_application():
        tray_hidden_state["app_exiting"] = True
        try:
            if tray_controller is not None:
                tray_controller.shutdown()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    def on_root_close():
        minimize_to_tray()

    def on_root_unmap(event):
        if tray_hidden_state["app_exiting"] or tray_hidden_state["hidden"]:
            return
        try:
            if root.state() == "iconic":
                root.after(0, minimize_to_tray)
        except Exception:
            pass

    def send_system_notification(title, message, level="info"):
        flag_map = {
            "info": NIIF_INFO if os.name == "nt" else 0,
            "warning": NIIF_WARNING if os.name == "nt" else 0,
            "error": NIIF_ERROR if os.name == "nt" else 0,
        }
        info_flag = flag_map.get(level, flag_map["info"])

        def notify():
            if tray_controller is None or not tray_controller.is_ready():
                return
            tray_controller.set_status_tip(title, message)
            if tray_hidden_state.get("hidden", False):
                tray_controller.show_balloon(title, message, info_flag)

        try:
            root.after(0, notify)
        except Exception:
            notify()

    def update_task_buttons(is_running, stop_pending=False):
        if "btn_start" not in globals() or "btn_stop" not in globals():
            return
        btn_start.config(
            state=tk.DISABLED if is_running else tk.NORMAL,
            text="🚀 启动任务"
        )
        btn_stop.config(
            state=tk.NORMAL if is_running and not stop_pending else tk.DISABLED,
            text="⏹ 正在停止..." if stop_pending else "⏹ 停止任务"
        )

    def notify_task_outcome(date_str, venue_name, status_text, success_obtained=False, auto_next_day_info=None, final_state=None):
        date_part = date_str or "未知日期"
        venue_part = venue_name or "未知场馆"
        auto_next_day_info = auto_next_day_info or {}
        state = final_state or snapshot_run_state()
        last_event = (state.get("last_event") or "").strip()
        last_success_event = (state.get("last_success_event") or "").strip()

        def build_auto_suffix():
            next_date = auto_next_day_info.get("next_date") or ""
            exec_time = auto_next_day_info.get("exec_time") or ""
            reason = auto_next_day_info.get("reason") or ""
            if auto_next_day_info.get("scheduled"):
                if next_date and exec_time:
                    return f"；已顺延到 {next_date}，计划于明天 {exec_time} 自动续跑"
                if next_date:
                    return f"；已顺延到 {next_date} 并准备自动续跑"
            if reason == "immediate_only" and next_date:
                return f"；已顺延到 {next_date}，但当前为“立即执行”，不会自动续跑"
            if reason in {"start_rejected", "ui_error", "ui_timeout", "dispatch_failed", "missing_payload", "cancelled"} and next_date:
                return f"；已顺延到 {next_date}，但自动续跑启动失败"
            if reason == "bad_date":
                return "；自动顺延失败：当前抢购日期格式无效"
            return ""

        auto_suffix = build_auto_suffix()

        def join_parts(*parts):
            return " | ".join(str(part).strip() for part in parts if str(part).strip())

        if success_obtained:
            success_detail = last_success_event or "本轮已有成功结果"
            message = (
                join_parts(venue_part, f"抢购日期 {date_part}", f"结果：{success_detail}")
                + auto_suffix
            )
            send_system_notification("抢场成功", message, level="info")
            return

        if status_text == "异常结束":
            final_detail = last_event or "程序异常结束，请恢复窗口查看日志"
            send_system_notification(
                "程序异常",
                join_parts(venue_part, f"抢购日期 {date_part}", f"结果：{final_detail}") + auto_suffix,
                level="error"
            )
            return

        if status_text == "已中止":
            final_detail = last_event or "触发系统拦截或风控，请人工处理"
            send_system_notification(
                "任务中止",
                join_parts(venue_part, f"抢购日期 {date_part}", f"结果：{final_detail}") + auto_suffix,
                level="warning"
            )
            return

        if status_text == "已手动停止":
            final_detail = last_event or "本轮任务已按你的指令停止"
            send_system_notification(
                "任务已停止",
                join_parts(venue_part, f"抢购日期 {date_part}", f"结果：{final_detail}"),
                level="warning"
            )
            return

        final_detail = last_event or (status_text or "任务结束")
        message = join_parts(venue_part, f"抢购日期 {date_part}", f"结果：{final_detail}") + auto_suffix
        send_system_notification("任务结束", message, level="info")

    if WindowsTrayController is not None:
        tray_controller = WindowsTrayController(root, resource_path("logo.ico"))
        tray_controller.start()

    root.protocol("WM_DELETE_WINDOW", on_root_close)
    root.bind("<Unmap>", on_root_unmap)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except:
        pass
    style.configure("App.TCombobox", padding=6)
    style.configure("App.Vertical.TScrollbar", troughcolor="#dbe3ee", background="#94a3b8", arrowcolor=TITLE_COLOR)

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_class(NO_WHEEL_BINDTAG, sequence, swallow_mousewheel)

    main_shell = tk.Frame(root, bg=APP_BG)
    main_shell.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

    header_card = tk.Frame(main_shell, bg=SECTION_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    header_card.pack(fill=tk.X, pady=(0, 14))

    header_text = tk.Frame(header_card, bg=SECTION_BG)
    header_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=18, pady=16)
    tk.Label(header_text, text="羽毛球抢场控制台", bg=SECTION_BG, fg=TITLE_COLOR, font=("微软雅黑", 18, "bold")).pack(anchor="w")
    tk.Label(
        header_text,
        text="左侧集中放配置与计划，右侧保留战况日志；支持一键收起右侧面板。",
        bg=SECTION_BG,
        fg=MUTED_COLOR,
        font=("微软雅黑", 9)
    ).pack(anchor="w", pady=(4, 0))
    header_card.pack_forget()

    content_shell = tk.Frame(main_shell, bg=APP_BG)
    content_shell.pack(fill=tk.BOTH, expand=True)

    left_workspace = tk.Frame(content_shell, bg=APP_BG)
    left_workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    left_workspace.grid_columnconfigure(0, weight=1, uniform="left_cols")
    left_workspace.grid_columnconfigure(1, weight=1, uniform="left_cols")

    right_sidebar = tk.Frame(content_shell, bg=APP_BG, width=380)
    right_sidebar.pack(side=tk.RIGHT, fill=tk.Y, padx=(16, 0))
    right_sidebar.pack_propagate(False)

    battle_panel_state = {"visible": True}

    def toggle_battle_panel():
        if battle_panel_state["visible"]:
            log_panel.pack_forget()
            right_sidebar.configure(width=52)
            collapsed_battle_stub.pack(fill=tk.BOTH, expand=True)
        else:
            collapsed_battle_stub.pack_forget()
            right_sidebar.configure(width=380)
            log_panel.pack(fill=tk.BOTH, expand=True)
        battle_panel_state["visible"] = not battle_panel_state["visible"]

    info_shell, info_body = create_section(left_workspace, "基础信息", "登录凭据、抢购日期和目标场馆")
    info_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(0, 14))
    entry_user = make_entry(info_body)
    entry_pass = make_entry(info_body, show="*")
    entry_date = make_entry(info_body)
    combo_venue = make_combo(info_body, values=list(VENUE_MAP.keys()), state="readonly")
    add_form_row(info_body, 0, "NetID 账号", entry_user)
    add_form_row(info_body, 1, "NetID 密码", entry_pass)
    add_form_row(info_body, 2, "抢购日期", entry_date)
    add_form_row(info_body, 3, "目标场馆", combo_venue)

    remember_var = tk.BooleanVar(value=False)
    remember_check = tk.Checkbutton(
        info_body,
        text="记住账号密码与所有设置",
        variable=remember_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9)
    )
    remember_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 4))
    protect_widget_from_mousewheel(remember_check)

    sprint_mode_var = tk.BooleanVar(value=False)
    sprint_mode_check = tk.Checkbutton(
        info_body,
        text="开启冲刺模式（更快，但稳定性会略降）",
        variable=sprint_mode_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9)
    )
    sprint_mode_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 2))
    protect_widget_from_mousewheel(sprint_mode_check)

    auto_next_day_var = tk.BooleanVar(value=False)
    auto_next_day_check = tk.Checkbutton(
        info_body,
        text="任务结束后自动顺延一天续跑",
        variable=auto_next_day_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9)
    )
    auto_next_day_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 2))
    protect_widget_from_mousewheel(auto_next_day_check)

    schedule_shell, schedule_body = create_section(left_workspace, "调度设置", "定时启动、挂机等待与本地配置")
    schedule_shell.grid(row=0, column=1, sticky="nsew", padx=(10, 0), pady=(0, 14))
    root.after(0, lambda: equalize_card_heights(info_shell, schedule_shell))
    combo_exec_day = make_combo(schedule_body, values=["今天", "明天", "后天"], state="readonly")
    combo_exec = make_combo(schedule_body, values=["立即执行", "08:40:00", "12:00:00"], state="normal")
    schedule_mode_var = tk.StringVar(value=SCHEDULE_MODE_SUMMER)
    combo_schedule_mode = make_combo(schedule_body, values=SCHEDULE_MODES, state="readonly")
    combo_schedule_mode.configure(textvariable=schedule_mode_var)
    add_form_row(schedule_body, 0, "启动日期", combo_exec_day)
    add_form_row(schedule_body, 1, "启动时间", combo_exec)
    add_form_row(schedule_body, 2, "作息模式", combo_schedule_mode)

    live_log_var = tk.BooleanVar(value=True)
    live_log_check = tk.Checkbutton(
        schedule_body,
        text="显示详细过程日志（关闭后只保留核心结果）",
        variable=live_log_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9)
    )
    live_log_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))
    protect_widget_from_mousewheel(live_log_check)

    timing_diag_var = tk.BooleanVar(value=True)
    timing_diag_check = tk.Checkbutton(
        schedule_body,
        text="显示耗时统计（关闭后不做这部分计算）",
        variable=timing_diag_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9),
        command=refresh_run_summary_ui
    )
    timing_diag_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 4))
    protect_widget_from_mousewheel(timing_diag_check)

    export_log_var = tk.BooleanVar(value=False)
    export_log_check = tk.Checkbutton(
        schedule_body,
        text="任务结束后自动导出日志",
        variable=export_log_var,
        bg=SECTION_BG,
        fg=TEXT_COLOR,
        activebackground=SECTION_BG,
        activeforeground=TITLE_COLOR,
        selectcolor=ENTRY_BG,
        font=("微软雅黑", 9),
        command=refresh_run_summary_ui
    )
    export_log_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 4))
    protect_widget_from_mousewheel(export_log_check)

    tk.Label(
        schedule_body,
        text="请将电脑的“睡眠”和“休眠”设置为从不，避免定时前自动挂起。",
        bg=SECTION_BG,
        fg=MUTED_COLOR,
        font=("微软雅黑", 9),
        justify="left",
        wraplength=360
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

    plan_shell, plan_body = create_section(left_workspace, "抢场计划", "默认展示 3 个计划，第 4、5 个备选计划按需添加")
    plan_shell.grid(row=1, column=0, columnspan=2, sticky="nsew")
    plan_body.grid_columnconfigure(0, weight=1, uniform="plan_cols")
    plan_body.grid_columnconfigure(1, weight=1, uniform="plan_cols")
    plan_body.grid_columnconfigure(2, weight=1, uniform="plan_cols")

    plan1_shell, plan1_body = create_plan_card(plan_body, "计划 1", "主战", SUCCESS_COLOR)
    plan1_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 10))
    combo_court1 = make_combo(plan1_body, state="readonly", width=18)
    combo_time1_1 = make_combo(plan1_body, state="readonly", width=18)
    combo_time1_2 = make_combo(plan1_body, state="readonly", width=18)
    add_form_row(plan1_body, 0, "场地", combo_court1)
    add_form_row(plan1_body, 1, "时间 1", combo_time1_1)
    add_form_row(plan1_body, 2, "时间 2", combo_time1_2)

    plan2_shell, plan2_body = create_plan_card(plan_body, "计划 2", "备选", ACCENT_COLOR)
    plan2_shell.grid(row=0, column=1, sticky="nsew", padx=8, pady=(0, 10))
    combo_court2 = make_combo(plan2_body, state="readonly", width=18)
    combo_b1_t1 = make_combo(plan2_body, state="readonly", width=18)
    combo_b1_t2 = make_combo(plan2_body, state="readonly", width=18)
    add_form_row(plan2_body, 0, "场地", combo_court2)
    add_form_row(plan2_body, 1, "时间 1", combo_b1_t1)
    add_form_row(plan2_body, 2, "时间 2", combo_b1_t2)

    plan3_shell, plan3_body = create_plan_card(plan_body, "计划 3", "备选", ACCENT_COLOR)
    plan3_shell.grid(row=0, column=2, sticky="nsew", padx=(8, 0), pady=(0, 10))
    combo_court3 = make_combo(plan3_body, state="readonly", width=18)
    combo_b2_t1 = make_combo(plan3_body, state="readonly", width=18)
    combo_b2_t2 = make_combo(plan3_body, state="readonly", width=18)
    add_form_row(plan3_body, 0, "场地", combo_court3)
    add_form_row(plan3_body, 1, "时间 1", combo_b2_t1)
    add_form_row(plan3_body, 2, "时间 2", combo_b2_t2)

    plan4_shell, plan4_body = create_plan_card(plan_body, "计划 4", "备选", ACCENT_COLOR)
    plan4_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 10))
    combo_court4 = make_combo(plan4_body, state="readonly", width=18)
    combo_b3_t1 = make_combo(plan4_body, state="readonly", width=18)
    combo_b3_t2 = make_combo(plan4_body, state="readonly", width=18)
    add_form_row(plan4_body, 0, "场地", combo_court4)
    add_form_row(plan4_body, 1, "时间 1", combo_b3_t1)
    add_form_row(plan4_body, 2, "时间 2", combo_b3_t2)

    plan5_shell, plan5_body = create_plan_card(plan_body, "计划 5", "备选", ACCENT_COLOR)
    plan5_shell.grid(row=1, column=1, sticky="nsew", padx=8)
    combo_court5 = make_combo(plan5_body, state="readonly", width=18)
    combo_b4_t1 = make_combo(plan5_body, state="readonly", width=18)
    combo_b4_t2 = make_combo(plan5_body, state="readonly", width=18)
    add_form_row(plan5_body, 0, "场地", combo_court5)
    add_form_row(plan5_body, 1, "时间 1", combo_b4_t1)
    add_form_row(plan5_body, 2, "时间 2", combo_b4_t2)

    quick_shell, quick_body = create_plan_card(plan_body, "快速启动", "操作", SUCCESS_COLOR)
    quick_shell.grid(row=1, column=2, sticky="nsew", padx=(8, 0))
    quick_body.grid_columnconfigure(0, weight=1)
    quick_body.grid_columnconfigure(1, weight=1)
    quick_action_bar = tk.Frame(quick_body, bg=SECTION_BG)
    quick_action_bar.grid(row=0, column=0, columnspan=2, sticky="ew")

    btn_start = tk.Button(
        quick_body,
        text="🚀 开始任务",
        font=("微软雅黑", 11, "bold"),
        bg="#1f9d62",
        fg="white",
        activebackground="#188453",
        activeforeground="white",
        relief="flat",
        padx=18,
        pady=10,
        command=start_program,
        cursor="hand2"
    )
    btn_start.grid(row=1, column=0, sticky="ew", pady=(12, 0), padx=(0, 6))
    protect_widget_from_mousewheel(btn_start)

    btn_stop = tk.Button(
        quick_body,
        text="⏹ 停止任务",
        font=("微软雅黑", 11, "bold"),
        bg="#e11d48",
        fg="white",
        activebackground="#be123c",
        activeforeground="white",
        relief="flat",
        padx=18,
        pady=10,
        command=stop_program,
        cursor="hand2",
        state=tk.DISABLED
    )
    btn_stop.grid(row=1, column=1, sticky="ew", pady=(12, 0), padx=(6, 0))
    protect_widget_from_mousewheel(btn_stop)

    update_task_buttons(False)

    def plan_has_custom_selection(*widgets):
        return any(widget.get().strip() and widget.get().strip() != "无" for widget in widgets)

    optional_plan_shells = [plan4_shell, plan5_shell]
    optional_plan_widgets = [
        (combo_court4, combo_b3_t1, combo_b3_t2),
        (combo_court5, combo_b4_t1, combo_b4_t2)
    ]
    optional_plan_state = {"visible": 0}

    def reset_optional_plan_widgets(plan_index):
        for widget in optional_plan_widgets[plan_index]:
            values = list(widget["values"])
            if "无" in values:
                widget.set("无")
            elif values:
                widget.set(values[0])
            else:
                widget.set("")

    def set_visible_optional_plans(count):
        count = max(0, min(count, len(optional_plan_shells)))
        for index, shell in enumerate(optional_plan_shells):
            if index < count:
                shell.grid()
            else:
                shell.grid_remove()
        optional_plan_state["visible"] = count
        if count < len(optional_plan_shells):
            add_plan_btn.config(state=tk.NORMAL, text="+ 添加备选计划")
        else:
            add_plan_btn.config(state=tk.DISABLED, text="备选计划已全部展开")
        if count > 0:
            remove_plan_btn.config(state=tk.NORMAL)
        else:
            remove_plan_btn.config(state=tk.DISABLED)

    def hide_last_optional_plan():
        if optional_plan_state["visible"] <= 0:
            return
        plan_index = optional_plan_state["visible"] - 1
        reset_optional_plan_widgets(plan_index)
        set_visible_optional_plans(plan_index)

    def sync_optional_plan_cards():
        required_count = 0
        for index, widgets in enumerate(optional_plan_widgets, start=1):
            if plan_has_custom_selection(*widgets):
                required_count = index
        set_visible_optional_plans(max(optional_plan_state["visible"], required_count))

    add_plan_btn = tk.Button(
        quick_action_bar,
        text="+ 添加备选计划",
        font=("微软雅黑", 10, "bold"),
        bg="#e8f1ff",
        fg=ACCENT_COLOR,
        activebackground="#d8e8ff",
        activeforeground=ACCENT_COLOR,
        relief="flat",
        padx=14,
        pady=8,
        command=lambda: set_visible_optional_plans(optional_plan_state["visible"] + 1),
        cursor="hand2"
    )
    add_plan_btn.pack(side=tk.LEFT)
    protect_widget_from_mousewheel(add_plan_btn)

    remove_plan_btn = tk.Button(
        quick_action_bar,
        text="− 删除备选计划",
        font=("微软雅黑", 10, "bold"),
        bg="#f3f4f6",
        fg=TEXT_COLOR,
        activebackground="#e5e7eb",
        activeforeground=TITLE_COLOR,
        relief="flat",
        padx=14,
        pady=8,
        command=hide_last_optional_plan,
        cursor="hand2"
    )
    remove_plan_btn.pack(side=tk.LEFT, padx=(10, 0))
    protect_widget_from_mousewheel(remove_plan_btn)

    set_visible_optional_plans(0)

    log_panel = tk.Frame(right_sidebar, bg=SECTION_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    log_panel.pack(fill=tk.BOTH, expand=True)

    log_header = tk.Frame(log_panel, bg=SECTION_BG)
    log_header.pack(fill=tk.X, padx=16, pady=(16, 8))
    tk.Label(log_header, text="实时战况", bg=SECTION_BG, fg=TITLE_COLOR, font=("微软雅黑", 13, "bold")).pack(side=tk.LEFT)
    tk.Button(
        log_header,
        text="→ 折叠",
        font=("微软雅黑", 9, "bold"),
        bg="#e2e8f0",
        fg=TITLE_COLOR,
        activebackground="#cbd5e1",
        activeforeground=TITLE_COLOR,
        relief="flat",
        padx=10,
        pady=6,
        command=toggle_battle_panel,
        cursor="hand2"
    ).pack(side=tk.RIGHT)

    run_summary_var = tk.StringVar(value="状态：待启动\n目标：- | -\n计划：0 组\n统计：成功 0  已被预订 0  名额已满 0\n      验证码有误 0  Cookie失效 0  未知返回 0\n耗时Top3：暂无")
    run_export_var = tk.StringVar(value="自动导出：未启用")

    summary_card = tk.Frame(log_panel, bg="#f8fafc", highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    summary_card.pack(fill=tk.X, padx=16, pady=(0, 12))

    summary_header = tk.Frame(summary_card, bg="#f8fafc")
    summary_header.pack(fill=tk.X, padx=12, pady=(10, 6))
    tk.Label(summary_header, text="运行总结", bg="#f8fafc", fg=TITLE_COLOR, font=("微软雅黑", 11, "bold")).pack(side=tk.LEFT)

    open_log_dir_btn = tk.Button(
        summary_header,
        text="打开日志目录",
        font=("微软雅黑", 8, "bold"),
        bg="#e2e8f0",
        fg=TITLE_COLOR,
        activebackground="#cbd5e1",
        activeforeground=TITLE_COLOR,
        relief="flat",
        padx=8,
        pady=4,
        command=open_log_export_dir,
        cursor="hand2"
    )
    open_log_dir_btn.pack(side=tk.RIGHT)
    protect_widget_from_mousewheel(open_log_dir_btn)

    tk.Label(
        summary_card,
        textvariable=run_summary_var,
        bg="#f8fafc",
        fg=TEXT_COLOR,
        font=("微软雅黑", 9),
        justify="left",
        anchor="w",
        wraplength=292
    ).pack(fill=tk.X, padx=12)

    tk.Label(
        summary_card,
        textvariable=run_export_var,
        bg="#f8fafc",
        fg=MUTED_COLOR,
        font=("Consolas", 8),
        justify="left",
        anchor="w",
        wraplength=320
    ).pack(fill=tk.X, padx=12, pady=(8, 10))

    log_area = tk.Frame(log_panel, bg=LOG_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    log_area.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))

    scrollbar = ttk.Scrollbar(log_area, style="App.Vertical.TScrollbar")
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=12)
    log_box = tk.Text(
        log_area,
        state=tk.DISABLED,
        bg=LOG_BG,
        fg=LOG_FG,
        insertbackground=LOG_FG,
        selectbackground="#dbeafe",
        relief="flat",
        bd=0,
        padx=14,
        pady=14,
        font=("Consolas", 10),
        yscrollcommand=scrollbar.set
    )
    log_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=log_box.yview)

    collapsed_battle_stub = tk.Frame(right_sidebar, bg=SECTION_BG, highlightbackground=LINE_COLOR, highlightthickness=1, bd=0)
    expand_battle_btn = tk.Button(
        collapsed_battle_stub,
        text="←\n展\n开\n战\n况",
        font=("微软雅黑", 9, "bold"),
        bg="#e2e8f0",
        fg=TITLE_COLOR,
        activebackground="#cbd5e1",
        activeforeground=TITLE_COLOR,
        relief="flat",
        padx=6,
        pady=10,
        command=toggle_battle_panel,
        cursor="hand2",
        justify="center"
    )
    expand_battle_btn.pack(fill=tk.BOTH, expand=True, padx=6, pady=10)
    protect_widget_from_mousewheel(expand_battle_btn)

    combo_venue.bind("<<ComboboxSelected>>", update_combos)
    combo_schedule_mode.bind("<<ComboboxSelected>>", on_schedule_mode_changed)

    refresh_run_summary_ui()
    load_config()
    refresh_run_summary_ui()

    log_msg(f"系统初始化完成：{APP_VERSION}。请填写账号并设置预约计划。", force=True)
    root.mainloop()
