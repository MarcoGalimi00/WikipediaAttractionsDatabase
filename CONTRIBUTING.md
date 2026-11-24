# Contribuire

Grazie per l'interesse! Il progetto è in fase iniziale: prima di un cambiamento grande, apri una issue.
Lingua del progetto: **italiano** (commenti, messaggi di commit, documentazione).

## Flusso di lavoro

1. Fai un fork o crea un branch: `feature/...`, `fix/...`, `docs/...`.
2. Commit con messaggi in italiano, brevi e descrittivi.
3. Apri una pull request su `main` compilando il modello.

## Regole del codice

- Python standard, commenti e messaggi in italiano.
- **Dry-run per default**: ogni script scrive solo con `--apply`.
- Nessuna chiamata per singolo luogo verso API esterne (usa file bulk o join).
- Un errore su una città non deve fermare le altre; le scritture sono idempotenti (upsert o sostituzione completa).
- Regole del filtro uguali per tutte le città, niente eccezioni per singola città.
- Parametri pubblici (città, indirizzi dei servizi) in `config.toml`; segreti in `.env`.
- **Segreti mai in git**: usa `.env` (ignorato); aggiorna `.env.example` se aggiungi variabili.

## Migrazioni SQL

File numerati in `sql/` (prossimo: `005_...sql`). Il file nel repo deve essere identico a ciò che è applicato al database:
se cambi una funzione già creata, aggiorna anche il file originale. Controlla `git diff --stat` prima del commit.

## Prima di aprire la PR

```bash
python -m py_compile *.py tools/*.py
python refresh_scores.py          # dry-run, deve terminare senza errori (serve .env)
```

Il contesto completo, le decisioni prese e i punti aperti sono in [AGENTS.md](AGENTS.md).
