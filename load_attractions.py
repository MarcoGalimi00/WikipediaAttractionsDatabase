#!/usr/bin/env python3
"""Scarica da Overpass gli oggetti OSM delle città e li scrive in public.osm_objects di Supabase.

Per ogni città della lista: una query Overpass (confine amministrativo + filtri larghi), risposta
salvata in data/overpass/<QID>.json, righe scritte su Supabase con un upsert. Il rumore (strade,
ferrovie, alloggi, ambasciate, uffici) NON si toglie qui: si salva tutto e si filtra con le viste
SQL osm_objects_noise / osm_objects_clean (vedi sql/002_osm_objects.sql).

Uso (dalla radice del repo):
    python load_attractions.py                          # dry-run su seed/citta_50.csv (se manca, legge le città da Supabase)
    python load_attractions.py --apply                  # come sopra e poi scrive su Supabase
    python load_attractions.py --cities mie_citta.txt   # un'altra lista (CSV con colonna wikidata, oppure un QID per riga)
    python load_attractions.py --only Q220,Q90          # solo queste città, ignorando la lista
    python load_attractions.py --refresh                # riscarica anche se il file in data/overpass esiste già
    python load_attractions.py --apply --notify         # a fine corsa manda per mail l'esito (successo o errori)

Area di ricerca. Di norma è il confine amministrativo che porta il codice Wikidata della città. Per le
città in cui OSM non ha quel confine (risposta vuota, "nessun oggetto") l'area si indica nella riga di
wikidata_cities: area_method = 'confine' con relation_id, oppure 'raggio' con radius_km (cerchio attorno
a lat/lon). Si imposta con fix_city_areas.py. Le città con quelle colonne vuote usano il confine per codice.
Con --apply ogni città scrive anche una riga in osm_ingestion_log (sql/003): esito, numero di oggetti, area usata.

Servono, solo con --apply, due variabili nel file .env (mai in git) o nell'ambiente:
    SUPABASE_URL                  es. https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY     la chiave con pieni poteri: resta solo sul tuo PC
Per --notify (mail di esito), nel .env:
    SMTP_USER                     l'indirizzo Gmail che invia (es. il tuo)
    SMTP_APP_PASSWORD             "password per le app" di Google (non la password dell'account)
    NOTIFY_TO                     facoltativo: destinatario; se manca scrive a SMTP_USER

Rilanciabile senza danni: le risposte già scaricate si riusano (data/overpass, solo se non vuote e se
l'area non è cambiata) e la scrittura è un upsert sulla chiave (osm_type, osm_id, città). Un oggetto
sparito da OSM resta però nella tabella.
"""

import argparse  # opzioni da riga di comando
import csv  # lettura della lista di città in formato CSV
import json  # salvataggio e lettura delle risposte di Overpass
import os  # variabili d'ambiente
import re  # espressioni regolari per i controlli
import smtplib  # invio della mail di esito
import socket  # nome del computer, per la mail
import sys  # uscita con codice di errore e messaggi su stderr
import time  # pause tra le chiamate e tra i tentativi
import traceback  # testo dell'errore imprevisto, per la mail
from datetime import datetime  # orari di inizio e fine
from email.message import EmailMessage  # costruzione della mail
from pathlib import Path  # percorsi indipendenti dal sistema operativo

import requests  # chiamate HTTP (Overpass e API REST di Supabase)

import config  # indirizzi dei servizi e User-Agent: config.toml

# truststore: usa l'archivio dei certificati di Windows invece di quello di certifi. Serve nelle reti
# aziendali che ispezionano il traffico HTTPS. Facoltativo: se non è installato si usa certifi.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

BASE = Path(__file__).parent  # cartella dello script (radice del repo)
TABLE = "osm_objects"  # tabella di destinazione (sql/002_osm_objects.sql)
CITIES_TABLE = "wikidata_cities"  # tabella delle città: osm_objects.city_wikidata ci punta con una chiave esterna
BATCH = 500  # righe per ogni chiamata di scrittura
DEFAULT_CITIES = str(BASE / "seed" / "citta_50.csv")  # lista di default; se manca, le città si leggono da Supabase

