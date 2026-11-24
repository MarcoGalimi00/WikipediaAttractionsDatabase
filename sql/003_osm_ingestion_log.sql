-- Migrazione 003: log delle corse di load_attractions.py e controllo di coerenza sull'area delle città.
-- Applicata sul progetto Supabase dell'app il 13 novembre 2026 con il nome "create_osm_ingestion_log".
-- Va applicata prima di lanciare load_attractions.py con --apply (senza la tabella lo script funziona lo stesso,
-- ma avvisa che il log non viene scritto).
--
-- Come si legge:
--   osm_ingestion_log   una riga per ogni città e per ogni corsa con --apply: quanti oggetti, con che esito, con quale area
--   osm_ingestion_last  l'ultima riga di ogni città: serve a trovare le città da sistemare (stato 'nessun_oggetto' o 'errore')

-- 1) Il log. La chiave è (città, data e ora della corsa): una città può comparire molte volte.
create table public.osm_ingestion_log (
  city_wikidata text        not null references public.wikidata_cities (wikidata) on delete cascade,
  ingested_at   timestamptz not null default now(),
  status        text        not null check (status in ('ok','nessun_oggetto','errore')),
  n_objects     integer     not null default 0 check (n_objects >= 0),   -- oggetti scritti in osm_objects in questa corsa
  n_with_qid    integer     not null default 0 check (n_with_qid >= 0),  -- di cui con codice Wikidata
  area_used     text        not null default 'qid',                      -- 'qid', 'relation:<id>' oppure 'raggio:<km>km'
  source        text        check (source in ('overpass','cache')),      -- scaricata ora o letta dal file salvato
  error_message text,                                                    -- solo per status = 'errore'
  primary key (city_wikidata, ingested_at)
);

create index osm_ingestion_log_status_idx on public.osm_ingestion_log (status);

comment on table public.osm_ingestion_log is 'Una riga per citta e per corsa di load_attractions.py --apply: esito, numero di oggetti e area usata. Scritta dallo script.';
comment on column public.osm_ingestion_log.status is 'ok = oggetti scritti; nessun_oggetto = Overpass ha risposto ma senza risultati (es. confine non trovato); errore = corsa fallita per la citta.';
comment on column public.osm_ingestion_log.area_used is 'Come e stata scelta l area: qid (confine con il codice della citta), relation:<id> (confine indicato a mano), raggio:<km>km (cerchio).';

-- 2) L'ultima corsa di ogni città, con il nome e i dati utili a decidere come sistemarla.
create view public.osm_ingestion_last with (security_invoker = true) as
select distinct on (l.city_wikidata)
       l.city_wikidata, c.name, c.country_code, c.lat, c.lon, c.population,
       c.area_method, c.relation_id, c.radius_km,
       l.ingested_at, l.status, l.n_objects, l.n_with_qid, l.area_used, l.source, l.error_message
from public.osm_ingestion_log l
join public.wikidata_cities c on c.wikidata = l.city_wikidata
order by l.city_wikidata, l.ingested_at desc;

-- 3) Coerenza dell'area in wikidata_cities: il metodo e i suoi dati si scrivono insieme.
--    Le righe attuali hanno tutte area_method vuoto, quindi il vincolo si applica senza modificare dati.
alter table public.wikidata_cities
  add constraint wikidata_cities_area_check check (
    area_method is null
    or (area_method = 'confine' and relation_id is not null and relation_id > 0)
    or (area_method = 'raggio'  and radius_km  is not null and radius_km  > 0)
  );

-- RLS attiva e nessuna regola, come per le altre tabelle: il loader usa la chiave service_role, che la aggira.
alter table public.osm_ingestion_log enable row level security;
