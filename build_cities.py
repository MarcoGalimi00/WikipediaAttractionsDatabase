"""Costruisce l'anagrafica delle 50 città (nome, paese, codice Wikidata, coordinate).

OBIETTIVO
    Per la pipeline delle attrazioni serve, per ogni città, il suo codice Wikidata (il "QID",
    per esempio Q220 per Roma). Il codice Wikidata è la chiave che lega tra loro OpenStreetMap,
    Wikipedia e il database. Questo script lo ricava da solo, così non va scritto a mano.

COME FUNZIONA (versione con le API, senza SPARQL)
    La versione precedente usava query SPARQL, che andavano in timeout. Qui si usano invece
    le due API "leggere" di Wikidata, che non fanno ricerche complesse:
      1. wbsearchentities: cerca un nome e restituisce i codici dei candidati;
      2. wbgetentities:    legge le voci dei candidati (paese, tipi, coordinate, lingue...),
                           fino a 50 voci in una sola chiamata.
    Poi il filtro per paese e l'ordinamento si fanno qui, in Python.
    Per ogni città i candidati si ordinano così:
      1. prima le voci che sono luoghi abitati: hanno un tipo in PLACE_TYPES oppure una
         popolazione (club, Olimpiadi, università e aeroporti non hanno popolazione),
      2. poi quelle presenti in più lingue su Wikipedia (una città famosa ha più lingue).

COSA SCRIVE (nella cartella indicata con --out, di default seed/)
    - citta_50.csv      una riga per città: la prima candidata, con una colonna `check`
    - candidati_50.csv  fino a 3 candidate per città, per la revisione a mano

    Valori di `check`:
      ok       = la prima candidata è un luogo abitato con coordinate e batte con margine le
                 altre: ha almeno MIN_RATIO volte le lingue del primo altro LUOGO e più
                 lingue di qualsiasi voce che non è un luogo (club, Olimpiadi, università...);
      verifica = va guardata a mano (rivale troppo vicino, o la prima non è un luogo);
      nessuna  = nessuna candidata trovata nel paese indicato;
      errore   = una chiamata all'API è fallita anche dopo i tentativi: va rilanciata
                 (non è un dato mancante), per esempio con --only;
      override = il codice è stato imposto a mano da override_qid.csv (vedi USO).
    La colonna `check_auto` dice sempre cosa avrebbe deciso l'algoritmo, anche quando
    `check` è override: così si capisce se un override serve ancora.

    La colonna `types` mostra i tipi della voce come codici (Q515, Q484170...). Serve a
    decidere quali tipi aggiungere all'elenco dei luoghi abitati, se una città esce `verifica`
    solo perché il suo tipo non è ancora nell'elenco.

NON SCRIVE SU SUPABASE. Il caricamento nel database viene dopo, quando i codici sono rivisti.

USO
    python build_cities.py                          # scrive in seed/
    python build_cities.py --out altra              # scrive in altra/
    python build_cities.py --place-types tipi.csv   # aggiunge altri tipi di luogo abitato
    python build_cities.py --pause 2                # pausa di 2s tra le chiamate
    python build_cities.py --min-ratio 1.5          # vantaggio minimo sulle lingue (default 2.0)
    python build_cities.py --only Amsterdam Siviglia  # solo queste città; scrive file con
                                                      # suffisso _only, senza toccare i completi
    python build_cities.py --overrides miei.csv     # codici imposti a mano (default:
                                                    # override_qid.csv nella cartella --out;
                                                    # colonne: city,wikidata,nota)
    Serve la libreria: requests
"""
# --- Importazioni: tutte della libreria standard di Python, tranne `requests` ---
import argparse  # legge le opzioni dalla riga di comando
import csv  # legge e scrive i file CSV
import sys  # serve per scrivere i messaggi di errore su stderr
import time  # serve per le pause tra una chiamata e l'altra
from pathlib import Path  # gestione dei percorsi di file e cartelle

import requests  # libreria esterna per le chiamate HTTP (pip install requests)

import config  # perimetro e indirizzi dei servizi: config.toml

