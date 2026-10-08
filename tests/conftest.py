import sys
from pathlib import Path

# The eval scripts import each other as plain modules (python eval/run_eval.py), so tests do too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