OVERPASS_URL = config.OVERPASS_URL  # config.toml, [servizi] overpass_url
# Overpass chiede di dichiarare chi chiama. Metti un contatto vero al posto di "contatto".
HEADERS = {"User-Agent": config.user_agent()}  # contatto in config.toml, [http] contact
TIMEOUT = 330  # secondi di attesa della risposta: poco oltre il timeout della query (300)
MAX_TRIES = 5  # tentativi per città prima di rinunciare
RETRY_STATUS = {429, 502, 503, 504}  # server occupato o scaduto: si riprova dopo una pausa
LOG_TABLE = "osm_ingestion_log"  # log delle corse (sql/003_osm_ingestion_log.sql)
AREA_OFFSET = 3_600_000_000  # un'area Overpass ha come id l'id della relation più questo numero
LOADABLE = {"ok", "override"}  # valori di check (citta_50.csv) che si caricano

QID = re.compile(r"Q[0-9]+")  # forma del codice Wikidata


# --------------------------------------------------------------------------- lista di città

def read_cities(path):
    """Legge la lista di città. Restituisce ([(QID, nome)], [messaggi di scarto], [errori]).

    Formati accettati:
      - CSV con colonna "wikidata" (come seed/citta_50.csv); se c'è la colonna "check" si
        caricano solo ok e override; se c'è "city" si usa come nome nei messaggi;
      - testo semplice: un codice per riga ("Q220" oppure "Q220 Roma"), righe con # ignorate.
    """
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    cities, skipped, errors, seen = [], [], [], set()
    if not lines:
        return cities, skipped, ["il file è vuoto"]

    def add(qid, name, where):
        """Aggiunge una città alla lista dopo i controlli (forma del codice, nessun ripetuto); altrimenti segnala l'errore."""
        if not QID.fullmatch(qid):
            errors.append(f"{where}: codice Wikidata non valido: {qid!r}")
        elif qid in seen:
            errors.append(f"{where}: codice {qid} ripetuto")
        else:
            seen.add(qid)
            cities.append((qid, name or qid))

    header = [h.strip().lower() for h in lines[0].split(",")]
    if "wikidata" in header:  # formato CSV
        for n, row in enumerate(csv.DictReader(lines), start=2):  # riga 1 = intestazione
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            name = row.get("city", "")
            if "check" in row and row["check"] not in LOADABLE:
                skipped.append(f"{name or row['wikidata']} (check = {row['check']})")
                continue
            add(row["wikidata"], name, f"riga {n}")
    else:  # testo semplice
        for n, line in enumerate(lines, start=1):
            parts = line.replace(",", " ").split(None, 1)
            add(parts[0], parts[1].strip() if len(parts) > 1 else "", f"riga {n}")
    return cities, skipped, errors


# --------------------------------------------------------------------------- area di ricerca di una città

def area_from_row(row):
    """Area di ricerca indicata in una riga di wikidata_cities, oppure None se non c'è nessuna eccezione.

    area_method vuoto  -> None: si usa il confine che porta il codice Wikidata della città (comportamento di base).
    'confine'          -> {"method": "confine", "relation_id": n}: quel confine, indicato a mano.
    'raggio'           -> {"method": "raggio", "radius_km": r, "lat": .., "lon": ..}: cerchio attorno al centro.
    Se il metodo c'è ma manca il suo dato solleva ValueError: non si ripiega in silenzio sul confine per codice.
    """
    method = row.get("area_method")
    if not method:
        return None
    if method == "confine":
        rel = row.get("relation_id")
        if not rel or int(rel) <= 0:
            raise ValueError("area_method = confine ma relation_id manca")
        return {"method": "confine", "relation_id": int(rel)}
    if method == "raggio":
        km, lat, lon = row.get("radius_km"), row.get("lat"), row.get("lon")
        if not km or float(km) <= 0:
            raise ValueError("area_method = raggio ma radius_km manca")
        if lat is None or lon is None:
            raise ValueError("area_method = raggio ma lat/lon della città mancano")
        return {"method": "raggio", "radius_km": float(km), "lat": float(lat), "lon": float(lon)}
    raise ValueError(f"area_method sconosciuto: {method}")


def area_label(area):
    """Descrizione breve dell'area, per il log e per la cache: 'qid', 'relation:123' o 'raggio:10km'."""
    if area is None:
        return "qid"
    if area["method"] == "confine":
        return f"relation:{area['relation_id']}"
    km = area["radius_km"]
    return f"raggio:{km:g}km"


# --------------------------------------------------------------------------- Overpass

