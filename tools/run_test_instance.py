"""起一个独立的测试实例（不写 web/config.local.js，不影响正在用的那个）。

    set DB_PATH=<副本库> & set BRAIN_PORT=8010
    .venv\\Scripts\\python.exe tools\\run_test_instance.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402

from brain.app_brain import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("BRAIN_PORT", "8010"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
