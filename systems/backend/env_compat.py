"""
Accept the environment variables' names from before the rename to airlock.

Every setting was SHOEBOX_* until v5.6.3 and is AIRLOCK_* now. The start
scripts pass the new names, and deploy/airlock-env.sh maps old ones from an
old env file. This covers what the shell did not: a container started some
other way, a variable exported by hand, a test harness. A SHOEBOX_X is copied
to AIRLOCK_X when AIRLOCK_X is not set, into os.environ, so workflows started
as subprocesses inherit it.

Imported first by app.py, before anything reads a setting. Remove in v5.7.
"""

import os

OLD, NEW = 'SHOEBOX_', 'AIRLOCK_'


def apply(environ=os.environ):
    """Copy old-named settings to their new names. Returns the old names used."""
    used = []
    for key in list(environ):
        if key.startswith(OLD):
            new = NEW + key[len(OLD):]
            if new not in environ:
                environ[new] = environ[key]
                used.append(key)
    return sorted(used)


RENAMED = apply()
if RENAMED:
    import sys
    print('note: settings read under their old names, now AIRLOCK_*: '
          + ', '.join(RENAMED), file=sys.stderr)
