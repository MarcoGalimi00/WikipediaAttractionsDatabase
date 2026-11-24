-- Migrazione 001: anagrafica delle città turistiche (database attrazioni).
-- Applicata sul progetto Supabase dell'app il 10 novembre 2026 con il nome "create_wikidata_cities".
-- Il nome "cities" è già usato dall'app (voli e aeroporti): per questo la tabella si chiama wikidata_cities.

create table public.wikidata_cities (
  id             bigint generated always as identity primary key,
  wikidata       text not null unique check (wikidata ~ '^Q[0-9]+$'),   -- chiave naturale: QID Wikidata
  name           text not null,                                          -- colonna "city" del CSV
  country_code   text not null check (country_code ~ '^[A-Z]{2}$'),
  wikidata_label text,
  lat            double precision,
  lon            double precision,
  population     bigint,
  n_languages    integer,
  types          text[] not null default '{}',                           -- colonna "types" del CSV, separata in elenco
  check_status   text not null check (check_status in ('ok','override','verifica','nessuna','errore')),  -- colonna "check" del CSV ("check" è parola riservata)
  check_auto     text check (check_auto in ('ok','verifica','nessuna','errore')),   -- cosa avrebbe deciso lo script da solo
  override_note  text,                                                   -- nota di seed/override_qid.csv
  area_method    text check (area_method in ('confine','raggio')),       -- vuoto finché non si decide confine o cerchio
  relation_id    bigint,
  radius_km      numeric,
  app_city_id    bigint references public.cities (id) on delete set null, -- legame facoltativo con la tabella cities dell'app
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.wikidata_cities is 'Anagrafica delle citta turistiche per il database attrazioni; una riga per codice Wikidata. Scritta da attractions-pipeline/build_cities.py.';
comment on column public.wikidata_cities.check_status is 'Valore finale di check: ok, override (scelta manuale da seed/override_qid.csv), verifica, nessuna, errore.';
comment on column public.wikidata_cities.check_auto is 'Cosa avrebbe deciso lo script da solo, indipendentemente dall override.';
comment on column public.wikidata_cities.app_city_id is 'Collegamento facoltativo alla tabella cities dell app (voli e aeroporti).';

create index wikidata_cities_country_idx on public.wikidata_cities (country_code);
create index wikidata_cities_app_city_idx on public.wikidata_cities (app_city_id);

-- RLS attiva e nessuna regola: l'accesso con chiave pubblica è bloccato; il loader usa la chiave service_role, che aggira le regole.
alter table public.wikidata_cities enable row level security;