# truststore fa usare a Python l'archivio dei certificati di Windows (o del sistema) invece di quello
# incluso in "certifi". Serve nelle reti aziendali che ispezionano il traffico HTTPS con un
# certificato proprio: quel certificato è già nell'archivio di Windows, ma non in certifi.
# È facoltativo: se non è installato (pip install truststore) si usa l'archivio di certifi.
try:
    import truststore

    truststore.inject_into_ssl()  # da qui in poi requests si fida dei certificati di sistema
except ImportError:
    pass

# --- Costanti di configurazione ---

# Indirizzo dell'API di Wikidata (la stessa per ricerca e lettura delle voci).
API_URL = config.WIKIDATA_API  # config.toml, [servizi] wikidata_api

# Wikimedia chiede che ogni script si identifichi con un User-Agent descrittivo.
# Il contatto è quello scelto in config.toml.
USER_AGENT = config.user_agent("build_cities.py")  # contatto in config.toml, [http] contact

# Pausa (in secondi) dopo ogni chiamata: il servizio è pubblico e condiviso, non va martellato.
# Si può cambiare con --pause (la funzione main() aggiorna questa variabile).
PAUSE = 1.0

# Elenco delle città. Ogni riga è una tupla di quattro valori:
#   (nome nella lista, etichetta italiana, etichetta inglese, paese ISO 3166-1 alpha-2)
# Il paese serve a distinguere i nomi ambigui: Orlando (US), Nizza (FR), Venezia (IT),
# Porto (PT) esistono come nome di più luoghi. Le due etichette servono perché su Wikidata
# alcune voci hanno l'etichetta italiana diversa da quella che scriviamo noi: si cerca con
# entrambe e si uniscono i candidati.
CITIES = config.CITIES  # elenco in config.toml (chiave «cities»)

# Codici Wikidata dei "tipi" che identificano un luogo abitato:
#   Q486972  = insediamento umano (il tipo più generale)
#   Q515     = città
#   Q15284   = comune/municipalità
#   Q1549591 = grande città
# ATTENZIONE: qui si confronta il tipo ESATTO della voce, senza seguire la gerarchia dei
# sottotipi (con le API non si può). Una voce di tipo "comune francese" non conta come luogo
# abitato finché Q484170 non è in questo elenco. Un tipo mancante non scarta la città: la
# fa uscire con check = verifica. Per aggiungere tipi usa --place-types (vedi sopra).
PLACE_TYPES = config.PLACE_TYPES  # config.toml, [wikidata] place_types

# Paesi che su Wikidata NON hanno il codice ISO sulla propria voce, a cui lo associamo noi.
# Caso trovato: Amsterdam ha come paese i "Paesi Bassi" (Q55), ma il codice NL è scritto
# sul "Regno dei Paesi Bassi" (Q29999). Senza questa riga il filtro per paese scarta la città.
# Il dizionario riempie in partenza la cache dei paesi {codice paese: ISO}, quindi quei
# paesi non vengono nemmeno richiesti all'API.
KNOWN_COUNTRIES = config.KNOWN_COUNTRIES  # config.toml, [wikidata.known_countries]

# Vantaggio minimo, in numero di lingue, che la prima candidata deve avere sul primo rivale
# che è anch'esso un luogo abitato perché la scelta sia 'ok'. 2.0 = il doppio.
# Dati osservati sulle 50 città: New York 1.32 (ambigua davvero), Dubrovnik 1.91, tutte le altre
# sopra 2.0. Si può cambiare da riga di comando con --min-ratio.
MIN_RATIO = config.MIN_RATIO  # config.toml, [wikidata] min_ratio

# Errori che l'API di Wikidata restituisce dentro una risposta HTTP 200 ma sono temporanei
# (server sovraccarico, replica in ritardo, limite di richieste): vale la pena riprovare.
# Anche tutti i codici che iniziano con "internal_api_error" sono trattati così.
TRANSIENT_API_ERRORS = {"maxlag", "ratelimited", "readonly"}


# --- Funzioni di accesso all'API ---


