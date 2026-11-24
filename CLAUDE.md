# CLAUDE.md: pipeline delle attrazioni

@AGENTS.md

## In più, per Claude Code

- **Permessi sul database**: l'utente vuole vedere l'SQL e dare un «sì» prima di qualunque modifica a Supabase
  (migrazioni, funzioni, refresh, script con `--apply`). Proponi, non eseguire. Le query di sola lettura sono libere.
- **Come procedere su una modifica al filtro o alle viste** (es. PA-58):
  1. query di sola lettura per misurare l'effetto (quante righe cambierebbero, per città e per valore del tag);
  2. modifica in un nuovo file `sql/005_...sql` (e aggiorna la definizione in `sql/002_osm_objects.sql` se cambi `osm_noise_reason`);
  3. prova su un Postgres locale con dati finti (vedi AGENTS.md §7), se possibile;
  4. mostra all'utente SQL e comando di refresh (`python refresh_scores.py --apply --clean`), aspetta il «sì»;
  5. dopo l'applicazione: ricontrolla le prime 20 posizioni di alcune città (Roma `Q220`, Tokyo, Londra, Sydney).
- **Verifica prima di dichiarare fatto**: `python -m py_compile <script>` e almeno un dry-run (senza `--apply`) per ogni script toccato.
- **Non lanciare** `load_attractions.py` su tutte le città senza chiederlo: usa Overpass pubblico, lento e con limiti.
- **Non scrivere** `connection_hint()` in `refresh_scores.py` salvo richiesta: è un esercizio lasciato all'utente.
- A fine lavoro, nel riepilogo: che cosa è cambiato, che cosa è stato applicato sul database (o resta da applicare),
  e quali note del vault Obsidian (fuori dal repo) vanno aggiornate.
