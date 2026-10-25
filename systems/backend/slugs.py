"""
slugs.py
========
One derivation of "a name a human typed" into "a segment a URL and a DNS label
can both carry" (documentation/specs/albums-enhancement-spec.md section 3.1).

Two different things need it — a catalog and a deployment — and they must agree,
because the URL is the pair:

    https://airlock.lan/albums/family/summer-2019
                               ^^^^^^ ^^^^^^^^^^^
                               catalog  deployment

The rule is a DNS label, which is the stricter of the two grammars: lowercase
letters, digits and hyphens, starting and ending with an alphanumeric, at most
40 characters. A URL segment would take much more; a subdomain would not, and
the subdomain form (section 7.2) has to keep working.

This module deliberately has no imports beyond the standard library and no
knowledge of either database, so the uniqueness check is passed in. Catalog
slugs are unique across catalog_meta; deployment slugs are unique within one
catalog's album_deployments. Neither scans the other — that is the whole point
of putting the catalog in the URL.
"""

import re

MAX_LENGTH = 40

# Start and end alphanumeric, hyphens inside, 3-40 characters. Anchored, and
# the same expression the nginx wildcard server_name uses so a name that is
# accepted here cannot fail to route.
SLUG_PATTERN = r'^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'
_SLUG_RE = re.compile(SLUG_PATTERN)


def is_valid(slug):
    """True when *slug* is a usable URL segment and DNS label."""
    return bool(slug) and bool(_SLUG_RE.match(slug))


def slugify(name, fallback='item'):
    """
    Derive a candidate slug from a display name. Never guaranteed unique — see
    unique_slug() for that.

        'Family Pictures'  -> 'family-pictures'
        'Summer 2019!'     -> 'summer-2019'
        '  --  '           -> 'item'   (the fallback; something must be returned)
    """
    text = (name or '').strip().lower()
    # Anything that is not a letter or digit becomes a separator, and runs of
    # separators collapse. Accented characters are dropped rather than
    # transliterated: guessing that 'ñ' means 'n' is a per-language decision,
    # and the user can always type the slug they want.
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    text = text[:MAX_LENGTH].strip('-')

    if len(text) < 3:
        # Too short to be a valid label. Pad from the fallback rather than
        # refusing, so a catalog called "Ed" still gets a working URL.
        text = (text + '-' + fallback).strip('-')[:MAX_LENGTH].strip('-')
    return text if is_valid(text) else fallback


def unique_slug(candidate, taken, fallback='item'):
    """
    *candidate*, or the first free `candidate-2`, `candidate-3`, … .

    `taken` is a container answering `in` — a set of existing slugs in whatever
    scope this slug has to be unique in. Truncation keeps the result inside
    MAX_LENGTH, so a 40-character name does not produce a 42-character slug that
    the database accepts and nginx will not route.
    """
    base = slugify(candidate, fallback=fallback)
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        tail = f'-{suffix}'
        trimmed = base[:MAX_LENGTH - len(tail)].rstrip('-')
        attempt = f'{trimmed}{tail}'
        if attempt not in taken:
            return attempt
    raise ValueError(f'could not find a free slug based on "{base}"')


def problem(slug):
    """A complaint string for a slug the user typed, or None when it is fine."""
    if not slug:
        return 'A slug is required.'
    if len(slug) > MAX_LENGTH:
        return f'A slug can be at most {MAX_LENGTH} characters.'
    if not is_valid(slug):
        return ('A slug may contain only lowercase letters, digits and hyphens, '
                'must start and end with a letter or digit, and must be at '
                'least 3 characters — it is used as both a URL segment and a '
                'subdomain.')
    return None
