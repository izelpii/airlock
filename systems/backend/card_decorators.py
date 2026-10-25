"""
Card decorators: the artwork strip across the top of a catalog or deployment
card.

Each theme is a folder in frontend/public/ holding cardDeco1..N.png, every one
with a black-and-white twin cardDeco<n>bw.png. A card is given one at random
when its catalog or deployment is created and keeps it - artwork that changed
on every visit would read as a glitch. The colour image is shown while the
thing is live (the open catalog, an enabled deployment), the twin otherwise.

Stored as "<theme>/cardDeco<n>", without an extension: the frontend adds
".png" or "bw.png", and a new theme needs no schema change.

Dependency-free so catalog.py and models.py can both import it.
"""

import random

# Catalogs moved to theme2 (2026-10-20), deployments keep theme1; each has 20
# decorators at 720x100. Stored picks on another theme are moved across with
# their number kept (catalog.py for catalogs, models.py for deployments).
CATALOG_THEME = 'theme2'
DEPLOYMENT_THEME = 'theme1'
DECORATORS_PER_THEME = 20


def random_decorator(theme, count=DECORATORS_PER_THEME):
    """One of the theme's decorators, picked at random."""
    return f'{theme}/cardDeco{random.randint(1, count)}'
