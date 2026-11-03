"""Start mcp_server.py with the repo's virtualenv Python, whatever `python` launched this.

.mcp.json points here so the same config works on Windows (.venv/Scripts) and macOS/Linux (.venv/bin).
"""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
candidates = [root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"]
python = next((str(p) for p in candidates if p.exists()), sys.executable)
sys.exit(subprocess.call([python, str(root / "mcp_server.py")], cwd=root))
