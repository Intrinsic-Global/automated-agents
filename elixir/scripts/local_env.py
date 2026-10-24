"""Load ignored, shell-style local settings without overriding the process environment."""

import os
from pathlib import Path
import shlex


def load_local_env():
    path = Path(os.environ.get('SYMPHONY_ENV_FILE', Path(__file__).resolve().parents[1] / '.env'))
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, raw_value = line.split('=', 1)
        name = name.strip()
        if not name or not name.replace('_', 'a').isalnum() or name[0].isdigit():
            continue
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        value = os.path.expandvars(' '.join(parts))
        os.environ.setdefault(name, value)
