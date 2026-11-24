#!/usr/bin/env python3
"""Aggiorna il punteggio delle attrazioni (QRank) e le due viste materializzate del livello gold.

Che cosa fa, in ordine (vedi sql/004_qrank_gold.sql):
  1. con --clean: refresh di osm_objects_clean (rumore tolto, una riga per città e QID). Va fatto dopo
     aver caricato o ricaricato delle città con load_attractions.py;
  2. legge i QID distinti di osm_objects_clean (una query);
  3. legge la versione corrente di QRank da datapackage.json; se è la stessa già caricata e non serve
     ricaricare, si ferma qui;
  4. DuckDB legge il CSV di QRank (circa 126 MB compressi, in streaming) e tiene solo i nostri QID: il join
     si fa sul tuo PC, nessuna chiamata per singolo QID;
  5. sostituisce il contenuto di wikidata_qrank in una transazione (cancella e ricarica con COPY);
  6. refresh di osm_objects_scored (gold + qrank), la vista letta dall'Itinerario AI.
I refresh sono CONCURRENTLY: chi legge mentre lo script gira vede la versione precedente, intera.

Uso (dalla radice del repo):
    python refresh_scores.py                    # dry-run: dice che cosa farebbe e quanti QID trova in QRank; non scrive
    python refresh_scores.py --apply            # aggiornamento periodico: ricarica QRank solo se c'è una versione nuova
    python refresh_scores.py --apply --clean    # dopo aver caricato delle città: refresh del gold e ricarica di QRank
    python refresh_scores.py --apply --force    # ricarica QRank anche se la versione è la stessa
    python refresh_scores.py --apply --via-file # scarica il CSV in un file temporaneo (reti aziendali, vedi sotto)
    python refresh_scores.py --apply --notify   # a fine corsa manda per mail l'esito (come load_attractions.py)

Serve una variabile nel file .env (mai in git) o nell'ambiente, anche per il dry-run:
    SUPABASE_DB_URL   stringa di connessione Postgres del Session pooler, con la password del database.
                      Supabase > pulsante "Connect" > scheda "Direct" > Session pooler. La connessione diretta
                      (db.xxx.supabase.co) è solo IPv6 e da molte reti non si raggiunge.
La chiave REST (SUPABASE_SERVICE_ROLE_KEY) qui non basta: REFRESH MATERIALIZED VIEW e COPY non passano dalle API.

--via-file: di norma DuckDB legge il CSV direttamente dall'URL. Se la rete aziendale ispeziona l'HTTPS e DuckDB
rifiuta il certificato, con --via-file il file si scarica con requests (che usa i certificati di Windows tramite
truststore), si verifica lo SHA-256 dichiarato da QRank, si legge e si cancella a fine corsa.

Rilanciabile senza danni: ogni passo sostituisce il risultato precedente per intero.
"""

import argparse  # opzioni da riga di comando
import hashlib  # verifica dello SHA-256 del file scaricato (--via-file)
import os  # variabili d'ambiente
import sys  # uscita con codice di errore e messaggi su stderr
import tempfile  # file temporaneo per --via-file
import time  # durata dei passi
import traceback  # testo dell'errore imprevisto, per la mail
import socket  # nome del computer, per la mail
from datetime import date, datetime  # versione di QRank e orari della corsa
from pathlib import Path  # percorsi indipendenti dal sistema operativo

import requests  # datapackage.json e, con --via-file, il CSV

try:
    import duckdb  # join tra il CSV di QRank e i nostri QID, in memoria
    import psycopg  # connessione Postgres a Supabase (refresh e COPY)
except ImportError as e:
    sys.exit(f"Manca una libreria ({e.name}): pip install -r requirements.txt")

# Riuso da load_attractions.py: lettura del .env, mail di esito, copia dell'output per la mail.
# Importarlo attiva anche truststore (certificati di Windows) per requests.
from load_attractions import Tee, load_env, send_mail
import config  # indirizzo di QRank e User-Agent: config.toml

BASE = Path(__file__).parent  # cartella dello script (radice del repo)
QRANK_BASE = config.QRANK_BASE  # config.toml, [servizi] qrank_base: dove QRank pubblica datapackage.json e i file datati
HEADERS = {"User-Agent": config.user_agent()}  # Wikimedia chiede di dichiararsi; contatto in config.toml
STATEMENT_TIMEOUT = "30min"  # il refresh del gold scorre tutte le città: il limite di default sarebbe troppo stretto

CLEAN = "public.osm_objects_clean"
SCORED = "public.osm_objects_scored"
QRANK_TABLE = "public.wikidata_qrank"


# --------------------------------------------------------------------------- QRank

def qrank_manifest(base):
    """Legge datapackage.json di QRank. Restituisce (versione, URL del CSV datato, byte, sha256 senza prefisso).

    Si usa il file DATATO indicato nel manifest (es. qrank-20261108.csv.gz), non l'URL senza data: così la
    versione salvata nel database corrisponde sempre ai numeri caricati, anche se QRank pubblica a metà corsa.
    """
    r = requests.get(base + "datapackage.json", headers=HEADERS, timeout=60)
    r.raise_for_status()
    pkg = r.json()
    res = next((x for x in pkg.get("resources", []) if x.get("name") == "qrank"), None)
    if res is None:
        raise RuntimeError("datapackage.json non contiene la risorsa 'qrank': il formato di QRank è cambiato?")
    version = date.fromisoformat(pkg["version"])
    sha = (res.get("hash") or "").removeprefix("sha256:")
    return version, base + res["path"], res.get("bytes"), sha


