# Sicurezza

## Segnalare una vulnerabilità

Non aprire una issue pubblica. Usa la funzione **Report a vulnerability** della scheda *Security* del repo su GitHub.

## Segreti

Il progetto usa chiavi Supabase (`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`) e credenziali SMTP.
Stanno solo in `.env`, che è ignorato da git. Se una chiave finisce per sbaglio in un commit, **revocala subito**
dal pannello del fornitore: riscrivere la cronologia non basta.
