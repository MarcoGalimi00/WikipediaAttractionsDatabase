#!/usr/bin/env python3
"""Carica seed/citta_50.csv nella tabella public.wikidata_cities di Supabase (upsert).

Uso (dalla radice del repo):
    python load_cities.py --dry-run     # mostra cosa scriverebbe, senza toccare il database
    python load_cities.py --apply       # scrive davvero (inserisce o aggiorna per codice Wikidata)

Servono due variabili, nel file .env (mai in git) o nell'ambiente:
    SUPABASE_URL                  es. https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY     la chiave con pieni poteri: resta solo sul tuo PC, lato server

Senza --apply lo script non scrive mai: --dry-run è il comportamento di base.
"""

import argparse  # lettura delle opzioni da riga di comando
import csv  # lettura dei file CSV
import os  # variabili d'ambiente
import re  # espressioni regolari per i controlli
import sys  # uscita con codice di errore e messaggi su stderr
from datetime import datetime, timezone  # per riempire updated_at
from pathlib import Path  # percorsi di file indipendenti dal sistema operativo

import requests  # chiamate HTTP (l'API REST di Supabase)

# truststore fa usare a Python l'archivio dei certificati di Windows (o del sistema) invece di quello
# incluso in "certifi". Serve nelle reti aziendali che ispezionano il traffico HTTPS con un
# certificato proprio: quel certificato è già nell'archivio di Windows, ma non in certifi.
# Vale anche per le chiamate a Supabase. È facoltativo: se non è installato (pip install truststore) si usa l'archivio di certifi.
try:
    import truststore

    truststore.inject_into_ssl()  # da qui in poi requests si fida dei certificati di sistema
except ImportError:
    pass

BASE = Path(__file__).parent  # cartella dello script (radice del repo)
TABLE = "wikidata_cities"  # tabella di destinazione
BATCH = 100  # righe per ogni chiamata di scrittura
LOADABLE = {"ok", "override"}  # valori di check che si caricano; gli altri vanno rivisti a mano


def load_env(path):
    """Legge un file .env semplice (RIGA CHIAVE=valore) e riempie os.environ senza sovrascrivere."""
    if not path.exists():  # se il file non c'è, si usano solo le variabili già presenti
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()  # toglie spazi e a capo
        if not line or line.startswith("#") or "=" not in line:  # salta righe vuote, commenti, righe senza "="
            continue
        key, value = line.split("=", 1)  # divide alla prima uguale
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))  # toglie eventuali virgolette


def to_int(text):
    """Testo in numero intero, oppure None se vuoto."""
    return int(float(text)) if text.strip() else None


def to_float(text):
    """Testo in numero decimale, oppure None se vuoto."""
    return float(text) if text.strip() else None


def read_overrides(path):
    """Legge seed/override_qid.csv e restituisce {nome città: nota}."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {r["city"]: r.get("nota", "") for r in csv.DictReader(f)}


def build_records(csv_path, overrides):
    """Trasforma le righe di citta_50.csv in record per la tabella; restituisce (record, scartate, errori)."""
    now = datetime.now(timezone.utc).isoformat()  # stesso orario per tutte le righe di questa corsa
    records, skipped, errors, seen = [], [], [], set()
    with csv_path.open(encoding="utf-8", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):  # riga 1 = intestazione
            name = r["city"]
            if r["check"] not in LOADABLE:  # verifica / nessuna / errore: non si caricano
                skipped.append((name, r["check"]))
                continue
            qid = r["wikidata"].strip()
            if not re.fullmatch(r"Q[0-9]+", qid):  # forma del codice Wikidata
                errors.append(f"riga {n} ({name}): codice Wikidata non valido: {qid!r}")
                continue
            if not re.fullmatch(r"[A-Z]{2}", r["country_code"]):  # codice paese a due lettere
                errors.append(f"riga {n} ({name}): codice paese non valido: {r['country_code']!r}")
                continue
            if qid in seen:  # la chiave unica è il codice Wikidata
                errors.append(f"riga {n} ({name}): codice {qid} già presente")
                continue
            seen.add(qid)
            records.append({
                "wikidata": qid,
                "name": name,
                "country_code": r["country_code"],
                "wikidata_label": r["wikidata_label"] or None,
                "lat": to_float(r["lat"]),
                "lon": to_float(r["lon"]),
                "population": to_int(r["population"]),
                "n_languages": to_int(r["n_languages"]),
                "types": [t.strip() for t in r["types"].split(",") if t.strip()],  # "Q1, Q2" -> ["Q1", "Q2"]
                "check_status": r["check"],  # nel database la colonna si chiama check_status
                "check_auto": r.get("check_auto") or None,
                "override_note": overrides.get(name) if r["check"] == "override" else None,
                "updated_at": now,
            })
    return records, skipped, errors


def headers(key):
    """Intestazioni per l'API REST di Supabase. La chiave non viene mai stampata."""
    h = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # chiave vecchio stile (JWT): va anche in Authorization
        h["Authorization"] = f"Bearer {key}"
    return h