def download(url, expected_bytes, expected_sha):
    """Scarica il CSV in un file temporaneo e ne verifica lo SHA-256. Restituisce il percorso (da cancellare)."""
    fd, path = tempfile.mkstemp(prefix="qrank-", suffix=".csv.gz")
    h, size = hashlib.sha256(), 0
    with os.fdopen(fd, "wb") as f, requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)
    if expected_sha and h.hexdigest() != expected_sha:
        os.remove(path)
        raise RuntimeError(f"SHA-256 diverso da quello dichiarato da QRank ({size} byte scaricati, attesi {expected_bytes}).")
    return path


def join_qrank(source, qids):
    """Legge il CSV di QRank (URL o file) e restituisce [(QID, qrank)] solo per i QID passati.

    Il join lo fa DuckDB in memoria: la lista dei nostri QID è la tabella piccola, il CSV scorre una volta sola.
    """
    con = duckdb.connect()  # database in memoria: non resta niente su disco
    if source.startswith("http"):
        con.execute("install httpfs; load httpfs;")  # lettura via HTTP (scaricata la prima volta, poi in cache)
    con.execute("create temp table nostri as select unnest(?::varchar[]) as wikidata", [qids])
    rows = con.execute(
        """
        select q.Entity, q.QRank
        from read_csv(?, header = true, columns = {'Entity': 'VARCHAR', 'QRank': 'BIGINT'}) q
        join nostri n on n.wikidata = q.Entity
        """,
        [source],
    ).fetchall()
    con.close()
    return rows


# --------------------------------------------------------------------------- Supabase

def connection_hint(message):
    """Dato il testo di un errore di connessione di psycopg, restituisce un consiglio in italiano su cosa controllare.

    Restituisce "" se l'errore non è tra quelli noti: in quel caso si mostra solo il messaggio originale.
    Casi incontrati finora (vedi la chat del 13 novembre):
      - "password authentication failed"  -> password sbagliata, parentesi del segnaposto rimaste, simboli non codificati
      - "Tenant or user not found"        -> utente senza ".<ref-progetto>" oppure host del pooler di un altro cluster
      - "timeout" / "could not translate host name" -> rete: connessione diretta solo IPv6, VPN, Wi-Fi aziendale
    """
    # TODO(Marco): mappa i casi sopra (ed eventuali altri che ritieni utili) su un consiglio di una riga.
    return ""


def connect(db_url):
    """Connessione in autocommit (ogni comando è subito definitivo, salvo dentro conn.transaction())."""
    try:
        conn = psycopg.connect(db_url, autocommit=True, application_name="mindtrip-refresh-scores", connect_timeout=15)
    except psycopg.OperationalError as e:
        first = str(e).splitlines()[0]  # psycopg ripete l'errore per ogni indirizzo IP del pooler: basta la prima riga
        hint = connection_hint(str(e))
        sys.exit(f"Connessione a Supabase non riuscita: {first}" + (f"\n{hint}" if hint else ""))
    conn.execute(f"set statement_timeout = '{STATEMENT_TIMEOUT}'")
    return conn


def check_schema(conn):
    """Verifica che la migrazione 004 sia applicata: le due viste devono essere materializzate."""
    found = {r[0] for r in conn.execute(
        "select matviewname from pg_matviews where schemaname = 'public' and matviewname in ('osm_objects_clean','osm_objects_scored')"
    )}
    has_table = conn.execute("select to_regclass(%s) is not null", (QRANK_TABLE,)).fetchone()[0]
    if found != {"osm_objects_clean", "osm_objects_scored"} or not has_table:
        sys.exit("Lo schema non è pronto: applica prima sql/004_qrank_gold.sql (servono wikidata_qrank e le due viste materializzate).")


def refresh(conn, view):
    """REFRESH CONCURRENTLY: ricalcola a parte e sostituisce in un colpo solo, senza bloccare chi legge."""
    t = time.monotonic()
    conn.execute(f"refresh materialized view concurrently {view}")
    print(f"  refresh {view}: {time.monotonic() - t:.1f} s")


def replace_qrank(conn, rows, version):
    """Sostituisce tutte le righe di wikidata_qrank in una transazione: o tutto il nuovo o niente."""
    with conn.transaction():
        conn.execute(f"delete from {QRANK_TABLE}")
        with conn.cursor().copy(f"copy {QRANK_TABLE} (wikidata, qrank, source_version) from stdin") as cp:
            for qid, qrank in rows:
                cp.write_row((qid, qrank, version))


# --------------------------------------------------------------------------- programma principale

