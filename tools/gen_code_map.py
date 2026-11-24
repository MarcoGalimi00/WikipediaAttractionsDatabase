"""Genera la "mappa delle funzioni" di uno script della pipeline per il vault Obsidian.

PERCHÉ ESISTE
    La documentazione scritta a mano si desincronizza dal codice. Questa mappa invece si
    ricostruisce dal codice stesso: elenco delle funzioni, righe, parametri, prima riga del
    docstring, chi chiama chi, e un grafo Mermaid delle chiamate. Si rilancia dopo ogni
    modifica al codice e il vault resta allineato. Funziona per build_cities.py, load_cities.py
    load_attractions.py e fix_city_areas.py (e per qualunque altro file Python con funzioni al livello più esterno).

COME FUNZIONA
    Legge build_cities.py SENZA eseguirlo: lo trasforma in albero sintattico con il modulo
    `ast` della libreria standard e lo percorre. Non servono librerie esterne.

USO (dalla radice del repo, con qualsiasi Python 3)
    python tools/gen_code_map.py                       # le mappe di tutti gli script
    python tools/gen_code_map.py --src load_cities.py  # una sola
    python tools/gen_code_map.py --src x.py --out "percorso/della/nota.md"
"""
import argparse  # opzioni da riga di comando
import ast  # analisi sintattica del codice sorgente
from pathlib import Path  # percorsi di file

# Radice del repo: la cartella che contiene "tools/" (questo file sta in tools/)
ROOT = Path(__file__).resolve().parent.parent

# Cartella delle note generate nel vault (la cartella del vault si chiama "MindTrip - Attractions")
VAULT_SCRIPT = ROOT / "MindTrip - Attractions" / "3 Tecnico"

# Le note del vault hanno un numero davanti (ordine di lettura). Qui i numeri delle note che le mappe
# nominano o che il generatore scrive: se si rinumera il vault si aggiorna questa tabella.
NUMERO_NOTA = {
    "build_cities.py - panoramica": "07", "load_cities.py - panoramica": "12",
    "load_attractions.py - panoramica": "13", "fix_city_areas.py - panoramica": "14",
    "Esempio Roma passo passo": "11", "Input - CITIES, paesi noti e override": "04",
    "build_cities.py - mappa funzioni": "16", "load_cities.py - mappa funzioni": "17",
    "load_attractions.py - mappa funzioni": "18", "fix_city_areas.py - mappa funzioni": "19",
}


def nome_nota(titolo):
    """Nome del file di una nota, con il numero davanti ('13 load_attractions.py - panoramica')."""
    return f"{NUMERO_NOTA[titolo]} {titolo}" if titolo in NUMERO_NOTA else titolo


def link(titolo):
    """Link Obsidian a una nota: porta al file numerato e mostra il titolo senza numero."""
    return f"[[{nome_nota(titolo)}|{titolo}]]"

# Script analizzati di base
SCRIPT = ["build_cities.py", "load_cities.py", "load_attractions.py", "fix_city_areas.py"]

