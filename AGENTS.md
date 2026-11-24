# AGENTS.md: WikipediaAttractionsDatabase

Contesto completo per un agente (Claude Code o altri) che lavora in questo repo.
Aggiornato al **24 novembre 2026**. Lingua del progetto: **italiano** (codice commentato, messaggi, commit, documentazione).

## 1. Che cosa è

Questo repo nasce dalla cartella `attractions-pipeline/` di Easy Travel (repo `MindTrip`, un planner di viaggio) ed è stato isolato il
24 novembre 2026. Easy Travel è il primo consumatore del catalogo: l'**AI compone l'itinerario giorno per giorno** leggendo le attrazioni.
Il repo costruisce, **solo con dati aperti**, il catalogo delle attrazioni che l'AI userà:
per ogni città, che cosa c'è da vedere e quanto è famoso.

- Fonti: **OpenStreetMap** (luoghi, via Overpass), **Wikidata** (QID: identità comune), **QRank** (visite Wikimedia per QID).
- Google Places non si salva (termini d'uso): al massimo si chiede in diretta per i pochi luoghi di un itinerario.
- Perimetro: **50 città turistiche** in 31 paesi (Londra, Parigi, Roma, Tokyo… Bruges). Hotel, B&B e voli NON sono qui.
- La chiave che lega tutto è il **QID Wikidata** (Roma `Q220`, Colosseo `Q10285`).

## 2. Stato attuale (24 novembre 2026)

| Passo | Stato | Numeri |
| --- | --- | --- |
| 1. Anagrafica città (`wikidata_cities`) | fatto | 50 città; 8 con area scelta a mano (`area_method`) |
| 2. Oggetti OSM (bronze, `osm_objects`) | fatto | 441.648 oggetti con nome |
| 3. Gold (`osm_objects_clean`, vista materializzata) | fatto, **da ripulire** | 100.707 luoghi (solo con QID, 1 riga per città e QID) |
| 4. Punteggio (`wikidata_qrank` + `osm_objects_scored`) | fatto | 83.163 QID con punteggio, QRank versione 2026-11-15, 50/50 città |

**Problema principale aperto (PA-58, priorità A):** il filtro del rumore non scarta i confini amministrativi
e gli elementi geografici. Nel gold ci sono 6.096 oggetti con il tag `boundary`; in **42 città su 50 il primo posto
della classifica è un confine** (a Roma: «Italia», «Roma», poi il Colosseo). Vedi §9.

Database Supabase: progetto «MarcoGalimi00's Project», eu-west-1, Postgres 17, piano gratuito (~500 MB; oggi 310 MB).

## 3. Regole di lavoro (obbligatorie)

1. **Mai modificare il database Supabase senza permesso esplicito.** Prima mostra l'SQL (o il comando con `--apply`)
   e aspetta un «sì». Vale per migrazioni, `create/replace function`, `refresh`, e per ogni script lanciato con `--apply`.
   Le query di sola lettura (`select`, `explain`) vanno bene.
2. **Segreti mai in git né in chat**: `.env` è ignorato da git; il modello è `.env.example`. Non stampare chiavi o password nei log.
3. **Gli script sono dry-run per default**: scrivono solo con `--apply`. Mantieni questa convenzione in ogni script nuovo.
4. **Il bronze non si tocca nella struttura**: `osm_objects` contiene tutto (rumore compreso), chiave `(osm_type, osm_id, city_wikidata)`.
   Le regole stanno nelle viste e nella funzione `osm_noise_reason` (decisione D08).
5. **Regole uguali per tutte le città**: niente eccezioni per singola città nel filtro (D11). Le eccezioni sull'area di una città
   stanno nei dati (`wikidata_cities.area_method`, D12).
6. **Le viste materializzate non si aggiornano da sole**: dopo aver cambiato `osm_noise_reason` o caricato città serve
   `python refresh_scores.py --apply --clean`.
7. **Migrazioni**: file numerati in `sql/` (prossimo: `005_...sql`), applicati a mano su Supabase. Il file nel repo è la copia
   di riferimento: deve essere identico a ciò che è applicato. Se cambi una funzione già creata (es. in `002`), aggiorna anche quel file.
   Controlla con `git diff --stat` prima del commit (il 13 novembre la 004 è finita su `main` vuota).
8. **Stile del codice**: Python standard, commenti e messaggi in italiano, nessuna chiamata per singolo luogo verso API esterne
   (usare file bulk o join), un errore su una città non deve fermare le altre, scritture idempotenti (upsert o sostituzione completa).
9. **Git**: lavora su un branch (`feature/...`, `fix/...`, `docs/...`), messaggi di commit in italiano, poi PR o avanti veloce su `main`.
10. **Documentazione**: quella completa è un vault Obsidian **fuori da questo repo** (nel Project claude.ai «Easy Travel»).
    I vecchi vault nel repo `MindTrip` sono fermi al 13 novembre e non fanno da fonte di verità. Quando cambi comportamento, nel riepilogo finale elenca quali note del vault
    vanno aggiornate (es. «Schema SQL e viste», «Punti aperti», «Come si usa la pipeline»).

## 4. Architettura dei dati

```
Overpass (OSM) ──load_attractions.py──► osm_objects (BRONZE, tutto)
                                            │  refresh: osm_noise_reason(tags) is null
                                            │           and wikidata is not null
                                            │           distinct on (city_wikidata, wikidata)
                                            ▼
                                   osm_objects_clean (GOLD, vista materializzata)
QRank (qrank.csv.gz) ──refresh_scores.py──► wikidata_qrank ──┐
                                            │                 │ left join su wikidata
                                            ▼                 ▼
                                   osm_objects_scored (GOLD + qrank, vista materializzata)
                                            │
                                            ▼
                              Itinerario AI: where city_wikidata = 'Q…' order by qrank desc
```

- Un solo oggetto per tutte le città; l'AI filtra per `city_wikidata` (indice `osm_objects_scored_rank`).
- Refresh `CONCURRENTLY`: chi legge vede la versione precedente intera; la coerenza vale per singola query
  (l'AI deve leggere una città con una query sola).
- Oggetti senza QID: restano nel bronze, **fuori dal gold** (decisione D14; chiude il vecchio punto sul «ripiego»).
- Tra le copie OSM dello stesso luogo vince `relation`, poi `way`, poi `node`; `n_copie` dice quante erano.

## 5. Oggetti del database (prefissi `wikidata_` e `osm_`; le altre tabelle sono dell'app, non toccarle)

| Oggetto | Tipo | Migrazione | Contenuto / note |
| --- | --- | --- | --- |
| `wikidata_cities` | tabella | 001 (+ vincolo in 003) | 50 città; `wikidata` (QID), `name`, `country_code`, `population`, `area_method` (`confine`/`raggio`/vuoto), `relation_id`, `radius_km`, `app_city_id` (vuota: legame con `cities` dell'app da fare) |
| `osm_objects` | tabella (bronze) | 002 | `osm_type`, `osm_id`, `city_wikidata`, `lat`, `lon`, `name` (not null), `wikidata`, `tags jsonb` |
| `osm_noise_reason(t jsonb)` | funzione sql immutable | 002 | restituisce `trasporti` / `alloggi_info` / `ambasciate` / `uffici` oppure NULL (si tiene). `search_path = ''` |
| `osm_objects_noise` | vista semplice | 002 | gli scartati con `noise_reason` |
| `osm_ingestion_log` / `osm_ingestion_last` | tabella / vista | 003 | una riga per città per corsa di `load_attractions.py --apply`; la vista dà l'ultima corsa di ogni città |
| `wikidata_qrank` | tabella | 004 | `wikidata` (PK), `qrank` (visite 52 settimane), `source_version` (date), `fetched_at`; sostituita per intero a ogni caricamento |
| `osm_objects_clean` | **vista materializzata** | 004 | gold: 50 colonne estratte dai tag + `n_copie`; indice unico `(city_wikidata, wikidata)` |
| `osm_objects_scored` | **vista materializzata** | 004 | `clean` + `qrank` + `qrank_version`; letta dall'AI |
| `wikidata_pageviews` | tabella vuota | 002 | non usata dopo D13 (PA-62: tenerla o toglierla) |

Sicurezza: RLS attiva senza regole su tutte le tabelle; le viste materializzate non hanno RLS, quindi la 004 fa
`revoke all ... from anon, authenticated`. Se crei nuove viste materializzate, fai lo stesso. La lettura da parte dell'app è da decidere (PA-39).

## 6. Script e comandi

Tutti si lanciano da la radice del repo con il venv attivo. Senza `--apply` non scrivono.

| Script | Fa | Comandi tipici |
| --- | --- | --- |
| `build_cities.py` | trova il QID di ogni città e giudica l'affidabilità → `seed/citta_50.csv` (fuori da git) | `python build_cities.py [--only Roma]` |
| `load_cities.py` | carica le città in `wikidata_cities` | `python load_cities.py --apply` |
| `load_attractions.py` | scarica da Overpass (cache in `data/overpass/`), upsert in `osm_objects` a blocchi di 500, scrive il log | `python load_attractions.py --only Q220` (prova) · `--apply [--refresh] [--notify]` |
| `fix_city_areas.py` | per le città con ultima corsa `nessun_oggetto` propone confine o raggio → `seed/aree_proposte.csv`; con `--csv … --apply` scrive l'area | `python fix_city_areas.py` · `--interattivo` · `--csv seed/aree_proposte.csv --apply` |
| `refresh_scores.py` | refresh del gold (`--clean`), join DuckDB tra QRank e i QID del gold, sostituisce `wikidata_qrank`, refresh di `scored` | `python refresh_scores.py` (dry-run) · `--apply` · `--apply --clean` · `--force` · `--via-file` · `--notify` |
| `tools/gen_code_map.py` | rigenera le note «mappa funzioni» del vecchio vault (non conosce `refresh_scores.py`, PA-48) | `python tools/gen_code_map.py` |

Ordine completo: `build_cities` → revisione CSV → `load_cities --apply` → `load_attractions --apply`
→ (città vuote: `fix_city_areas` → `load_attractions --only QID --apply`) → **`refresh_scores --apply --clean`**.
Aggiornamento periodico del punteggio (QRank esce ogni giorno): `refresh_scores --apply` (si ferma con «niente da fare» se la versione è la stessa).

`refresh_scores.py` riusa `load_env`, `send_mail`, `Tee` da `load_attractions.py`. Ha un `TODO(Marco)` in `connection_hint()`
(consigli per gli errori di connessione): lasciarlo all'utente salvo richiesta (PA-63).

## 7. Ambiente e segreti

**Configurazione pubblica**: `config.toml` (letto da `config.py`) contiene l'elenco delle città, i tipi di luogo, gli indirizzi di Wikidata/Overpass/QRank e il contatto dello `User-Agent` (`[http] contact`, da impostare: contiene il segnaposto `OWNER`). I segreti restano in `.env`.

Variabili in `.env` (lette da `load_env`, che non sovrascrive quelle già nell'ambiente):

| Variabile | Serve a |
| --- | --- |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | API REST (`load_cities`, `load_attractions`, `fix_city_areas`) |
| `SUPABASE_DB_URL` | `refresh_scores.py`: Postgres diretto per `REFRESH` e `COPY`. **Session pooler**, porta 5432, utente `postgres.<ref>`, password del database (non la service_role). Da Supabase → Connect → scheda Direct → Session pooler |
| `SMTP_USER`, `SMTP_APP_PASSWORD`, `NOTIFY_TO` | `--notify` (password per le app di Gmail) |

Dipendenze (`requirements.txt`): `requests`, `truststore`, `duckdb`, `psycopg[binary]`, `openpyxl` (facoltativa), `supabase` (solo il notebook).
Gli script usano l'API REST con `requests`, non la libreria `supabase`.

Insidie già incontrate:
- `db.<ref>.supabase.co` è solo IPv6: usare il Session pooler. La porta 6543 (Transaction pooler) non va bene per `REFRESH`/`COPY`.
- Rete aziendale con ispezione HTTPS: DuckDB non vede i certificati di sistema → `refresh_scores.py --via-file` (scarica con `requests` + `truststore`, verifica lo SHA-256).
- Sul PC aziendale SentinelOne mette in quarantena i file del venv. Lo sviluppo è sul **Mac personale**, venv **fuori dal repo** (`~/venvs/attractions`): il `.gitignore` non esclude i venv.
- Python molto recente (3.15) può non avere pacchetti precompilati: se `pip install` fallisce, 3.13.
- GitHub dal Mac: serve un token personale (classic, scope `repo`) al posto della password.

### Prove in locale senza toccare Supabase

Il modo usato finora per verificare migrazioni e script: un Postgres 16 locale con le migrazioni vere e dati finti.
Prima di `sql/001` creare `public.cities (id bigint primary key)` e i ruoli `anon`, `authenticated`;
poi applicare 001→004, inserire poche righe in `wikidata_cities` e `osm_objects`, e servire un QRank finto
(`datapackage.json` + `qrank-YYYYMMDD.csv.gz` con SHA-256) da `python -m http.server`, puntando lo script con
l'opzione nascosta `--qrank-base http://127.0.0.1:PORTA/` e `SUPABASE_DB_URL` al Postgres locale.
Usa questo schema per provare ogni modifica a `osm_noise_reason` o alle viste prima di proporla.

## 8. Decisioni prese (non rimetterle in discussione senza motivo)

| Sigla | Decisione |
| --- | --- |
| D01 | Il QID Wikidata è la chiave di join tra tutte le fonti |
| D02 | API di Wikidata, non SPARQL |
| D03 | Una città è un «luogo» se il tipo è in elenco o ha popolazione |
| D04 | Paesi noti e override manuali dei QID in `seed/override_qid.csv` (tracciato) |
| D05 | Perimetro: 50 città, solo attrazioni (stadi compresi) |
| D06 | Segreti Supabase fuori da git |
| D07 | Nel giudizio delle città i rivali «non luoghi» non competono |
| D08 | Si salva tutto nel bronze e si filtra nelle viste |
| D09 | Prefissi `wikidata_` / `osm_` per non toccare le tabelle dell'app |
| D10 | Dato grezzo in tabella, formula del punteggio nella vista |
| D11 | Regole del rumore semplici e uguali per tutte le città |
| D12 | L'area di una città senza confine OSM si scrive nei dati (`area_method`), scelta da una persona |
| D13 | Punteggio = visite delle ultime 52 settimane da **QRank** (1 file, CC0), unito con DuckDB in memoria: nessuna chiamata per singolo luogo |
| D14 | Gold e punteggio come **viste materializzate** per tutte le città, solo luoghi con QID, 1 riga per (città, QID); refresh da script, non da Supabase; l'AI legge dati pronti |

## 9. Punti aperti del catalogo (in ordine di priorità)

| Sigla | Pri. | Punto | Che cosa serve |
| --- | --- | --- | --- |
| **PA-58** | **A** | Confini (`boundary`) ed elementi geografici (fiumi, mari, catene montuose: Danubio, Mare del Nord, Alpi, Lago Ontario) nel gold, in cima alle classifiche | Nuova regola in `osm_noise_reason`, coerente con D11: es. `boundary = administrative` → `confini`; `natural` in (`water`, `strait`, `bay`, …) o `place` in (`sea`, `ocean`, …) → `geografia`. **Attenzione**: `boundary=national_park` / `protected_area` possono essere mete vere: valutare prima con query di conteggio. Poi `refresh_scores.py --apply --clean` e controllo delle prime 20 di ogni città |
| PA-25 | A | Regole del rumore tarate su Roma (Via Sacra tra i trasporti, eccezione uffici nata dal Senato) | Misurare su tutte le città gli scartati `trasporti` con `historic`/`tourism` e gli `uffici` con QID |
| PA-19 / PA-56 | A | Città senza confine OSM e prima prova completa del log + `fix_city_areas.py` | Sistemare le città `nessun_oggetto`, riscaricarle, chiudere con `refresh_scores --apply --clean` |
| PA-59 | B | Aziende, aeroporti, club nel gold (Rocket Lab, FC Bayern AG, Changi, Arsenal) | Regola per `aeroway`, `office` di aziende, `club`; gli stadi restano |
| PA-60 | B | Monumenti con il QID della **persona** raffigurata (statua di F. D. Roosevelt a Buenos Aires, José Martí a Cancún) | Contare i QID che sono persone (Wikidata `P31 = Q5`) e decidere se escluderli o usare `subject:wikidata` |
| PA-24 | B | Classificazione per categoria (museo, chiesa, parco, sito archeologico…) | Serve anche alla futura profilazione utente a card («una card per categoria»). Partire dai tag `tourism`, `historic`, `amenity`, `leisure` del gold |
| PA-28 | B | Formula del punteggio | Oggi `qrank` grezzo; rivedere (log, normalizzazione per città, pesi per categoria) dopo PA-58/59/60, nella definizione di `osm_objects_scored` |
| PA-36 | B | Legame città del catalogo ↔ `cities` dell'app | Popolare `wikidata_cities.app_city_id` |
| PA-38 | B | Spazio: 310 MB su ~500 | Rimisurare dopo ogni ampliamento |
| PA-20 / PA-21 | B | Confini troppo grandi (Sydney e Melbourne con lo stato in cima) / luoghi fuori dal comune (Vaticano) | Confronto confine vs cerchio |
| PA-45 | C | Aggiornamento periodico automatico | Proposta: GitHub Action settimanale con `refresh_scores.py --apply --notify` e segreti di Actions |
| PA-39 | C | Lettura da parte dell'app (chiave pubblica) | `grant select` mirato o vista/funzione di lettura sopra la vista materializzata |
| PA-48 | C | `gen_code_map.py` non conosce `refresh_scores.py` | Aggiungerlo |
| PA-61 | C | Dipendenza da QRank (un solo manutentore) | Ripiego: dump mensili `pageview_complete` |
| PA-62 | C | `wikidata_pageviews` vuota | Tenerla o toglierla in una migrazione |
| PA-63 | C | `connection_hint()` da scrivere | Lo scrive l'utente |
| PA-64 | C | Vecchie cartelle vault | Chiuso: non sono state portate in questo repo |
| PA-49 | B | Obblighi della licenza ODbL di OpenStreetMap | Controllo legale prima di usare i dati nel prodotto |

## 10. Query utili (sola lettura)

```sql
-- classifica di una città
select name, qrank, n_copie, coalesce(tourism, historic, amenity, leisure) as tipo
from osm_objects_scored where city_wikidata = 'Q220'
order by qrank desc nulls last limit 20;

-- i non-luoghi in cima (PA-58)
select city_wikidata, name, boundary, "natural", qrank from osm_objects_scored
where boundary is not null order by qrank desc nulls last limit 30;

-- valori di boundary nel gold, per scegliere la regola
select boundary, count(*), count(qrank) from osm_objects_clean
where boundary is not null group by 1 order by 2 desc;

-- prime 3 di ogni città
with r as (select city_wikidata, name, boundary,
             row_number() over (partition by city_wikidata order by qrank desc nulls last) pos
           from osm_objects_scored)
select city_wikidata, string_agg(name || coalesce(' [' || boundary || ']', ''), ' | ' order by pos)
from r where pos <= 3 group by 1;

-- scartati per motivo / versione di QRank caricata / ultima corsa per città
select noise_reason, count(*) from osm_objects_noise group by 1 order by 2 desc;
select source_version, count(*), max(fetched_at) from wikidata_qrank group by 1;
select status, count(*) from osm_ingestion_last group by 1;
```

Nota: un `count(*)` su `osm_objects_noise` ricalcola `osm_noise_reason` su tutto il bronze ed è lento; il gold invece è materializzato e veloce.

## 11. Contesto di prodotto utile per i prossimi passi

- L'AI dell'itinerario legge `osm_objects_scored` per la città di arrivo: deve ricevere solo mete vere, ordinate per fama.
- È in progettazione una **profilazione dell'utente a card** («swipe sì/no») prima dell'itinerario: si mostrano attrazioni di tipologie diverse,
  almeno una card per categoria, con frequenze proporzionali a quanto ogni categoria è presente nel paese.
  Per questo la classificazione per categoria (PA-24) è il passo che segue la pulizia del gold.
- Il punteggio misura la **fama, non la qualità** (un luogo famoso ma deludente sale lo stesso).