def main():
    ap = argparse.ArgumentParser(description="Aggiorna QRank e le viste materializzate del gold delle attrazioni.")
    ap.add_argument("--apply", action="store_true", help="scrive davvero su Supabase (senza: dry-run)")
    ap.add_argument("--clean", action="store_true", help="refresh anche di osm_objects_clean (dopo il caricamento di città)")
    ap.add_argument("--force", action="store_true", help="ricarica QRank anche se la versione è già quella caricata")
    ap.add_argument("--via-file", action="store_true", help="scarica il CSV in un file temporaneo invece di leggerlo dall'URL")
    ap.add_argument("--notify", action="store_true", help="a fine corsa manda per mail l'esito (serve SMTP_* nel .env)")
    ap.add_argument("--qrank-base", default=QRANK_BASE, help=argparse.SUPPRESS)  # solo per le prove
    args = ap.parse_args()

    load_env(BASE / ".env")
    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        sys.exit("Manca SUPABASE_DB_URL: mettila nel file .env (vedi .env.example e l'intestazione di questo script).")

    mode = "APPLY" if args.apply else "DRY-RUN (nessuna scrittura)"
    print(f"refresh_scores.py - {mode}")
    with connect(db_url) as conn:
        check_schema(conn)

        # 1) gold
        if args.clean:
            if args.apply:
                refresh(conn, CLEAN)
            else:
                print(f"  farei: refresh {CLEAN}")

        # 2) i nostri QID
        qids = [r[0] for r in conn.execute(f"select distinct wikidata from {CLEAN}")]
        print(f"  QID in {CLEAN}: {len(qids):,}")
        if not qids:
            sys.exit(f"{CLEAN} è vuota: carica delle città con load_attractions.py e rilancia con --clean.")

        # 3) versione di QRank
        stored_version, stored_rows = conn.execute(f"select max(source_version), count(*) from {QRANK_TABLE}").fetchone()
        version, csv_url, csv_bytes, csv_sha = qrank_manifest(args.qrank_base)
        print(f"  QRank pubblicato: {version}   caricato nel database: {stored_version or 'nessuno'} ({stored_rows:,} righe)")

        reasons = [why for cond, why in [
            (args.force, "--force"),
            (args.clean, "--clean: i QID possono essere cambiati"),
            (stored_rows == 0, "tabella vuota"),
            (stored_version != version, "versione nuova"),
        ] if cond]
        if not reasons:
            print("Riepilogo: niente da fare, QRank è già aggiornato.")
            return
        print(f"  ricarico QRank: {', '.join(reasons)}")

        # 4) join con QRank
        t = time.monotonic()
        tmp = download(csv_url, csv_bytes, csv_sha) if args.via_file else None
        try:
            rows = join_qrank(tmp or csv_url, qids)
        except duckdb.Error as e:
            hint = "" if tmp else "\nSe è un problema di certificati o di rete, riprova con --via-file."
            sys.exit(f"DuckDB non è riuscito a leggere QRank: {e}{hint}")
        finally:
            if tmp:
                os.remove(tmp)
        print(f"  join con {csv_url.rsplit('/', 1)[-1]}: {len(rows):,} QID su {len(qids):,} hanno visite ({time.monotonic() - t:.1f} s)")

        if not args.apply:
            print(f"Riepilogo: dry-run, QRank {version}: {len(rows):,} punteggi pronti. Per scrivere: python refresh_scores.py --apply"
                  + (" --clean" if args.clean else ""))
            return

        # 5) e 6) scrittura e refresh del gold con punteggio
        replace_qrank(conn, rows, version)
        print(f"  {QRANK_TABLE}: {len(rows):,} righe, versione {version}")
        refresh(conn, SCORED)

        total, scored = conn.execute(f"select count(*), count(qrank) from {SCORED}").fetchone()
        print(f"Riepilogo: QRank {version}, {scored:,} luoghi con punteggio su {total:,} in {SCORED}.")


def run_with_report():
    """Esegue main(); con --notify, a fine corsa manda per mail come è stato lanciato e com'è andata."""
    notify = "--notify" in sys.argv
    started = datetime.now()
    buffer = []
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(real_out, buffer), Tee(real_err, buffer)
    code = 0
    try:
        main()
    except SystemExit as e:  # sys.exit("testo") = errore; sys.exit(n) = quel codice
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

    if notify:
        ended = datetime.now()
        text = "".join(buffer)
        status = "ERRORE" if code else "SUCCESSO"
        riepilogo = next((l for l in text.splitlines() if l.startswith("Riepilogo:")), "")
        subject = f"[WikipediaAttractionsDatabase] refresh_scores: {status}" + (f" - {riepilogo[len('Riepilogo: '):]}" if riepilogo else "")
        body = (
            f"Esito: {status} (codice di uscita {code})\n"
            f"Comando: {Path(sys.argv[0]).name} {' '.join(sys.argv[1:])}\n"
            f"Computer: {socket.gethostname()}\n"
            f"Inizio: {started:%Y-%m-%d %H:%M:%S}   Fine: {ended:%H:%M:%S}   Durata: {str(ended - started).split('.')[0]}\n"
            f"\n---- output ----\n{text}\n"
        )
        send_mail(subject, body)
    sys.exit(code)


if __name__ == "__main__":
    run_with_report()