# Raggruppamento delle funzioni nel grafo, per script: (titolo del riquadro, funzioni che contiene).
# Le funzioni non elencate finiscono nel riquadro "Altro", così una funzione nuova non sparisce.
GRUPPI_PER_FILE = {
    "build_cities.py": [
        ("Accesso all'API", ["api_get", "search_ids", "get_entities"]),
        ("Lettura di una voce", ["best_claims", "item_ids", "first_coord", "first_population"]),
        ("Logica delle candidate", ["country_iso_codes", "build_row", "candidates_for", "judge"]),
        ("File e orchestrazione", ["load_place_types", "load_overrides", "main"]),
    ],
    "load_cities.py": [
        ("Lettura dei file", ["read_overrides", "build_records", "to_int", "to_float"]),
        ("Supabase", ["load_env", "headers", "upsert", "count_by_status"]),
        ("Orchestrazione", ["main"]),
    ],
    "load_attractions.py": [
        ("Lista di città", ["read_cities", "cities_from_db"]),
        ("Area di ogni città", ["area_from_row", "area_label", "areas_from_db"]),
        ("Overpass", ["build_query", "overpass_query", "fetch_city"]),
        ("Trasformazione", ["to_records"]),
        ("Supabase", ["load_env", "headers", "known_cities", "upsert", "count_city", "log_run"]),
        ("Mail di esito", ["send_mail"]),
        ("Orchestrazione", ["main", "run_with_report"]),
    ],
    "fix_city_areas.py": [
        ("Città e candidati", ["fetch_cities", "cities_to_fix", "parse_population", "find_candidates", "propose"]),
        ("File CSV o Excel", ["write_proposals", "read_table", "to_number", "decisions_from_rows"]),
        ("Modalità interattiva", ["ask", "ask_number", "interactive"]),
        ("Verifica e scrittura", ["verify_relations", "same_area", "show_changes", "apply_changes"]),
        ("Testi", ["fmt", "describe", "describe_area"]),
        ("Orchestrazione", ["main"]),
    ],
}


def prima_riga_docstring(node):
    """Restituisce la prima riga del docstring di una funzione, oppure '' se manca."""
    doc = ast.get_docstring(node)
    return doc.splitlines()[0].strip() if doc else ""


def valore_breve(src, node):
    """Rappresenta il valore di una costante in poche parole (le liste lunghe diventano un conteggio)."""
    testo = ast.get_source_segment(src, node) or ""
    if "\n" in testo or len(testo) > 70:  # costante lunga o su più righe
        if isinstance(node, (ast.List, ast.Set, ast.Tuple, ast.Dict)):
            n = len(node.elts) if not isinstance(node, ast.Dict) else len(node.keys)
            return f"{n} elementi"
        return "(valore lungo)"
    return testo


