"""Exercise task startup and worker transitions without Tk or network requests."""
import ast
import datetime
from pathlib import Path
import time
import types
import unittest
from unittest.mock import Mock


def load_function(name, namespace):
    source = (Path(__file__).resolve().parent.parent / "sport.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(Path(__file__).resolve().parent.parent / "sport.py"), "exec"), namespace)
    return namespace[name]


class FlowError(Exception):
    def __init__(self, stage="登录", reason="账号或密码错误"):
        self.stage = stage
        super().__init__(f"{stage}: {reason}")


class Cancelled(Exception):
    pass


class DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.driver = Mock()
        self.driver.get_cookies.return_value = [{"name": "SESSION", "value": "dummy"}]
        self.navigator = Mock()
        self.navigator.save_diagnostics.return_value = "login_diagnostics/test.json"
        self.logs = []
        self.stopped = False
        self.ns = {
            "time": time, "datetime": datetime, "traceback": __import__("traceback"),
            "LoginCancelled": Cancelled, "LoginFlowError": FlowError,
            "LoginNavigator": Mock(return_value=self.navigator),
            "webdriver": types.SimpleNamespace(EdgeOptions=Mock(), Edge=Mock(return_value=self.driver)),
            "VENUE_MAP": {"test venue": {"id": "105"}},
            "hot_verbose_enabled": lambda _: False,
            "get_speed_profile": lambda _: {},
            "stop_requested": lambda: self.stopped,
            "set_active_driver": Mock(), "clear_active_driver": Mock(),
            "get_app_base_dir": lambda: ".", "record_stage_costs": Mock(),
            "log_msg": lambda msg, **_: self.logs.append(msg),
            "run_state_lock": DummyLock(), "current_run_state": {},
            "finalize_run_record": Mock(return_value={}), "mark_task_idle": Mock(),
            "handle_auto_next_day_after_run": Mock(return_value={"scheduled": False}),
            "notify_task_outcome": Mock(), "update_task_buttons": Mock(),
            "root": types.SimpleNamespace(after=Mock()), "restore_from_tray": Mock(),
            "timing_diag_enabled": lambda _: False,
            "ensure_api_runtime_context": Mock(return_value=({"session": Mock()}, {})),
            "extract_time_slots": lambda *_: ["08:00-09:00"],
            "pure_api_booking": Mock(return_value="FAILED"),
        }
        self.run_worker = load_function("automation_task", self.ns)

    def run_with(self):
        self.run_worker("dummy user", "dummy password", "2026-10-30", "test venue",
                        [("场地13", "08:00-09:00", "无")], "今天", "立即执行", {})

    def status(self):
        return self.ns["finalize_run_record"].call_args.args[0]

    def test_successful_login_enters_the_selected_venue_and_booking_worker(self):
        self.run_with()
        self.navigator.prepare.assert_called_once_with("dummy user", "dummy password")
        self.driver.get.assert_called_once_with("http://202.117.17.144:8080/web/product/show.html?id=105")
        self.ns["pure_api_booking"].assert_called_once()
        self.assertEqual(self.ns["pure_api_booking"].call_args.args[1:6],
                         ("2026-10-30", "场地13", "08:00-09:00", "无", "105"))
        self.driver.quit.assert_called_once()
        self.assertEqual("已完成", self.status())

    def test_failed_login_records_stage_and_never_continues(self):
        self.navigator.prepare.side_effect = FlowError("门户登录", "账号或密码错误")
        self.run_with()
        self.assertEqual("登录未完成", self.status())
        self.assertIn("门户登录", self.ns["current_run_state"]["last_event"])
        self.navigator.save_diagnostics.assert_called_once()
        self.driver.get.assert_not_called()
        self.ns["pure_api_booking"].assert_not_called()
        self.ns["handle_auto_next_day_after_run"].assert_not_called()

    def test_cancelled_login_is_reported_as_stopped(self):
        self.navigator.prepare.side_effect = Cancelled()
        self.run_with()
        self.assertEqual("已手动停止", self.status())
        self.driver.get.assert_not_called()
        self.ns["pure_api_booking"].assert_not_called()
        self.ns["handle_auto_next_day_after_run"].assert_not_called()

    def test_stop_arriving_after_login_prevents_venue_navigation(self):
        def stop_after_prepare(*_):
            self.stopped = True
        self.navigator.prepare.side_effect = stop_after_prepare
        self.run_with()
        self.assertEqual("已手动停止", self.status())
        self.driver.get.assert_not_called()
        self.ns["pure_api_booking"].assert_not_called()


class TaskStartupTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "uid": "dummy user", "pwd": "dummy password", "date_str": "2026-10-30",
            "venue_name": "test venue", "exec_day_str": "今天", "exec_time_str": "08:40:00",
            "groups": [("场地13", "08:00-09:00", "无")],
            "runtime_options": {"auto_next_day": False, "sprint_mode": False,
                                "timing_diag": True, "live_log": True},
        }
        self.running = False
        self.ns = {
            "datetime": datetime, "task_running": lambda: self.running,
            "build_task_payload_from_ui": Mock(return_value=self.payload),
            "clone_runtime_options": lambda opts: dict(opts),
            "reset_task_control_for_start": Mock(), "save_config": Mock(),
            "begin_run_record": Mock(), "update_task_buttons": Mock(),
            "log_msg": Mock(), "APP_VERSION": "test", "automation_task": Mock(),
            "threading": types.SimpleNamespace(Thread=Mock()), "set_active_run_thread": Mock(),
            "root": types.SimpleNamespace(after=Mock()), "minimize_to_tray": Mock(),
            "messagebox": Mock(),
        }
        load_function("normalize_groups", self.ns)
        load_function("clone_task_payload", self.ns)
        load_function("validate_task_payload", self.ns)
        self.start = load_function("start_program", self.ns)

    def test_start_requires_both_credentials_and_a_booking_plan(self):
        for field, value in (("uid", ""), ("pwd", ""), ("groups", [])):
            with self.subTest(field=field):
                original = self.payload[field]
                self.payload[field] = value
                self.assertFalse(self.start())
                self.payload[field] = original
        self.ns["threading"].Thread.assert_not_called()
        self.ns["save_config"].assert_not_called()
        self.assertEqual(self.ns["messagebox"].showwarning.call_count, 3)

    def test_valid_start_creates_one_worker_with_a_stable_plan_snapshot(self):
        self.assertTrue(self.start(task_payload=self.payload))
        thread_factory = self.ns["threading"].Thread
        thread_factory.assert_called_once()
        args = thread_factory.call_args.kwargs["args"]
        self.assertEqual(args[:4], ("dummy user", "dummy password", "2026-10-30", "test venue"))
        self.assertEqual(args[4], [("场地13", "08:00-09:00", "无")])
        self.payload["groups"][0] = ("场地18", "09:01-10:00", "无")
        self.payload["runtime_options"]["sprint_mode"] = True
        self.assertEqual(args[4], [("场地13", "08:00-09:00", "无")])
        self.assertFalse(args[-1]["sprint_mode"])
        thread_factory.return_value.start.assert_called_once()
        self.ns["save_config"].assert_called_once()
        self.ns["root"].after.assert_called_once_with(250, self.ns["minimize_to_tray"])

    def test_an_active_task_rejects_duplicate_start(self):
        self.running = True
        self.assertFalse(self.start())
        self.ns["build_task_payload_from_ui"].assert_not_called()
        self.ns["threading"].Thread.assert_not_called()

    def test_public_start_signature_only_exposes_booking_start_options(self):
        source = (Path(__file__).resolve().parent.parent / "sport.py").read_text(encoding="utf-8")
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "start_program")
        self.assertEqual([arg.arg for arg in node.args.args],
                         ["exec_day_override", "suppress_validation_popups", "auto_mode", "task_payload"])


if __name__ == "__main__":
    unittest.main()