def api_get(session, params, tries=4):
    """Fa una chiamata all'API di Wikidata e restituisce la risposta come dizionario.

    Se il servizio risponde con un errore temporaneo (429 = troppe richieste,
    5xx = problema del server) aspetta e riprova, fino a `tries` volte.
    """
    params = {**params, "format": "json"}  # copia dei parametri, con la risposta in JSON
    for attempt in range(1, tries + 1):  # tentativo numero 1, 2, 3, 4
        try:
            r = session.get(API_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
        except requests.exceptions.SSLError as e:
            # Un errore di certificato non passa riprovando: si spiega la causa e si ferma.
            raise RuntimeError(
                "Certificato HTTPS non accettato (rete aziendale che ispeziona il traffico?). "
                "Soluzione: pip install truststore, poi rilancia lo script. "
                f"Dettaglio: {str(e)[:200]}"
            ) from e
        if r.status_code == 200:  # 200 = tutto bene
            data = r.json()
            if "error" in data:  # l'API segnala gli errori anche con codice 200
                err = data["error"] if isinstance(data["error"], dict) else {"info": str(data["error"])}
                code = str(err.get("code", ""))  # codice dell'errore, es. "maxlag"
                # Errore temporaneo: si riprova come per gli errori HTTP (attesa crescente).
                # Errore permanente (es. parametri sbagliati): si ferma subito.
                # Il codice di alcuni errori temporanei non è noto in anticipo (es. la ricerca
                # sovraccarica risponde "Search is currently too busy"): si guarda anche il testo.
                info = str(err.get("info", "")).lower()
                busy = "too busy" in info or "try again later" in info
                if attempt < tries and (code in TRANSIENT_API_ERRORS or code.startswith("internal_api_error") or busy):
                    wait = 5 * attempt
                    print(f"  Errore API temporaneo ({code}), riprovo tra {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue  # nuovo tentativo
                raise RuntimeError(f"Errore API Wikidata ({code}): {err.get('info', err)}")
            time.sleep(PAUSE)  # pausa di cortesia prima della prossima chiamata
            return data
        # Errori temporanei: si riprova, tranne all'ultimo tentativo
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries:
            # Se il server dice quanto aspettare (Retry-After) lo si rispetta,
            # altrimenti si aspetta 5s, 10s, 15s... (attesa crescente)
            wait = int(r.headers.get("Retry-After", 5 * attempt))
            print(f"  HTTP {r.status_code}, riprovo tra {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue  # torna all'inizio del ciclo: nuovo tentativo
        # Qualsiasi altro errore (o ultimo tentativo fallito): solleva un'eccezione
        r.raise_for_status()
    return {}  # non dovrebbe mai arrivarci; è una rete di sicurezza


def search_ids(session, label, lang):
    """Cerca un nome su Wikidata e restituisce la lista dei codici dei candidati.

    label: il nome da cercare. lang: la lingua in cui cercarlo ('it' o 'en').
    Restituisce al massimo 10 codici, in ordine di rilevanza (decide Wikidata).
    """
    data = api_get(session, {
        "action": "wbsearchentities",  # l'azione di ricerca per nome
        "search": label,  # il testo cercato
        "language": lang,  # lingua in cui si cerca il nome
        "uselang": lang,  # lingua delle descrizioni nella risposta
        "type": "item",  # solo voci (non proprietà)
        "limit": 10,  # al massimo 10 risultati
    })
    return [hit["id"] for hit in data.get("search", [])]  # solo i codici, es. Q90


def get_entities(session, ids, props):
    """Legge le voci con i codici dati e le restituisce come {codice: voce}.

    ids: lista di codici. props: cosa chiedere, separato da '|' (per esempio
    'claims|sitelinks|labels'). L'API accetta al massimo 50 codici per chiamata,
    quindi si spezza la lista in blocchi.
    """
    entities = {}
    for i in range(0, len(ids), 50):  # un blocco di 50 codici per volta
        chunk = ids[i:i + 50]
        data = api_get(session, {
            "action": "wbgetentities",  # l'azione di lettura delle voci
            "ids": "|".join(chunk),  # i codici separati da barra verticale
            "props": props,  # quali parti della voce vogliamo
            "languages": "it|en",  # le etichette solo in italiano e inglese
        })
        entities.update(data.get("entities", {}))  # aggiunge le voci lette al dizionario
    return entities


# --- Funzioni per leggere i dati dentro una voce ---


def best_claims(entity, pid):
    """Restituisce le "dichiarazioni" di una proprietà, tenendo solo quelle di rango migliore.

    In Wikidata ogni valore ha un rango: 'preferred' (preferito), 'normal' o 'deprecated'
    (da ignorare). Se esiste almeno un valore preferito si usano solo quelli, altrimenti
    tutti quelli non deprecati. È la stessa regola che applica `wdt:` nelle query SPARQL.
    """
    claims = entity.get("claims", {}).get(pid, [])  # tutte le dichiarazioni di quella proprietà
    valid = [c for c in claims if c.get("rank") != "deprecated"]  # scarta quelle deprecate
    preferred = [c for c in valid if c.get("rank") == "preferred"]  # quelle preferite
    return preferred or valid  # se ci sono preferite solo quelle, altrimenti tutte


def item_ids(entity, pid):
    """Restituisce i codici delle voci a cui punta una proprietà (per esempio il paese o i tipi)."""
    out = []
    for c in best_claims(entity, pid):
        snak = c.get("mainsnak", {})  # il valore principale della dichiarazione
        if snak.get("snaktype") != "value":  # scarta "valore sconosciuto" o "nessun valore"
            continue
        value = snak["datavalue"]["value"]  # per una voce è del tipo {"id": "Q142", ...}
        if isinstance(value, dict) and "id" in value:
            out.append(value["id"])
    return out


def first_coord(entity):
    """Restituisce (latitudine, longitudine) come testo, oppure ('', '') se mancano."""
    for c in best_claims(entity, "P625"):  # P625 = coordinate geografiche
        snak = c.get("mainsnak", {})
        if snak.get("snaktype") == "value":
            v = snak["datavalue"]["value"]  # è del tipo {"latitude": 41.89, "longitude": 12.48, ...}
            return str(v["latitude"]), str(v["longitude"])
    return "", ""  # nessuna coordinata


def first_population(entity):
    """Restituisce la popolazione come testo, oppure '' se manca."""
    for c in best_claims(entity, "P1082"):  # P1082 = popolazione
        snak = c.get("mainsnak", {})
        if snak.get("snaktype") == "value":
            # Il valore è del tipo {"amount": "+2800000", ...}: si toglie il segno "+"
            return snak["datavalue"]["value"]["amount"].lstrip("+")
    return ""


def country_iso_codes(session, country_ids, cache):
    """Dai codici dei paesi (es. Q142) ricava i codici ISO (es. FR).

    cache: dizionario {codice paese: ISO} che si riempie via via, così lo stesso paese non
    viene richiesto due volte nel corso dello script.
    """
    missing = [q for q in country_ids if q not in cache]  # paesi non ancora conosciuti
    if missing:
        for qid, ent in get_entities(session, missing, "claims").items():
            # P297 = codice ISO 3166-1 alpha-2 del paese, è un testo (non una voce)
            claims = best_claims(ent, "P297")
            iso = claims[0]["mainsnak"]["datavalue"]["value"] if claims else ""
            cache[qid] = iso  # anche se vuoto: così non lo richiediamo di nuovo
    return {q: cache.get(q, "") for q in country_ids}


# --- Costruzione delle candidate per una città ---


def build_row(qid, ent, place_types):
    """Trasforma una voce di Wikidata nel dizionario di una candidata (una riga dei CSV)."""
    types = list(dict.fromkeys(item_ids(ent, "P31")))  # P31 = "è un"; tolti i doppioni, ordine invariato
    lat, lon = first_coord(ent)
    population = first_population(ent)  # popolazione, vuota se non c'è
    labels = ent.get("labels", {})  # etichette per lingua, es. {"it": {"value": "Roma"}}
    label = (labels.get("it") or labels.get("en") or {}).get("value", "")
    return {
        "wikidata": qid,  # codice Wikidata, per esempio Q220
        "label": label,  # nome leggibile della voce
        "langs": len(ent.get("sitelinks", {})),  # numero di pagine collegate (= lingue)
        "population": population,
        "lat": lat,  # latitudine
        "lon": lon,  # longitudine
        # Luogo abitato = tipo in elenco OPPURE popolazione presente. La seconda condizione
        # copre i tipi che l'elenco non conosce (es. "comune spagnolo") ed esclude club,
        # Olimpiadi, università e aeroporti, che non hanno popolazione.
        "is_place": bool(population) or any(t in place_types for t in types),
        "types": ", ".join(types),  # i tipi come codici, per la revisione a mano
    }


def candidates_for(session, label_it, label_en, iso, place_types, cache):
    """Restituisce la lista delle candidate (dizionari) per una città, in ordine di preferenza."""
    # 1. Cerca il nome in italiano e in inglese, e unisce i codici senza ripetizioni
    ids = []
    for label, lang in ((label_it, "it"), (label_en, "en")):
        for qid in search_ids(session, label, lang):
            if qid not in ids:  # mantiene l'ordine di arrivo e scarta i doppioni
                ids.append(qid)
    if not ids:  # nessun risultato: la città non è stata trovata
        return []

    # 2. Legge le voci di tutti i candidati in una sola chiamata
    entities = get_entities(session, ids, "claims|sitelinks|labels")

    # 3. Ricava il codice ISO di ogni paese citato dai candidati
    country_ids = sorted({c for e in entities.values() for c in item_ids(e, "P17")})
    iso_of = country_iso_codes(session, country_ids, cache)

    # 4. Costruisce le candidate, tenendo solo quelle nel paese richiesto
    rows = []
    for qid in ids:  # nell'ordine di ricerca
        ent = entities.get(qid)
        if not ent or "missing" in ent:  # voce cancellata o inesistente
            continue
        # P17 = paese della voce; la voce può averne più di uno (per esempio storici)
        countries = [iso_of.get(c, "") for c in item_ids(ent, "P17")]
        if iso not in countries:  # non è nel paese giusto (es. Roma in Texas): scartata
            continue
        rows.append(build_row(qid, ent, place_types))  # vedi build_row sopra

    # 5. Ordina: prima i luoghi abitati, poi quelli con più lingue.
    #    `not r["is_place"]` vale False (0) per i luoghi, quindi vengono per primi;
    #    il segno meno davanti a langs mette per primo il numero più alto.
    rows.sort(key=lambda r: (not r["is_place"], -r["langs"]))
    return rows


def judge(rows, min_ratio=MIN_RATIO):
    """Giudica quanto è affidabile la scelta della prima candidata.

    Restituisce 'ok', 'verifica' oppure 'nessuna' (vedi la descrizione in testa al file).
    `rows` è già ordinata: prima i luoghi abitati, poi per numero di lingue decrescente.
    """
    if not rows:  # nessuna candidata: la città non è stata trovata
        return "nessuna"
    first = rows[0]
    # La prima deve essere un luogo abitato (popolazione o tipo noto) e avere le coordinate.
    # Se non lo è, nessuna candidata lo è (le righe sono ordinate): decide una persona.
    if not first["is_place"] or not first["lat"]:
        return "verifica"
    rivals = rows[1:]  # tutte le altre candidate
    places = [r for r in rivals if r["is_place"]]  # rivali che sono anch'essi luoghi
    others = [r for r in rivals if not r["is_place"]]  # club, Olimpiadi, università...
    # Contro un altro LUOGO (contea, Stato, omonimo) serve un vantaggio netto di lingue.
    # `places[0]` è quello con più lingue, perché le righe sono ordinate.
    if places and first["langs"] < min_ratio * places[0]["langs"]:
        return "verifica"
    # Le voci che non sono luoghi non competono, ma non devono essere più famose della prima:
    # se lo fossero, la prima potrebbe essere un paesino mentre la vera città ha poca
    # popolazione registrata. In quel caso decide una persona.
    if any(r["langs"] >= first["langs"] for r in others):
        return "verifica"
    return "ok"


def load_place_types(path):
    """Legge un file con un codice Wikidata per riga (prima colonna) e li aggiunge ai tipi."""
    types = set(PLACE_TYPES)  # parte dall'elenco di base
    if path:
        with open(path, encoding="utf-8") as f:
            for line in f:
                code = line.split(",")[0].strip()  # prima colonna, senza spazi
                if code.startswith("Q"):  # salta intestazioni, righe vuote e commenti
                    types.add(code)
    return types


def load_overrides(path):
    """Legge i codici imposti a mano: file CSV con colonne city,wikidata,nota.

    Restituisce {nome città in minuscolo: codice}. Se il file non esiste restituisce {}.
    """
    result = {}
    if path and Path(path).exists():
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                qid = (row.get("wikidata") or "").strip()
                city = (row.get("city") or "").strip()
                if city and qid.startswith("Q"):  # salta righe vuote o incomplete
                    result[city.casefold()] = qid
    return result


def main():
    """Punto di ingresso: legge le opzioni, ripete la ricerca per ogni città, scrive i CSV."""
    global PAUSE  # serve per poter cambiare la costante PAUSE da qui

    # --- Opzioni dalla riga di comando ---
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="seed", help="cartella di destinazione (default: seed)")
    ap.add_argument("--place-types", default=None,
                    help="file con altri codici di tipo 'luogo abitato', uno per riga")
    ap.add_argument("--pause", type=float, default=PAUSE, help="secondi di pausa tra le chiamate")
    ap.add_argument("--only", nargs="+", default=None,
                    help="elabora solo queste città (nomi come in CITIES); scrive file con suffisso _only")
    ap.add_argument("--overrides", default=None,
                    help="CSV city,wikidata,nota con codici imposti a mano (default: <out>/override_qid.csv)")
    ap.add_argument("--min-ratio", type=float, default=MIN_RATIO,
                    help="vantaggio minimo sulle lingue rispetto al primo altro luogo (default: 2.0)")
    args = ap.parse_args()
    PAUSE = args.pause
    min_ratio = args.min_ratio
    place_types = load_place_types(args.place_types)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)  # crea la cartella se non esiste
    overrides = load_overrides(args.overrides or out / "override_qid.csv")

    # Con --only si tengono solo le città richieste (confronto senza maiuscole/minuscole)
    cities = CITIES
    suffix = ""  # suffisso dei file di uscita
    if args.only:
        wanted = {n.casefold() for n in args.only}
        cities = [c for c in CITIES if c[0].casefold() in wanted]
        suffix = "_only"  # così i file completi non vengono sovrascritti
        if not cities:
            sys.exit(f"Nessuna città trovata tra: {', '.join(args.only)}")

    # Una sola sessione HTTP per tutte le chiamate: riusa la connessione ed è più veloce
    session = requests.Session()
    country_cache = dict(KNOWN_COUNTRIES)  # {codice paese: ISO}, parte dai paesi noti
    best = []  # una riga per città (la prima candidata)
    cands = []  # fino a 3 righe per città (per la revisione a mano)

    # --- Ciclo principale: una ricerca per ogni città ---
    for i, (name, lab_it, lab_en, iso) in enumerate(cities, 1):  # i parte da 1
        print(f"[{i}/{len(cities)}] {name} ({iso})")  # avanzamento a schermo
        forced = overrides.get(name.casefold())  # codice imposto a mano, oppure None

        # 1. Ricerca normale, ANCHE per le città con override: serve a mostrare le alternative
        #    in candidati_50.csv e a sapere cosa direbbe l'algoritmo senza l'override.
        failed = False  # diventa True se una chiamata fallisce dopo tutti i tentativi
        try:
            rows = candidates_for(session, lab_it, lab_en, iso, place_types, country_cache)
        except (requests.RequestException, RuntimeError) as e:
            # Errore di rete o dell'API dopo tutti i tentativi: si segnala e si prosegue
            # con la città successiva, senza fermare tutto lo script
            print(f"  ERRORE: {e}", file=sys.stderr)
            rows = []
            failed = True
        # Con un errore il risultato è 'errore' (da rilanciare), non 'nessuna' (dato mancante)
        check_auto = "errore" if failed else judge(rows, min_ratio)
        check = check_auto

        # 2. Override: la voce imposta va in testa, le altre candidate restano sotto
        if forced:
            try:
                ent = get_entities(session, [forced], "claims|sitelinks|labels").get(forced, {})
                if ent and "missing" not in ent:
                    forced_row = build_row(forced, ent, place_types)
                    rows = [forced_row] + [r for r in rows if r["wikidata"] != forced]
                    check = "override"
                else:  # il QID imposto non esiste: si segnala e vale il giudizio automatico
                    print(f"  ATTENZIONE: {forced} non esiste su Wikidata, override ignorato", file=sys.stderr)
            except (requests.RequestException, RuntimeError) as e:
                print(f"  ERRORE (override {forced}): {e}", file=sys.stderr)
                check = "errore"
        top = rows[0] if rows else {}  # la prima candidata, oppure niente

        # Riga di citta_50.csv: se non c'è una candidata, i campi restano vuoti
        best.append({
            "city": name,
            "country_code": iso,
            "wikidata": top.get("wikidata", ""),
            "wikidata_label": top.get("label", ""),
            "lat": top.get("lat", ""),
            "lon": top.get("lon", ""),
            "population": top.get("population", ""),
            "n_languages": top.get("langs", ""),
            "types": top.get("types", ""),
            "check": check,
            "check_auto": check_auto,  # cosa ha deciso l'algoritmo, anche con override
        })
        # Righe di candidati_50.csv: le prime tre, con la posizione in classifica (rank)
        for rank, r in enumerate(rows[:3], 1):
            cands.append({"city": name, "country_code": iso, "rank": rank, **r})

    def write(path, rows, fields):
        """Scrive una lista di dizionari in un file CSV, con la riga di intestazione."""
        # newline="" evita righe vuote in più su Windows; utf-8 mantiene accenti e simboli
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)  # `fields` fissa l'ordine delle colonne
            w.writeheader()
            w.writerows(rows)

    # Se TUTTE le chiamate sono fallite (rete, certificato...), non si sovrascrivono i CSV buoni.
    if best and all(b["check"] == "errore" for b in best):
        sys.exit("Tutte le città sono andate in errore: i file NON sono stati scritti. "
                 "Controlla il primo messaggio ERRORE qui sopra (rete, certificato HTTPS) e rilancia.")

    # --- Scrittura dei due file ---
    write(
        out / f"citta_50{suffix}.csv",
        best,
        ["city", "country_code", "wikidata", "wikidata_label", "lat", "lon",
         "population", "n_languages", "types", "check", "check_auto"],
    )
    write(
        out / f"candidati_50{suffix}.csv",
        cands,
        ["city", "country_code", "rank", "wikidata", "label", "langs", "population",
         "lat", "lon", "is_place", "types"],
    )

    # --- Riepilogo finale ---
    da_guardare = [b["city"] for b in best if b["check"] not in ("ok", "override")]
    print(f"\nFatto: {len(best) - len(da_guardare)} ok, {len(da_guardare)} da guardare")
    if da_guardare:
        print("Da guardare in candidati_50.csv:", ", ".join(da_guardare))
    errori = [b["city"] for b in best if b["check"] == "errore"]  # chiamate fallite
    if errori:
        # Le città con errore non sono dati mancanti: basta rilanciarle da sole
        elenco = " ".join(f'"{c}"' for c in errori)
        print(f"Chiamate fallite: rilancia con  python build_cities.py --only {elenco}")


# Questo blocco fa partire main() solo quando il file viene lanciato direttamente
# (python build_cities.py), non quando viene importato da un altro file.
if __name__ == "__main__":
    main()