def build_query(qid, area=None):
    """La query definitiva (vault: «Overpass - come costruire una query»).

    Il corpo è uguale per tutte le città: sei rami che cercano oggetti con nome, uniti per togliere i
    doppioni, e "out center tags" che restituisce i tag e, per way e relation, un punto centrale.
    Cambia solo da dove si prende l'area (vedi area_from_row):
      area None       rel[...] trova il confine amministrativo che porta il QID; map_to_area lo trasforma
                      in un'area salvata nell'insieme .a (comportamento di base, per quasi tutte le città)
      confine         l'area si ottiene direttamente dall'id della relation scelta a mano
      raggio          nessuna area: ogni ramo cerca entro R metri dal centro della città
    """
    if area is None:
        define = f'rel["wikidata"="{qid}"]["boundary"="administrative"];\nmap_to_area->.a;\n'
        where = "(area.a)"
    elif area["method"] == "confine":
        define = f"area(id:{AREA_OFFSET + area['relation_id']})->.a;\n"
        where = "(area.a)"
    else:
        define = ""
        where = f"(around:{round(area['radius_km'] * 1000)},{area['lat']},{area['lon']})"
    return f"""
[out:json][timeout:300];
{define}(
  nwr["name"]["wikidata"]{where};
  nwr["name"]["tourism"]{where};
  nwr["name"]["historic"]{where};
  nwr["name"]["amenity"="place_of_worship"]{where};
  nwr["name"]["leisure"~"^(park|garden|nature_reserve|stadium)$"]{where};
  nwr["name"]["man_made"~"^(tower|obelisk|bridge)$"]{where};
);
out center tags;
"""


def overpass_query(query):
    """Invia una query a Overpass e restituisce la risposta JSON. Ritenta se il server è occupato.

    Usata sia dallo scarico delle città sia da fix_city_areas.py. Solleva RuntimeError se i tentativi finiscono.
    """
    last_error = ""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            r = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=TIMEOUT)
        except requests.exceptions.SSLError as e:  # certificato non fidato: inutile riprovare
            raise RuntimeError("errore SSL (rete aziendale?): installa truststore, vedi requirements.txt") from e
        except requests.exceptions.RequestException as e:  # rete caduta, timeout di lettura
            last_error = f"rete: {e}"
        else:
            if r.status_code == 200:
                data = r.json()
                # Con server sovraccarico Overpass risponde 200 ma con un "remark" di errore e risultati parziali:
                # accettarli darebbe una città incompleta senza che nessuno se ne accorga.
                remark = data.get("remark", "")
                if "runtime error" in remark or "timed out" in remark:
                    last_error = f"Overpass: {remark}"
                else:
                    return data
            elif r.status_code in RETRY_STATUS:
                last_error = f"HTTP {r.status_code}"
            else:
                r.raise_for_status()  # altri errori (400: query sbagliata) non si risolvono riprovando
        if attempt < MAX_TRIES:
            wait = 30 * attempt  # 30, 60, 90, 120 secondi: Overpass è un servizio pubblico condiviso
            print(f"  tentativo {attempt}/{MAX_TRIES} fallito ({last_error}); riprovo tra {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{MAX_TRIES} tentativi falliti ({last_error})")


def fetch_city(qid, cache_dir, refresh, area=None):
    """Restituisce (risposta JSON di Overpass, True se letta da file). Scarica solo se serve.

    Il file salvato porta con sé l'area usata ("_area"): se nel frattempo l'area della città è cambiata
    (per esempio dopo fix_city_areas.py) il file non vale più e si riscarica. Le risposte vuote non si
    salvano: una città senza oggetti si ritenta a ogni corsa, così un vuoto dovuto al server non resta.
    """
    cache = cache_dir / f"{qid}.json"
    label = area_label(area)
    if cache.exists() and not refresh:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("_area", "qid") == label:  # i file vecchi senza "_area" sono del confine per codice
                return saved, True
            print(f"  {cache.name} è di un'altra area ({saved.get('_area', 'qid')}), riscarico")
        except json.JSONDecodeError:  # file troncato (corsa interrotta): si riscarica
            print(f"  {cache.name} illeggibile, riscarico")

    data = overpass_query(build_query(qid, area))
    if data.get("elements"):
        data["_area"] = label
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")  # scrittura in un file temporaneo, poi rinomina: niente file a metà
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache)
    return data, False


# --------------------------------------------------------------------------- da Overpass ai record

