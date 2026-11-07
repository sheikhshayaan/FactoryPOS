"""Factory Management System - launcher.  Run:  python main.py"""
import os
import sys

# make `config`, `database`, `services`, `ui`, `reports` importable from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
