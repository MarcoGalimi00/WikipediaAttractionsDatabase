"""Lettura di config.toml (percorso alternativo: variabile d'ambiente WAD_CONFIG).

Contiene il perimetro (città, tipi di luogo) e gli indirizzi dei servizi pubblici.
I segreti restano nel file .env: qui non ce ne sono.
"""

import os
import tomllib  # libreria standard da Python 3.11
from pathlib import Path

BASE = Path(__file__).parent
PATH = Path(os.environ.get("WAD_CONFIG") or BASE / "config.toml")

with open(PATH, "rb") as f:
    _cfg = tomllib.load(f)

CITIES = [tuple(c) for c in _cfg["cities"]]
PLACE_TYPES = set(_cfg["wikidata"]["place_types"])
KNOWN_COUNTRIES = dict(_cfg["wikidata"]["known_countries"])
MIN_RATIO = float(_cfg["wikidata"]["min_ratio"])

WIKIDATA_API = _cfg["servizi"]["wikidata_api"]
OVERPASS_URL = _cfg["servizi"]["overpass_url"]
QRANK_BASE = _cfg["servizi"]["qrank_base"]


def user_agent(script=None):
    """User-Agent dichiarato verso Wikimedia e Overpass (contatto preso da config.toml)."""
    ua = f"WikipediaAttractionsDatabase/0.1 ({_cfg['http']['contact']}"
    return ua + (f"; {script})" if script else ")")
