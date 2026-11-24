#!/usr/bin/env python3
"""Indica a load_attractions.py quale area scaricare per le città in cui il confine per codice non esiste.

Il problema: load_attractions.py cerca in OSM il confine che porta il codice Wikidata della città. Per alcune
città (es. Atene) quel confine non esiste e la risposta è vuota ("nessun oggetto"). La soluzione è scrivere
nella riga della città, in wikidata_cities, l'area giusta: area_method = 'confine' con relation_id (il confine
indicato a mano) oppure 'raggio' con radius_km (un cerchio attorno al centro). Questo script serve a farlo
senza scrivere a mano le query SQL: trova le città da sistemare, propone i confini, raccoglie la tua scelta e
la scrive. Senza --apply non scrive niente.

Tre modi di lavorare, tutti con lo stesso esito (una lista di decisioni città -> area):

    python fix_city_areas.py                       # cerca i candidati e scrive seed/aree_proposte.csv (da rivedere)
    python fix_city_areas.py --interattivo         # mostra ogni caso e chiede: numero, r (cerchio), i (id a mano), s (salta)
    python fix_city_areas.py --csv seed/aree_proposte.csv   # applica le decisioni di un file CSV o Excel (.xlsx)

    ... --apply           scrive davvero su wikidata_cities (chiede conferma, salvo --yes)
    ... --only Q1524,Q37100   invece delle città con l'ultima corsa "nessun_oggetto" (osm_ingestion_log), solo queste
    ... --raggio-di-riserva 10   per le città senza proposta, precompila un cerchio da 10 km (si rivede prima di applicare)

Il file CSV ha una riga per città e queste colonne (le altre si ignorano):
    wikidata     obbligatoria, es. Q1524
    area_method  confine | raggio | qid (qid = toglie l'eccezione); se manca si deduce dalle due colonne seguenti
    relation_id  il confine da usare (area_method = confine)
    radius_km    il raggio del cerchio in km (area_method = raggio)
Le righe senza area (nessun metodo, relation_id e radius_km vuoti) si saltano. Salvato da Excel con il punto e
virgola e la virgola decimale va bene lo stesso.

Dopo --apply, per riscaricare le città corrette:
    python load_attractions.py --only Q1524 --apply
Servono SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY nel .env, come per gli altri script.
"""

import argparse  # opzioni da riga di comando
import csv  # lettura e scrittura del file delle proposte
import os  # variabili d'ambiente (chiavi di Supabase)
import re  # lettura del livello amministrativo dalla descrizione di una relation
import sys  # uscita con messaggi
import time  # pausa tra le chiamate a Overpass
from pathlib import Path  # percorsi indipendenti dal sistema operativo

import requests  # API REST di Supabase

# riuso di funzioni già collaudate in load_attractions.py (importarlo non lancia niente)
import load_attractions as la

BASE = Path(__file__).parent  # cartella dello script (radice del repo)
PROPOSTE = BASE / "seed" / "aree_proposte.csv"  # file delle proposte, da rivedere e poi applicare
MAX_RATIO = 2.0  # un candidato è proponibile se la sua popolazione sta entro un fattore 2 da quella di Wikidata
WIDE_LEVEL = 4  # admin_level pari o inferiore (stato, regione): di solito troppo largo per una città, si avvisa
RESET_WORDS = {"qid", "reset", "nessuna"}  # valori di area_method che tolgono l'eccezione
CITY_COLUMNS = "wikidata,name,country_code,lat,lon,population,area_method,relation_id,radius_km"


# --------------------------------------------------------------------------- città da sistemare

def fetch_cities(url, key, qids=None):
    """Righe di wikidata_cities (dati utili alla decisione e area attuale): {QID: riga}."""
    query = f"{url}/rest/v1/{la.CITIES_TABLE}?select={CITY_COLUMNS}"
    if qids:
        query += f"&wikidata=in.({','.join(qids)})"
    r = requests.get(query, headers=la.headers(key), timeout=60)
    r.raise_for_status()
    return {row["wikidata"]: row for row in r.json()}


