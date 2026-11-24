# Changelog

Formato basato su [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

## [Non rilasciato]

### Aggiunto
- Repo isolato dalla cartella `attractions-pipeline/` del progetto Easy Travel (MindTrip).
- Script: `build_cities.py`, `load_cities.py`, `load_attractions.py`, `fix_city_areas.py`, `refresh_scores.py`.
- Migrazioni SQL 001–004 (città, oggetti OSM, log di caricamento, QRank e viste gold).
- File del repo: README, LICENSE (MIT), DATA_LICENSES, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, modelli GitHub e CI di base.

### Modificato
- Configurazione in `config.toml` (letto da `config.py`): elenco delle città, tipi di luogo, indirizzi di Wikidata/Overpass/QRank
  e contatto dello `User-Agent`, prima scritti nel codice. Il contatto personale è stato tolto (sostituire `OWNER` in `[http] contact`).