def to_records(elements, city_qid):
    """Trasforma gli elementi di Overpass in record per osm_objects. Restituisce (record, statistiche)."""
    records, seen = [], set()
    stats = {"elementi": len(elements), "senza_nome": 0, "doppioni": 0, "qid_scartati": 0}
    for e in elements:
        tags = e.get("tags", {})
        name = (tags.get("name") or "").replace("\x00", "").strip()
        if not name:  # la query chiede già il nome, ma la tabella lo esige: controllo di sicurezza
            stats["senza_nome"] += 1
            continue
        key = (e["type"], e["id"])
        if key in seen:  # lo stesso oggetto due volte nello stesso invio farebbe fallire l'upsert
            stats["doppioni"] += 1
            continue
        seen.add(key)

        qid = (tags.get("wikidata") or "").strip()
        if qid and not QID.fullmatch(qid):
            # In OSM il tag wikidata a volte è sbagliato o contiene più codici ("Q1;Q2"). Il vincolo della
            # tabella rifiuterebbe l'intero blocco: si mette NULL e si conta.
            stats["qid_scartati"] += 1
            qid = ""

        center = e.get("center", {})  # node: lat/lon diretti; way e relation: punto centrale in "center"
        records.append({
            "osm_type": e["type"],
            "osm_id": e["id"],
            "city_wikidata": city_qid,
            "lat": e.get("lat", center.get("lat")),
            "lon": e.get("lon", center.get("lon")),
            "name": name,
            "wikidata": qid or None,
            # senza le traduzioni name:xx (centinaia di chiavi inutili); \x00 non è ammesso in jsonb
            "tags": {k: v.replace("\x00", "") for k, v in tags.items() if not k.startswith("name:")},
        })
    return records, stats


# --------------------------------------------------------------------------- Supabase

def load_env(path):
    """Legge un file .env semplice (CHIAVE=valore) e riempie os.environ senza sovrascrivere."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def headers(key):
    """Intestazioni per l'API REST di Supabase. La chiave non viene mai stampata."""
    h = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # chiave vecchio stile (JWT): va anche in Authorization
        h["Authorization"] = f"Bearer {key}"
    return h


def known_cities(url, key):
    """Codici presenti in wikidata_cities: osm_objects può contenere solo città già registrate."""
    r = requests.get(f"{url}/rest/v1/{CITIES_TABLE}?select=wikidata", headers=headers(key), timeout=60)
    r.raise_for_status()
    return {row["wikidata"] for row in r.json()}


def cities_from_db(url, key):
    """Lista di città letta da Supabase (wikidata_cities): è la fonte in produzione.

    Prende solo le righe con check_status ok o override, come fa read_cities con il CSV.
    Restituisce ([(QID, nome)], [messaggi di scarto]).
    """
    r = requests.get(f"{url}/rest/v1/{CITIES_TABLE}?select=wikidata,name,check_status&order=name",
                     headers=headers(key), timeout=60)
    r.raise_for_status()
    cities, skipped = [], []
    for row in r.json():
        if row["check_status"] in LOADABLE:
            cities.append((row["wikidata"], row["name"]))
        else:
            skipped.append(f"{row['name']} (check = {row['check_status']})")
    return cities, skipped


def areas_from_db(url, key):
    """Area di ricerca di ogni città che ha un'eccezione in wikidata_cities: {QID: area, oppure ValueError}.

    Le città con area_method vuoto non compaiono (usano il confine per codice). Una riga incoerente non
    ferma le altre: al suo posto c'è l'errore, e main lo segnala solo per quella città.
    """
    r = requests.get(f"{url}/rest/v1/{CITIES_TABLE}?select=wikidata,area_method,relation_id,radius_km,lat,lon"
                     f"&area_method=not.is.null", headers=headers(key), timeout=60)
    r.raise_for_status()
    areas = {}
    for row in r.json():
        try:
            areas[row["wikidata"]] = area_from_row(row)
        except ValueError as e:
            areas[row["wikidata"]] = e
    return areas


_log_avvisato = False  # l'avviso "log non scritto" si stampa una volta sola per corsa