def cities_to_fix(url, key):
    """Codici delle città la cui ultima corsa di load_attractions.py è 'nessun_oggetto' (vista osm_ingestion_last).

    Le corse in errore non c'entrano: sono problemi del server o della rete, non dell'area.
    """
    r = requests.get(f"{url}/rest/v1/osm_ingestion_last?select=city_wikidata&status=eq.nessun_oggetto&order=city_wikidata",
                     headers=la.headers(key), timeout=60)
    if r.status_code == 404:
        sys.exit("La vista osm_ingestion_last non esiste: applica prima sql/003_osm_ingestion_log.sql, "
                 "oppure indica le città con --only Q1524,Q37100.")
    r.raise_for_status()
    return [row["city_wikidata"] for row in r.json()]


# --------------------------------------------------------------------------- candidati

def parse_population(text):
    """Numero di abitanti da un tag OSM ('664046', '664,046', '664 046'): None se non è leggibile."""
    digits = "".join(c for c in (text or "") if c.isdigit())
    return int(digits) if digits else None


def find_candidates(city):
    """Confini amministrativi che contengono il centro della città, dal più largo al più stretto.

    is_in prende tutte le aree che contengono il punto (lat, lon di wikidata_cities); rel(pivot) risale ai
    confini. Si escludono gli Stati (admin_level 2): per una città-Stato si indica l'id a mano.
    """
    query = (f'[out:json][timeout:90];\nis_in({city["lat"]},{city["lon"]})->.a;\n'
             f'rel(pivot.a)["boundary"="administrative"];\nout tags;\n')
    out = []
    for el in la.overpass_query(query).get("elements", []):
        tags = el.get("tags", {})
        level = tags.get("admin_level", "")
        level = int(level) if level.isdigit() else None
        if level == 2:
            continue
        out.append({"relation_id": el["id"], "name": tags.get("name:en") or tags.get("name") or "?", "level": level,
                    "wikidata": tags.get("wikidata"), "population": parse_population(tags.get("population"))})
    return sorted(out, key=lambda c: (c["level"] is None, c["level"] or 0, c["relation_id"]))


def propose(candidates, ref_population):
    """Sceglie il candidato con la popolazione più vicina a quella di Wikidata. Restituisce (candidato, motivo).

    È un suggerimento, non una decisione: la popolazione di Wikidata può riferirsi a un'area diversa dal
    confine (comune, città metropolitana). Senza dati confrontabili non propone niente.
    """
    if not ref_population:
        return None, "nessuna proposta: popolazione di Wikidata assente"
    best, best_ratio = None, None
    for c in candidates:
        if not c["population"]:
            continue
        ratio = max(c["population"], ref_population) / min(c["population"], ref_population)
        if best_ratio is None or ratio < best_ratio:
            best, best_ratio = c, ratio
    if best is None:
        return None, "nessuna proposta: nessun candidato ha la popolazione in OSM"
    if best_ratio > MAX_RATIO:
        return None, f"nessuna proposta: il candidato più vicino ha popolazione {fmt(best['population'])} contro {fmt(ref_population)}"
    why = f"popolazione OSM {fmt(best['population'])} contro {fmt(ref_population)} in Wikidata (rapporto {best_ratio:.2f})"
    if best["level"] is not None and best["level"] <= WIDE_LEVEL:
        why += f"; ATTENZIONE: confine molto ampio (liv. {best['level']}), verifica"
    return best, why


def fmt(n):
    """Numero con il punto delle migliaia (664.046); '—' se manca."""
    return f"{n:,}".replace(",", ".") if n else "—"


def describe(c):
    """Una riga di testo per un candidato."""
    return f"liv. {c['level'] if c['level'] is not None else '?'}  {c['name']}  pop {fmt(c['population'])}  relation {c['relation_id']}"


# --------------------------------------------------------------------------- file delle proposte e delle decisioni

