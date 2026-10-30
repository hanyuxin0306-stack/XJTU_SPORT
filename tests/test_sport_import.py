"""The combined module can be imported without starting a task or reading credentials."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SafeImportTests(unittest.TestCase):
    def test_import_does_not_create_ui_read_config_or_start_a_browser(self):
        project = Path(__file__).resolve().parent.parent
        script = r'''
import builtins
import io
import os
from pathlib import Path
import sys
from unittest.mock import patch
import tkinter
from selenium import webdriver

sys.path.insert(0, sys.argv[1])
original_open, original_io_open = builtins.open, io.open

def guard_config(open_function):
    def guarded(file, *args, **kwargs):
        if isinstance(file, (str, bytes, Path)):
            path = Path(os.fsdecode(file)).name
            if path == "config.json":
                raise AssertionError("Import must not read or write runtime credentials")
        return open_function(file, *args, **kwargs)
    return guarded

with patch.object(tkinter, "Tk", side_effect=AssertionError("Import must not create a window")), \
     patch.object(webdriver, "Edge", side_effect=AssertionError("Import must not start a browser")), \
     patch.object(builtins, "open", guard_config(original_open)), \
     patch.object(io, "open", guard_config(original_io_open)):
    import sport
    assert not hasattr(sport, "root"), "Main window was initialized during import"
    assert sport.LoginNavigator.__module__ == "sport", "Login code was not merged into the main file"
    assert "sport_login_1030" not in sys.modules, "Import still depends on the removed login file"
print("safe import passed")
'''
        with tempfile.TemporaryDirectory(prefix="sport_import_test_") as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, str(project)], cwd=directory,
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe import passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
