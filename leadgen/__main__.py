import os
import sys

from .cli import main

try:
    sys.exit(main())
except BrokenPipeError:  # output piped into `head` etc.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    sys.exit(0)