def write_proposals(path, rows):
    """Scrive il file da rivedere. Separatore ';' e BOM: Excel italiano lo apre in colonne senza configurazioni."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["wikidata", "city", "country", "population", "area_method", "relation_id", "radius_km", "motivo", "candidati"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def read_table(path):
    """Righe di un file CSV o .xlsx come lista di dizionari con le intestazioni in minuscolo."""
    path = Path(path)
    if not path.exists():
        sys.exit(f"File non trovato: {path}")
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # facoltativa: serve solo per leggere Excel
        except ImportError:
            sys.exit("Per leggere un file Excel serve openpyxl (pip install openpyxl); in alternativa salvalo come CSV.")
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        head = [str(h or "").strip().lower() for h in rows[0]]
        return [dict(zip(head, ["" if v is None else str(v).strip() for v in r])) for r in rows[1:]]
    lines = path.read_text(encoding="utf-8-sig").splitlines()  # utf-8-sig toglie il BOM che Excel aggiunge
    if not lines:
        return []
    sep = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.DictReader(lines, delimiter=sep)
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def to_number(text):
    """Numero da un testo di Excel ('10', '10,5', '1370736.0'); None se vuoto. ValueError se non è un numero."""
    text = (text or "").strip().replace(",", ".")
    return float(text) if text else None


def decisions_from_rows(rows):
    """Trasforma le righe del file in decisioni. Restituisce (decisioni, errori, saltate).

    Una decisione è {"wikidata", "area_method", "relation_id", "radius_km"}; con area_method None toglie l'eccezione.
    """
    decisions, errors, skipped = [], [], []
    seen = set()
    for n, row in enumerate(rows, start=2):  # riga 1 = intestazioni
        qid = row.get("wikidata", "")
        if not la.QID.fullmatch(qid):
            errors.append(f"riga {n}: codice Wikidata non valido ({qid!r})")
            continue
        if qid in seen:
            errors.append(f"riga {n}: {qid} compare più volte")
            continue
        seen.add(qid)
        method = row.get("area_method", "").lower()
        try:
            rel, km = to_number(row.get("relation_id")), to_number(row.get("radius_km"))
        except ValueError:
            errors.append(f"riga {n}: {qid}: relation_id o radius_km non è un numero")
            continue
        if rel is not None and (rel != int(rel) or rel <= 0):
            errors.append(f"riga {n}: {qid}: relation_id deve essere un intero positivo")
            continue
        if km is not None and km <= 0:
            errors.append(f"riga {n}: {qid}: radius_km deve essere maggiore di zero")
            continue
        if method in RESET_WORDS:
            decisions.append({"wikidata": qid, "area_method": None, "relation_id": None, "radius_km": None})
            continue
        if not method:  # si deduce dai dati compilati
            if rel is not None and km is not None:
                errors.append(f"riga {n}: {qid}: sono compilati sia relation_id sia radius_km, indica area_method")
                continue
            method = "confine" if rel is not None else "raggio" if km is not None else ""
        if not method:
            skipped.append(qid)
            continue
        if method == "confine" and rel is None:
            errors.append(f"riga {n}: {qid}: area_method = confine ma relation_id è vuoto")
        elif method == "raggio" and km is None:
            errors.append(f"riga {n}: {qid}: area_method = raggio ma radius_km è vuoto")
        elif method not in ("confine", "raggio"):
            errors.append(f"riga {n}: {qid}: area_method deve essere confine, raggio o qid (non {method!r})")
        else:
            decisions.append({"wikidata": qid, "area_method": method,
                              "relation_id": int(rel) if method == "confine" else None,
                              "radius_km": km if method == "raggio" else None})
    return decisions, errors, skipped


# --------------------------------------------------------------------------- modalità interattiva

def ask(prompt):
    """input() che si ferma con un messaggio chiaro se non c'è un terminale (es. lanciato da uno scheduler)."""
    if not sys.stdin.isatty():
        sys.exit("La modalità interattiva richiede un terminale. Usa il file: --csv seed/aree_proposte.csv")
    return input(prompt).strip()


def ask_number(prompt, default=None, integer=False):
    """Chiede un numero positivo finché non è valido."""
    while True:
        text = ask(prompt).replace(",", ".") or (str(default) if default is not None else "")
        try:
            value = float(text)
            if value > 0 and (not integer or value == int(value)):
                return int(value) if integer else value
        except ValueError:
            pass
        print("  Valore non valido, riprova.")