def upsert(url, key, records):
    """Scrive le righe a blocchi: inserisce se il codice Wikidata manca, aggiorna se esiste."""
    endpoint = f"{url}/rest/v1/{TABLE}?on_conflict=wikidata"
    h = {**headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        r = requests.post(endpoint, headers=h, json=chunk, timeout=60)
        if r.status_code not in (200, 201, 204):  # qualunque altro codice è un errore
            raise RuntimeError(f"Scrittura fallita (HTTP {r.status_code}): {r.text[:500]}")
        print(f"  scritte {i + len(chunk)}/{len(records)} righe")


def count_by_status(url, key):
    """Legge dal database il numero di righe per check_status, come controllo finale."""
    r = requests.get(f"{url}/rest/v1/{TABLE}?select=check_status", headers=headers(key), timeout=60)
    r.raise_for_status()
    counts = {}
    for row in r.json():
        counts[row["check_status"]] = counts.get(row["check_status"], 0) + 1
    return counts


def main():
    """Programma principale: legge CSV e override, controlla le righe, mostra l'anteprima o scrive su Supabase (--apply)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(BASE / "seed" / "citta_50.csv"), help="file delle città")
    ap.add_argument("--overrides", default=str(BASE / "seed" / "override_qid.csv"), help="file degli override")
    ap.add_argument("--dry-run", action="store_true", help="non scrive (è già il comportamento di base)")
    ap.add_argument("--apply", action="store_true", help="scrive davvero nel database")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        sys.exit("Scegli una sola opzione: --dry-run oppure --apply.")

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"File non trovato: {csv_path}. Lancia prima build_cities.py.")
    records, skipped, errors = build_records(csv_path, read_overrides(Path(args.overrides)))

    print(f"Lette dal CSV: {len(records) + len(skipped) + len(errors)} righe")
    print(f"  da caricare: {len(records)} (ok: {sum(r['check_status'] == 'ok' for r in records)}, "
          f"override: {sum(r['check_status'] == 'override' for r in records)})")
    for name, check in skipped:
        print(f"  NON caricata: {name} (check = {check})")
    for e in errors:
        print(f"  ERRORE: {e}", file=sys.stderr)
    if errors:
        sys.exit("Correggi gli errori sopra prima di caricare.")

    if not args.apply:  # dry run: mostra un'anteprima e si ferma
        print("\nANTEPRIMA (nessuna scrittura). Prime 3 righe, come andrebbero nel database:")
        for rec in records[:3]:
            print("  ", {k: rec[k] for k in ("wikidata", "name", "country_code", "population", "check_status", "check_auto")})
        for rec in records:
            if rec["check_status"] == "override":
                print(f"  override: {rec['name']} {rec['wikidata']} (auto: {rec['check_auto']}) - {rec['override_note']}")
        print("\nPer scrivere davvero: python load_cities.py --apply")
        return

    load_env(BASE / ".env")  # solo ora servono le credenziali
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        sys.exit("Mancano SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY: mettile nel file .env (vedi .env.example).")
    print(f"\nScrittura su {url.split('//')[-1]} nella tabella {TABLE}...")  # si stampa solo l'host, mai la chiave
    upsert(url, key, records)
    print("Controllo finale, righe nel database per check_status:", count_by_status(url, key))


if __name__ == "__main__":
    main()