def log_run(url, key, qid, status, n_objects=0, n_with_qid=0, area=None, source=None, error=None):
    """Scrive una riga in osm_ingestion_log. Un problema col log non deve mai fermare lo scarico."""
    global _log_avvisato
    row = {"city_wikidata": qid, "status": status, "n_objects": n_objects, "n_with_qid": n_with_qid,
           "area_used": area_label(area), "source": source, "error_message": (error or None) and error[:500]}
    try:
        r = requests.post(f"{url}/rest/v1/{LOG_TABLE}", headers={**headers(key), "Prefer": "return=minimal"},
                          json=row, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001 - tabella assente, rete, chiave: si avvisa e si prosegue
        if not _log_avvisato:
            hint = " (applica prima sql/003_osm_ingestion_log.sql)" if "404" in str(e) else ""
            print(f"  ATTENZIONE: log della corsa non scritto in {LOG_TABLE}{hint}: {e}", file=sys.stderr)
            _log_avvisato = True


def upsert(url, key, records):
    """Scrive le righe a blocchi: inserisce se la chiave manca, aggiorna se esiste."""
    endpoint = f"{url}/rest/v1/{TABLE}?on_conflict=osm_type,osm_id,city_wikidata"
    h = {**headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        r = requests.post(endpoint, headers=h, json=chunk, timeout=120)
        if r.status_code not in (200, 201, 204):
            hint = " (la tabella non esiste: applica prima sql/002_osm_objects.sql)" if r.status_code == 404 else ""
            raise RuntimeError(f"scrittura fallita, HTTP {r.status_code}{hint}: {r.text[:400]}")


def count_city(url, key, qid):
    """Righe presenti in osm_objects per una città, lette dal database (controllo finale)."""
    h = {**headers(key), "Prefer": "count=exact", "Range": "0-0"}  # una riga sola: ci serve solo l'intestazione
    r = requests.get(f"{url}/rest/v1/{TABLE}?select=osm_id&city_wikidata=eq.{qid}", headers=h, timeout=60)
    r.raise_for_status()
    return int(r.headers["Content-Range"].split("/")[1])  # es. "0-0/8792"


# --------------------------------------------------------------------------- mail di esito

class Tee:
    """Scrive tutto quello che il programma stampa sia sul terminale sia in una lista in memoria.

    Serve per mandare per mail lo stesso testo che vedi a schermo senza riscrivere ogni print.
    """

    def __init__(self, stream, buffer):
        """stream = terminale vero (stdout o stderr); buffer = lista in cui si accumula il testo per la mail."""
        self.stream, self.buffer = stream, buffer

    def write(self, text):
        """Copia il testo nel buffer e lo passa al terminale."""
        self.buffer.append(text)
        return self.stream.write(text)

    def flush(self):
        """Svuota il flusso vero (richiesto da chi scrive su stdout)."""
        self.stream.flush()

    def __getattr__(self, name):  # tutto il resto (encoding, isatty...) lo gestisce il flusso vero
        return getattr(self.stream, name)


def send_mail(subject, body):
    """Invia una mail con Gmail (SMTP su SSL). Un problema qui non deve mai cambiare l'esito della corsa."""
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_APP_PASSWORD", "")
    if not user or not password:
        print("Mail NON inviata: mancano SMTP_USER o SMTP_APP_PASSWORD nel file .env (vedi .env.example).", file=sys.stderr)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.environ.get("NOTIFY_TO") or user
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), timeout=30) as smtp:
            smtp.login(user, password)  # la password non viene mai stampata
            smtp.send_message(msg)
        print(f"Mail di esito inviata a {msg['To']}.")
    except Exception as e:  # noqa: BLE001 - rete bloccata, password sbagliata, ecc.
        print(f"Mail NON inviata ({type(e).__name__}: {e}).", file=sys.stderr)


def run_with_report():
    """Esegue main(); con --notify, a fine corsa manda per mail come è stato lanciato e com'è andata."""
    notify = "--notify" in sys.argv
    started = datetime.now()
    buffer = []
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(real_out, buffer), Tee(real_err, buffer)  # da qui in poi si registra tutto
    code, status = 0, "SUCCESSO"
    try:
        main()
    except SystemExit as e:  # sys.exit(...) dentro main: con un testo è un errore, con un numero vale quel codice
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            code = 1
        else:
            code = e.code or 0
    except BaseException:  # noqa: BLE001 - errore imprevisto (o Ctrl+C): si registra e si segnala
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    if code:
        status = "ERRORE"

    if notify:
        ended = datetime.now()
        text = "".join(buffer)
        riepilogo = next((l for l in text.splitlines() if l.startswith("Riepilogo:")), "")
        subject = f"[WikipediaAttractionsDatabase] load_attractions: {status}" + (f" - {riepilogo[len('Riepilogo: '):]}" if riepilogo else "")
        last_lines = "\n".join(text.splitlines()[-300:])  # le ultime 300 righe bastano; la mail resta leggera
        body = (
            f"Esito: {status} (codice di uscita {code})\n"
            f"Comando: {Path(sys.argv[0]).name} {' '.join(sys.argv[1:])}\n"
            f"Computer: {socket.gethostname()}\n"
            f"Inizio: {started:%Y-%m-%d %H:%M:%S}   Fine: {ended:%H:%M:%S}   Durata: {str(ended - started).split('.')[0]}\n"
            f"\n---- output ----\n{last_lines}\n"
        )
        send_mail(subject, body)
    sys.exit(code)