def interactive(cities, found, reserve_km=None):
    """Presenta ogni caso, chiede la scelta e restituisce le decisioni. found = {QID: (candidati, proposta, motivo)}.

    Senza proposta il default è 's' (salta); con reserve_km è 'r' (cerchio di quel raggio), sempre modificabile.
    """
    decisions = []
    for n, (qid, city) in enumerate(cities.items(), start=1):
        candidates, best, why = found[qid]
        print(f"\n[{n}/{len(cities)}] {city['name']} ({qid}, {city['country_code']}): popolazione Wikidata {fmt(city['population'])}")
        print(f"  Area attuale: {describe_area(city.get('area_method'), city.get('relation_id'), float(city['radius_km']) if city.get('radius_km') else None)}")
        if candidates:
            print("  Confini che contengono il centro della città:")
            for i, c in enumerate(candidates, start=1):
                print(f"    {i}) {describe(c)}{'   <-- proposta' if c is best else ''}")
            print(f"  Proposta: {why}")
        else:
            print("  Nessun confine trovato attorno al centro.")
        default = str(candidates.index(best) + 1) if best else "r" if reserve_km else "s"
        while True:
            choice = ask(f"  Scelta: numero, r = cerchio, i = id relation a mano, s = salta [{default}]: ").lower() or default
            if choice == "s":
                break
            if choice == "r":
                km = ask_number(f"  Raggio in km [{(reserve_km or 10):g}]: ", default=reserve_km or 10)
                decisions.append({"wikidata": qid, "area_method": "raggio", "relation_id": None, "radius_km": km})
                break
            if choice == "i":
                rel = ask_number("  Id della relation: ", integer=True)
                decisions.append({"wikidata": qid, "area_method": "confine", "relation_id": rel, "radius_km": None})
                break
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                decisions.append({"wikidata": qid, "area_method": "confine",
                                  "relation_id": candidates[int(choice) - 1]["relation_id"], "radius_km": None})
                break
            print("  Scelta non valida, riprova.")
    return decisions


# --------------------------------------------------------------------------- verifica e scrittura

def verify_relations(decisions):
    """Controlla su Overpass che ogni relation indicata esista e sia un confine amministrativo.

    Evita il refuso (un id sbagliato darebbe una risposta vuota o, peggio, un'altra città). Restituisce
    ({relation_id: descrizione}, [errori]).
    """
    info, errors = {}, []
    for d in decisions:
        rel = d["relation_id"]
        if d["area_method"] != "confine" or rel in info:
            continue
        data = la.overpass_query(f"[out:json][timeout:60];\nrel(id:{rel});\nout tags;\n")
        els = data.get("elements", [])
        tags = els[0].get("tags", {}) if els else {}
        if not els:
            errors.append(f"{d['wikidata']}: la relation {rel} non esiste in OSM")
        elif tags.get("boundary") != "administrative":
            errors.append(f"{d['wikidata']}: la relation {rel} ({tags.get('name', '?')}) non è un confine amministrativo")
        else:
            info[rel] = f"{tags.get('name:en') or tags.get('name', '?')}, liv. {tags.get('admin_level', '?')}"
        time.sleep(1)  # pochi secondi di pausa: Overpass è condiviso
    return info, errors


def describe_area(method, rel, km, rel_info=None):
    """Testo dell'area per il riepilogo: 'confine per codice', 'relation 123 (nome)' o 'cerchio 10 km'."""
    if not method:
        return "confine per codice (nessuna eccezione)"
    if method == "confine":
        extra = f" ({rel_info[rel]})" if rel_info and rel in rel_info else ""
        m = re.search(r"liv\. (\d+)", rel_info.get(rel, "")) if rel_info else None
        if m and int(m.group(1)) <= WIDE_LEVEL:
            extra += f" ATTENZIONE: confine molto ampio (liv. {m.group(1)}), verifica"
        return f"relation {rel}{extra}"
    return f"cerchio {km:g} km"


def same_area(decision, city):
    """True se la decisione coincide con l'area già scritta in wikidata_cities (non c'è niente da modificare)."""
    km = float(city["radius_km"]) if city.get("radius_km") else None
    return (decision["area_method"] == city.get("area_method") and decision["relation_id"] == city.get("relation_id")
            and decision["radius_km"] == km)


def show_changes(decisions, cities, rel_info):
    """Stampa le modifiche: per ogni città, area attuale e area nuova."""
    print("\nModifiche a wikidata_cities:")
    for d in decisions:
        c = cities[d["wikidata"]]
        old = describe_area(c.get("area_method"), c.get("relation_id"), float(c["radius_km"]) if c.get("radius_km") else None)
        new = describe_area(d["area_method"], d["relation_id"], d["radius_km"], rel_info)
        print(f"  {d['wikidata']:<10} {c['name']:<22} {old}  ->  {new}")


