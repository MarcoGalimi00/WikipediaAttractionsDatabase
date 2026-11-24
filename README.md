# WikipediaAttractionsDatabase

Catalogo delle attrazioni turistiche di 50 città del mondo, costruito **solo con dati aperti**
(OpenStreetMap, Wikidata, QRank / visite Wikimedia) e con un punteggio di popolarità per ogni luogo.

> **Stato: lavori in corso.** Gli script esistono e girano, ma il progetto non è finito: il catalogo contiene ancora rumore
> (per esempio confini amministrativi in cima alle classifiche, vedi [punti aperti](AGENTS.md)) e oggi i dati vengono
> scritti su un database **Postgres/Supabase**. L'obiettivo è che chiunque possa scaricarsi il database in locale a propria discrezione;
> quel passo (esportazione o dump pronto da scaricare) **non è ancora implementato**.

## Come funziona

```
Overpass (OSM) ──load_attractions.py──► osm_objects (bronze, tutto)
                                            │ filtro del rumore + solo luoghi con QID Wikidata
                                            ▼
                                   osm_objects_clean (gold)
QRank (qrank.csv.gz) ──refresh_scores.py──► wikidata_qrank
                                            ▼
                                   osm_objects_scored (gold + punteggio)
```

La chiave che lega le fonti è il **QID Wikidata** (Roma `Q220`, Colosseo `Q10285`).
Il punteggio è la fama (visite alle pagine Wikipedia nelle ultime 52 settimane), non la qualità.

## Requisiti

- Python 3.11–3.13 (3.15 può non avere pacchetti precompilati)
- Un database Postgres (oggi i flussi sono pensati per un progetto [Supabase](https://supabase.com))

## Installazione

```bash
git clone https://github.com/OWNER/WikipediaAttractionsDatabase.git
cd WikipediaAttractionsDatabase
python3 -m venv ~/venvs/attractions      # meglio fuori dal repo
source ~/venvs/attractions/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # segreti: poi compila i valori
# poi apri config.toml: sostituisci OWNER in [http] contact e, se vuoi, cambia l'elenco delle città
```

## Configurazione

- `.env` (non in git): chiavi Supabase e SMTP. Modello: `.env.example`.
- `config.toml` (in git): perimetro e parametri pubblici. Percorso alternativo con `WAD_CONFIG=/percorso/file.toml`.
  **Prima di usare gli script imposta `[http] contact`**: è l'unico segnaposto (`OWNER`) da sostituire; finisce nello
  `User-Agent` verso Wikidata, Overpass e QRank, che chiedono di dichiararsi.

## Uso

Gli script sono **dry-run per default**: scrivono sul database solo con `--apply`.
Prima di usarli crea lo schema applicando a mano le migrazioni in [`sql/`](sql/) in ordine (001 → 004).

```bash
python build_cities.py                    # trova il QID di ogni città → seed/citta_50.csv
python load_cities.py --apply             # carica le città
python load_attractions.py --apply        # scarica da Overpass e carica gli oggetti OSM
python refresh_scores.py --apply --clean  # ricalcola gold e punteggio
```

Ordine completo, opzioni e insidie note: [AGENTS.md](AGENTS.md).

## Struttura del repo

| Percorso | Contenuto |
| --- | --- |
| `build_cities.py`, `load_cities.py`, `fix_city_areas.py` | anagrafica delle 50 città |
| `load_attractions.py` | scarico da Overpass e caricamento |
| `refresh_scores.py` | punteggio QRank e viste materializzate |
| `config.toml`, `config.py` | configurazione: città, tipi di luogo, indirizzi dei servizi, contatto per lo User-Agent |
| `sql/` | migrazioni numerate dello schema |
| `seed/` | input a mano (`override_qid.csv`) |
| `tools/` | utilità di sviluppo |
| `AGENTS.md`, `CLAUDE.md` | contesto completo, decisioni e punti aperti (anche per gli agenti AI) |

## Contribuire

Leggi [CONTRIBUTING.md](CONTRIBUTING.md). Lingua del progetto: italiano.

## Licenze e attribuzioni

- Il **codice** è sotto licenza [MIT](LICENSE).
- I **dati** hanno licenze proprie e non sono coperti dalla licenza del codice: vedi [DATA_LICENSES.md](DATA_LICENSES.md)
  (OpenStreetMap è ODbL e richiede attribuzione; Wikidata e QRank sono CC0).