# --------------------------------------------------------------------------- programma principale

def main():
    """Programma principale: opzioni, lista città (file o Supabase), scarico e scrittura città per città, riepilogo."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities", default=DEFAULT_CITIES, help="lista di città (CSV o un QID per riga); se il file di default manca si leggono da Supabase")
    ap.add_argument("--only", help="codici separati da virgola (es. Q220,Q90): ignora la lista")
    ap.add_argument("--apply", action="store_true", help="scrive su Supabase (senza, solo scarica e controlla)")
    ap.add_argument("--refresh", action="store_true", help="riscarica anche se la risposta è già in data/overpass")
    ap.add_argument("--pause", type=float, default=5, help="secondi di pausa tra una città e l'altra (default 5)")
    ap.add_argument("--notify", action="store_true", help="a fine corsa manda per mail l'esito (serve SMTP_* nel .env)")
    ap.add_argument("--cache-dir", default=str(BASE / "data" / "overpass"), help="dove salvare le risposte grezze")
    args = ap.parse_args()
    load_env(BASE / ".env")  # sempre: servono sia a --apply sia a --notify

    # 1) credenziali: servono a --apply (scrittura) e, se manca il file della lista, per leggere le città dal database
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    have_db = bool(url and key)

    # 2) lista delle città: --only, poi il file, poi (se il file di default non c'è) Supabase
    if args.only:
        cities = [(q.strip(), q.strip()) for q in args.only.split(",") if q.strip()]
        bad = [q for q, _ in cities if not QID.fullmatch(q)]
        if bad:
            sys.exit(f"Codici non validi in --only: {bad}")
    else:
        path = Path(args.cities)
        if path.exists():
            cities, skipped, errors = read_cities(path)
        elif args.cities != DEFAULT_CITIES:  # hai indicato tu un file e non esiste: è un errore, non si cambia fonte
            sys.exit(f"File non trovato: {path}")
        elif have_db:  # produzione: nessun CSV locale, la lista è la tabella delle città
            print(f"File {path.name} non trovato: leggo le città da {CITIES_TABLE} su Supabase.")
            try:
                cities, skipped = cities_from_db(url, key)
            except Exception as e:  # noqa: BLE001 - rete, chiave sbagliata, tabella assente
                sys.exit(f"Impossibile leggere {CITIES_TABLE} da Supabase ({type(e).__name__}: {e}).")
            errors = []
            if not cities:
                sys.exit(f"{CITIES_TABLE} non contiene città con check ok o override: lancia prima load_cities.py.")
        else:
            sys.exit(f"File non trovato: {path}, e mancano SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY per leggere "
                     f"le città dal database. Lancia build_cities.py, passa --cities o compila il file .env.")
        for s_ in skipped:
            print(f"NON in lista: {s_}")
        for e in errors:
            print(f"ERRORE: {e}", file=sys.stderr)
        if errors:
            sys.exit("Correggi gli errori sopra prima di procedere.")
    print(f"Città da elaborare: {len(cities)}   modalità: {'SCRITTURA su Supabase' if args.apply else 'dry-run (nessuna scrittura)'}\n")

    # 3) controllo preventivo del database (solo con --apply)
    if args.apply:
        if not have_db:
            sys.exit("Mancano SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY: mettile nel file .env (vedi .env.example).")
        registered = known_cities(url, key)
        print(f"Database: {url.split('//')[-1]}, città registrate in {CITIES_TABLE}: {len(registered)}\n")  # mai la chiave

    # 4) città con un'area indicata a mano (wikidata_cities.area_method): le altre usano il confine per codice
    areas = {}
    if have_db:
        try:
            areas = areas_from_db(url, key)
        except Exception as e:  # noqa: BLE001 - rete, chiave, tabella
            if args.apply:  # scrivere con l'area sbagliata è peggio che non scrivere
                sys.exit(f"Impossibile leggere le aree da {CITIES_TABLE} ({type(e).__name__}: {e}).")
            print(f"ATTENZIONE: aree per città non lette ({type(e).__name__}: {e}): uso il confine per codice.\n")
    custom = [f"{q} ({area_label(a)})" if not isinstance(a, Exception) else f"{q} (NON VALIDA)" for q, a in areas.items()]
    if custom:
        print(f"Città con area indicata a mano: {', '.join(custom)}\n")

    # 5) una città alla volta: un errore su una non ferma le altre
    cache_dir = Path(args.cache_dir)
    totals = {"righe": 0, "con_qid": 0}
    failures, empty = [], []
    for n, (qid, name) in enumerate(cities, start=1):
        label = f"[{n}/{len(cities)}] {qid} {name}" if name != qid else f"[{n}/{len(cities)}] {qid}"
        if args.apply and qid not in registered:
            print(f"{label}: SALTATA, non è in {CITIES_TABLE} (lancia load_cities.py)")
            failures.append((qid, "città non registrata"))
            continue
        area = areas.get(qid)
        if isinstance(area, Exception):  # riga di wikidata_cities incoerente: si segnala e si passa oltre
            print(f"{label}: ERRORE: area non valida in {CITIES_TABLE}: {area}", file=sys.stderr)
            failures.append((qid, f"area non valida: {area}"))
            if args.apply:
                log_run(url, key, qid, "errore", error=f"area non valida: {area}")
            continue
        try:
            data, cached = fetch_city(qid, cache_dir, args.refresh, area)
            source = "cache" if cached else "overpass"
            records, st = to_records(data.get("elements", []), qid)
            if not records:  # città senza confine amministrativo in OSM, oppure area indicata a mano senza oggetti
                print(f"{label}: nessun oggetto ({'confine non trovato?' if area is None else 'area ' + area_label(area)})")
                empty.append(qid)
                if args.apply:
                    log_run(url, key, qid, "nessun_oggetto", area=area, source=source)
            else:
                with_qid = sum(r["wikidata"] is not None for r in records)
                print(f"{label}: {len(records)} oggetti, {with_qid} con QID"
                      f"{'' if cached else ' (scaricata ora)'}"
                      f"{', %d QID malformati azzerati' % st['qid_scartati'] if st['qid_scartati'] else ''}"
                      f"{', %d doppioni' % st['doppioni'] if st['doppioni'] else ''}")
                totals["righe"] += len(records)
                totals["con_qid"] += with_qid
                if args.apply:
                    upsert(url, key, records)
                    in_db = count_city(url, key, qid)
                    ok = "ok" if in_db >= len(records) else "ATTENZIONE: meno righe del previsto"
                    print(f"      scritte, in tabella per questa città: {in_db} ({ok})")
                    log_run(url, key, qid, "ok", len(records), with_qid, area, source)
        except Exception as e:  # noqa: BLE001 - si registra l'errore e si passa alla città dopo
            print(f"{label}: ERRORE: {e}", file=sys.stderr)
            failures.append((qid, str(e)))
            if args.apply:
                log_run(url, key, qid, "errore", area=area, error=str(e))
            cached = True  # niente pausa lunga dopo un errore già gestito
        if n < len(cities) and not cached:
            time.sleep(args.pause)  # pausa solo dopo una chiamata vera: Overpass è un server pubblico condiviso

    # 6) riepilogo
    print(f"\nRiepilogo: {totals['righe']} oggetti ({totals['con_qid']} con QID) da {len(cities) - len(failures) - len(empty)} città")
    if empty:
        print(f"Senza oggetti: {', '.join(empty)}")
        print("  Per indicare l'area giusta di queste città: python fix_city_areas.py")
    if failures:
        print("Fallite:", file=sys.stderr)
        for qid, msg in failures:
            print(f"  {qid}: {msg}", file=sys.stderr)
        sys.exit(1)  # codice di errore: utile se un giorno lo lanci da uno scheduler
    if not args.apply:
        print("Nessuna scrittura. Per scrivere davvero: python load_attractions.py --apply")


if __name__ == "__main__":
    run_with_report()