def main():
    """Rigenera le mappe delle funzioni di tutti gli script nel vault."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="file Python da analizzare (se manca: tutti gli script della pipeline)")
    ap.add_argument("--out", help="nota Markdown da scrivere (di base: <vault>/3 Tecnico/<NN> <script> - mappa funzioni.md)")
    args = ap.parse_args()

    if args.src:
        genera(Path(args.src), Path(args.out) if args.out else VAULT_SCRIPT / f"{nome_nota(Path(args.src).name + ' - mappa funzioni')}.md")
    else:
        for nome in SCRIPT:
            genera(ROOT / nome, VAULT_SCRIPT / f"{nome_nota(nome + ' - mappa funzioni')}.md")


def genera(src_path, dest):
    """Analizza un file Python e scrive la sua mappa delle funzioni in `dest`."""
    nome_file = src_path.name
    gruppi = GRUPPI_PER_FILE.get(nome_file, [])  # senza gruppi, tutto finisce in "Altro"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)  # albero sintattico dell'intero file

    # Funzioni definite al livello più esterno del file (le funzioni annidate, come write()
    # dentro main(), non sono elencate: fanno parte di main)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    # chiama[f] = funzioni del file chiamate da f (ignora le chiamate a librerie e le ricorsioni)
    chiama = {}
    for nome, node in funcs.items():
        trovate = set()
        for c in ast.walk(node):  # visita ogni nodo dentro la funzione
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name):
                if c.func.id in funcs and c.func.id != nome:
                    trovate.add(c.func.id)
        chiama[nome] = sorted(trovate)
    chiamata_da = {n: sorted(k for k, v in chiama.items() if n in v) for n in funcs}

    # Costanti: assegnazioni di primo livello con nome tutto maiuscolo
    costanti = []
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            if n.targets[0].id.isupper():
                costanti.append((n.targets[0].id, n.lineno, valore_breve(src, n.value)))

    righe_totali = len(src.splitlines())
    out = []
    w = out.append  # scorciatoia per aggiungere righe

    # --- Intestazione della nota (frontmatter + avviso) ---
    w("---")
    w("tipo: tecnico")
    w("tags: [tecnico, script, generata]")
    w("---")
    w(f"# {nome_file} - mappa funzioni")
    w("")
    w("> [!warning] File generato")
    w("> Non modificarlo a mano. Si rigenera dal codice con `python tools/gen_code_map.py`")
    w(f"> (dalla radice del repo). Sorgente analizzato: `{nome_file}`, {righe_totali} righe.")
    w("")
    w(f"Torna alla {link(nome_file + ' - panoramica')}.")
    if nome_file == "build_cities.py":
        w(f"Per un percorso concreto vedi {link('Esempio Roma passo passo')}.")
    w("")

    # --- Grafo delle chiamate ---
    w("## Chi chiama chi")
    w("")
    w("Una freccia `A --> B` significa «A chiama B». `main` è il punto di ingresso.")
    w("")
    w("```mermaid")
    w("flowchart LR")
    nel_gruppo = set()
    for idx, (titolo, nomi) in enumerate(gruppi):
        presenti = [n for n in nomi if n in funcs]
        if not presenti:
            continue
        w(f'  subgraph G{idx}["{titolo}"]')
        for n in presenti:
            w(f'    {n}["{n}()"]')
            nel_gruppo.add(n)
        w("  end")
    altre = [n for n in funcs if n not in nel_gruppo]
    if altre:  # funzioni nuove non ancora assegnate a un gruppo
        w('  subgraph GX["Altro"]')
        for n in altre:
            w(f'    {n}["{n}()"]')
        w("  end")
    for nome in funcs:
        for chiamata in chiama[nome]:
            w(f"  {nome} --> {chiamata}")
    w("  classDef ingresso fill:#22c55e,stroke:#15803d,color:#fff")
    if "main" in funcs:
        w("  class main ingresso")
    w("```")
    w("")

    # --- Tabella delle funzioni, nell'ordine in cui compaiono nel file ---
    w("## Funzioni")
    w("")
    w("| Funzione | Righe | Parametri | Cosa fa | Chiama | Chiamata da |")
    w("| --- | --- | --- | --- | --- | --- |")
    for nome, node in funcs.items():
        params = ", ".join(a.arg for a in node.args.args) or "nessuno"
        righe = f"{node.lineno}-{node.end_lineno} ({node.end_lineno - node.lineno + 1})"
        c = ", ".join(f"`{x}`" for x in chiama[nome]) or "-"
        cb = ", ".join(f"`{x}`" for x in chiamata_da[nome]) or "-"
        w(f"| `{nome}` | {righe} | `{params}` | {prima_riga_docstring(node)} | {c} | {cb} |")
    w("")

    # --- Costanti ---
    w("## Costanti")
    w("")
    w("| Costante | Riga | Valore |")
    w("| --- | --- | --- |")
    for nome, riga, valore in costanti:
        w(f"| `{nome}` | {riga} | `{valore}` |")
    w("")
    if nome_file == "build_cities.py":
        w(f"Le costanti si leggono in {link('Input - CITIES, paesi noti e override')}.")
        w("")

    # --- Classi (se ce ne sono) ---
    classi = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if classi:
        w("## Classi")
        w("")
        w("| Classe | Righe | Metodi | Cosa fa |")
        w("| --- | --- | --- | --- |")
        for c in classi:
            metodi = ", ".join(f"`{m.name}`" for m in c.body if isinstance(m, ast.FunctionDef))
            w(f"| `{c.name}` | {c.lineno}-{c.end_lineno} | {metodi} | {prima_riga_docstring(c)} |")
        w("")

    dest.parent.mkdir(parents=True, exist_ok=True)  # crea le cartelle se mancano
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Scritto {dest} ({len(funcs)} funzioni, {len(costanti)} costanti)")


if __name__ == "__main__":
    main()
