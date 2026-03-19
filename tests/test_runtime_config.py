import os
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path


SOURCE_RUNTIME_CONFIG = Path(__file__).resolve().parents[1] / "runtime_config.py"
CONFIG_ENV_KEYS = [
    "HA_HOST",
    "PORT",
    "TUYA_LOCAL_KEY",
    "ENABLE_REMOTE_CHECK",
    "HA_WATCHDOG_DISABLE_LOCAL_CONFIG",
]


class RuntimeConfigLoadingTests(unittest.TestCase):
    def _load_runtime_config(self, config_text, extra_env=None):
        saved = {key: os.environ.get(key) for key in CONFIG_ENV_KEYS}
        extra_env = extra_env or {}

        try:
            for key in CONFIG_ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(extra_env)

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                shutil.copy2(SOURCE_RUNTIME_CONFIG, temp_path / "runtime_config.py")
                if config_text is not None:
                    (temp_path / "config.env").write_text(config_text, encoding="utf-8")
                return runpy.run_path(str(temp_path / "runtime_config.py"))
        finally:
            for key in CONFIG_ENV_KEYS:
                os.environ.pop(key, None)
                if saved[key] is not None:
                    os.environ[key] = saved[key]

    def test_loads_local_config_file_values(self):
        config = self._load_runtime_config(
            "# comment\nHA_HOST=my-ha.local\nexport TUYA_LOCAL_KEY=abc123=\nPORT=9001\n"
        )

        self.assertEqual(config["HA_HOST"], "my-ha.local")
        self.assertEqual(config["TUYA_LOCAL_KEY"], "abc123=")
        self.assertEqual(config["PORT"], 9001)
        self.assertEqual(config["HA_CORE_URL"], "http://my-ha.local:8123/api/")

    def test_existing_environment_variables_override_local_config(self):
        config = self._load_runtime_config(
            "HA_HOST=file-ha.local\nPORT=9001\n",
            extra_env={"HA_HOST": "env-ha.local", "PORT": "8124"},
        )

        self.assertEqual(config["HA_HOST"], "env-ha.local")
        self.assertEqual(config["PORT"], 8124)

    def test_disable_flag_skips_local_config_loading(self):
        config = self._load_runtime_config(
            "HA_HOST=file-ha.local\nTUYA_LOCAL_KEY=abc123=\n",
            extra_env={"HA_WATCHDOG_DISABLE_LOCAL_CONFIG": "true"},
        )

        self.assertEqual(config["HA_HOST"], "homeassistant.local")
        self.assertEqual(config["TUYA_LOCAL_KEY"], "")


if __name__ == "__main__":
    unittest.main()