def apply_changes(url, key, decisions):
    """Scrive le decisioni: un aggiornamento per città, con metodo e dati insieme (il database controlla la coerenza)."""
    for d in decisions:
        body = {"area_method": d["area_method"], "relation_id": d["relation_id"], "radius_km": d["radius_km"]}
        r = requests.patch(f"{url}/rest/v1/{la.CITIES_TABLE}?wikidata=eq.{d['wikidata']}",
                           headers={**la.headers(key), "Prefer": "return=representation"}, json=body, timeout=60)
        if r.status_code not in (200, 204):
            sys.exit(f"{d['wikidata']}: scrittura fallita, HTTP {r.status_code}: {r.text[:300]}")
        if len(r.json()) != 1:
            sys.exit(f"{d['wikidata']}: nessuna riga aggiornata (la città non è in {la.CITIES_TABLE}?)")
        print(f"  {d['wikidata']}: aggiornata")


# --------------------------------------------------------------------------- programma principale

def main():
    """Trova le città da sistemare, raccoglie le decisioni (file o interattivo) e, con --apply, le scrive."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="codici separati da virgola: invece delle città con ultima corsa 'nessun_oggetto'")
    ap.add_argument("--interattivo", action="store_true", help="chiede la scelta per ogni città")
    ap.add_argument("--csv", help="applica le decisioni di un file CSV o Excel (non cerca candidati)")
    ap.add_argument("--apply", action="store_true", help="scrive su wikidata_cities (senza, solo anteprima)")
    ap.add_argument("--yes", action="store_true", help="con --apply non chiede la conferma finale")
    ap.add_argument("--senza-verifica", action="store_true", help="non controlla su Overpass che le relation esistano")
    ap.add_argument("--pause", type=float, default=5, help="secondi di pausa tra una ricerca e l'altra su Overpass")
    ap.add_argument("--raggio-di-riserva", type=float, metavar="KM", dest="raggio_di_riserva",
                    help="per le città senza proposta di confine precompila un cerchio di questo raggio in km (da rivedere)")
    ap.add_argument("--output", default=str(PROPOSTE), help="dove scrivere il file delle proposte")
    args = ap.parse_args()
    if args.csv and args.interattivo:
        sys.exit("Scegli un modo solo: --csv oppure --interattivo.")
    if args.raggio_di_riserva is not None and args.raggio_di_riserva <= 0:
        sys.exit("--raggio-di-riserva deve essere maggiore di zero (km).")
    if args.csv and args.raggio_di_riserva:
        sys.exit("--raggio-di-riserva serve a preparare il file delle proposte: non ha effetto con --csv.")

    la.load_env(BASE / ".env")
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        sys.exit("Mancano SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY: mettile nel file .env (vedi .env.example).")

    # 1) quali città e con quale decisione di partenza
    if args.csv:
        decisions, errors, skipped = decisions_from_rows(read_table(args.csv))
        for e in errors:
            print(f"ERRORE: {e}", file=sys.stderr)
        if errors:
            sys.exit("Correggi gli errori sopra e rilancia: non è stato scritto niente.")
        if skipped:
            print(f"Righe senza area, saltate: {', '.join(skipped)}")
        cities = fetch_cities(url, key, [d["wikidata"] for d in decisions])
        missing = [d["wikidata"] for d in decisions if d["wikidata"] not in cities]
        if missing:
            sys.exit(f"Città non presenti in {la.CITIES_TABLE}: {', '.join(missing)}")
    else:
        qids = [q.strip() for q in args.only.split(",") if q.strip()] if args.only else cities_to_fix(url, key)
        bad = [q for q in qids if not la.QID.fullmatch(q)]
        if bad:
            sys.exit(f"Codici non validi: {bad}")
        if not qids:
            sys.exit("Nessuna città da sistemare: l'ultima corsa di ognuna ha trovato oggetti.")
        cities = fetch_cities(url, key, qids)
        missing = [q for q in qids if q not in cities]
        if missing:
            sys.exit(f"Città non presenti in {la.CITIES_TABLE}: {', '.join(missing)}")
        no_center = [q for q, c in cities.items() if c["lat"] is None or c["lon"] is None]
        if no_center:
            sys.exit(f"Mancano lat/lon in {la.CITIES_TABLE} per: {', '.join(no_center)}")
        elenco = [f"{c['name']} ({q})" for q, c in cities.items()]
        print(f"Città da sistemare: {', '.join(elenco)}\n")

        # 2) candidati per ciascuna (una chiamata a Overpass a città)
        found = {}
        for n, (qid, city) in enumerate(cities.items(), start=1):
            print(f"[{n}/{len(cities)}] {city['name']}: cerco i confini attorno al centro...")
            try:
                candidates = find_candidates(city)
            except Exception as e:  # noqa: BLE001 - la città si salta, le altre proseguono
                print(f"  ERRORE: {e}", file=sys.stderr)
                continue
            best, why = propose(candidates, city["population"])
            found[qid] = (candidates, best, why)
            print(f"  {len(candidates)} candidati. {why}")
            if n < len(cities):
                time.sleep(args.pause)
        cities = {q: c for q, c in cities.items() if q in found}  # le città con errore restano fuori

        if args.interattivo:
            decisions = interactive(cities, found, args.raggio_di_riserva)
        else:  # modalità file: si scrive il CSV con la proposta e ci si ferma
            rows = []
            for qid, city in cities.items():
                candidates, best, why = found[qid]
                reserve = args.raggio_di_riserva if not best else None  # il cerchio solo dove manca una proposta
                if reserve:
                    why += f"; ripiego: cerchio da {reserve:g} km"
                rows.append({"wikidata": qid, "city": city["name"], "country": city["country_code"],
                             "population": city["population"] or "",
                             "area_method": "confine" if best else "raggio" if reserve else "",
                             "relation_id": best["relation_id"] if best else "", "radius_km": reserve or "", "motivo": why, "candidati": " | ".join(describe(c) for c in candidates)})
            write_proposals(Path(args.output), rows)
            n_conf, n_circle = sum(bool(r["relation_id"]) for r in rows), sum(r["area_method"] == "raggio" for r in rows)
            print(f"\nScritto {args.output}: {len(rows)} città, {n_conf} con un confine proposto"
                  f"{f', {n_circle} con il cerchio di ripiego' if n_circle else ''}, {len(rows) - n_conf - n_circle} senza proposta.")
            print("Aprilo (Excel va bene), correggi area_method / relation_id / radius_km dove serve, salvalo, poi:")
            print(f"    python fix_city_areas.py --csv {args.output}           # anteprima")
            print(f"    python fix_city_areas.py --csv {args.output} --apply   # scrittura")
            return

    unchanged = [d["wikidata"] for d in decisions if same_area(d, cities[d["wikidata"]])]
    if unchanged:  # es. Invio sulla proposta quando l'area è già quella
        print(f"\nGià impostate così, nessuna modifica: {', '.join(unchanged)}")
        decisions = [d for d in decisions if d["wikidata"] not in unchanged]
    if not decisions:
        print("\nNessuna modifica: niente da scrivere.")
        return

    # 3) verifica, riepilogo e (solo con --apply) scrittura
    rel_info = {}
    if not args.senza_verifica:
        print("\nVerifico le relation su Overpass...")
        rel_info, errors = verify_relations(decisions)
        for e in errors:
            print(f"ERRORE: {e}", file=sys.stderr)
        if errors:
            sys.exit("Correggi gli errori sopra e rilancia: non è stato scritto niente.")
    show_changes(decisions, cities, rel_info)

    if not args.apply:
        print("\nNessuna scrittura. Per scrivere davvero aggiungi --apply.")
        return
    if not args.yes and ask(f"\nScrivo {len(decisions)} modifiche su {la.CITIES_TABLE}? [s/N] ").lower() != "s":
        sys.exit("Annullato: non è stato scritto niente.")
    apply_changes(url, key, decisions)
    qids = ",".join(d["wikidata"] for d in decisions if d["area_method"])
    print(f"\nFatto. Per scaricare le città corrette:\n    python load_attractions.py --only {qids} --apply")


if __name__ == "__main__":
    main()
