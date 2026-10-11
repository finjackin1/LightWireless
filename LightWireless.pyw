import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lightwireless.__main__ import main  # noqa: E402

main()
