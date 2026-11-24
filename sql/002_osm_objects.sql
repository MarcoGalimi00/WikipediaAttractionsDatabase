-- Migrazione 002: oggetti OSM, pageviews Wikipedia e viste. Applicata sul progetto Supabase il 12 novembre 2026 con il nome "create_osm_objects".
--
-- Come si legge (da sinistra a destra, ogni passo si appoggia al precedente):
--   osm_objects          il magazzino: TUTTI gli oggetti scaricati, con i tag originali
--   osm_objects_noise    i record SCARTATI come rumore, col motivo (per controllare cosa si butta)
--   osm_objects_clean    i record TENUTI, con solo le colonne validate (estratte da tags)
--   osm_objects_scored   i record tenuti + le visite Wikipedia (lo score vero lo calcoliamo dopo)
-- Le regole del rumore stanno in UN solo punto: la funzione osm_noise_reason.

-- 1) Il magazzino: una riga per oggetto OSM e per città.
create table public.osm_objects (
  osm_type      text   not null check (osm_type in ('node','way','relation')),
  osm_id        bigint not null,
  city_wikidata text   not null references public.wikidata_cities (wikidata) on delete cascade,
  lat           double precision,
  lon           double precision,
  name          text   not null,                                   -- gli oggetti senza nome non si caricano
  wikidata      text   check (wikidata ~ '^Q[0-9]+$'),             -- QID dell'oggetto (chiave verso le pageviews), nullo se OSM non lo ha
  tags          jsonb  not null,                                   -- tutti i tag OSM originali (senza traduzioni name:xx)
  created_at    timestamptz not null default now(),
  primary key (osm_type, osm_id, city_wikidata)                    -- un oggetto al confine può ricadere in due città
);

create index osm_objects_city_idx     on public.osm_objects (city_wikidata);
create index osm_objects_wikidata_idx on public.osm_objects (wikidata) where wikidata is not null;

comment on table public.osm_objects is 'Magazzino: tutti gli oggetti OSM scaricati con Overpass per le citta di wikidata_cities. Una riga per (osm_type, osm_id, citta). Scritta da load_attractions.py.';

-- 2) La regola del rumore, in un punto solo. Restituisce il motivo dello scarto, oppure NULL se il record si tiene.
--    Le regole si valutano in ordine: vale la prima che scatta. Le modifiche si fanno qui e valgono per tutte le viste.
create function public.osm_noise_reason(t jsonb) returns text
language sql immutable set search_path = ''
as $$
  select case
    when t->>'highway' is not null or t->>'railway' is not null
         or t->>'public_transport' is not null or t->>'type' = 'route'       then 'trasporti'
    when t->>'tourism' in ('hotel','guest_house','hostel','apartment','motel','chalet',
                           'camp_site','caravan_site','information','picnic_site') then 'alloggi_info'
    when t->>'diplomatic' is not null or t->>'embassy' is not null           then 'ambasciate'
    when t->>'office' is not null and t->>'tourism' is null and t->>'historic' is null then 'uffici'  -- il Senato (anche palazzo storico) resta
  end
$$;

-- 3) I record SCARTATI, col motivo. Servono per controllare che il filtro non butti cose buone.
create view public.osm_objects_noise with (security_invoker = true) as
select osm_type, osm_id, city_wikidata, name, wikidata,
       public.osm_noise_reason(tags) as noise_reason,
       tags->>'tourism' as tourism, tags->>'historic' as historic, tags->>'amenity' as amenity,
       tags
from public.osm_objects
where public.osm_noise_reason(tags) is not null;

-- 4) I record TENUTI, con le sole colonne validate. I nomi con i due punti diventano con il trattino basso.
--    country_code è la nazione della città; addr_country è quella scritta in OSM sull'oggetto (spesso vuota).
create view public.osm_objects_clean with (security_invoker = true) as
select
  o.osm_type, o.osm_id, o.city_wikidata,
  c.country_code,                                   -- nazione della città (da wikidata_cities), sempre compilata
  o.lat, o.lon, o.name, o.wikidata,
  tags->>'wikipedia'            as wikipedia,
  tags->>'wikimedia_commons'    as wikimedia_commons,
  tags->>'subject:wikidata'     as subject_wikidata,
  tags->>'subject:wikipedia'    as subject_wikipedia,
  -- che cosa è
  tags->>'tourism'              as tourism,
  tags->>'historic'             as historic,
  tags->>'amenity'              as amenity,
  tags->>'leisure'              as leisure,
  tags->>'man_made'             as man_made,
  tags->>'building'             as building,
  tags->>'natural'              as "natural",
  tags->>'boundary'             as boundary,
  -- sottotipi
  tags->>'artwork_type'         as artwork_type,
  tags->>'archaeological_site'  as archaeological_site,
  tags->>'memorial'             as memorial,
  tags->>'ruins'                as ruins,
  tags->>'tomb'                 as tomb,
  tags->>'museum'               as museum,
  tags->>'basilica'             as basilica,
  tags->>'tower:type'           as tower_type,
  tags->>'religion'             as religion,
  tags->>'historic:civilization' as historic_civilization,
  -- descrizione
  tags->>'alt_name'             as alt_name,
  tags->>'old_name'             as old_name,
  tags->>'official_name'        as official_name,
  tags->>'description'          as description,
  tags->>'period'               as period,
  tags->>'architect'            as architect,
  tags->>'artist_name'          as artist_name,
  -- informazioni pratiche
  tags->>'website'              as website,
  tags->>'contact:website'      as contact_website,
  tags->>'phone'                as phone,
  tags->>'email'                as email,
  tags->>'opening_hours'        as opening_hours,
  tags->>'fee'                  as fee,
  tags->>'wheelchair'           as wheelchair,
  tags->>'access'               as access,
  tags->>'addr:street'          as addr_street,
  tags->>'addr:housenumber'     as addr_housenumber,
  tags->>'addr:city'            as addr_city,
  tags->>'addr:postcode'        as addr_postcode,
  tags->>'addr:country'         as addr_country
from public.osm_objects o
join public.wikidata_cities c on c.wikidata = o.city_wikidata
where public.osm_noise_reason(o.tags) is null;

-- 5) Visite Wikipedia grezze: una riga per QID, lingua e periodo. Lo score NON si salva: si calcola nella vista.
create table public.wikidata_pageviews (
  wikidata     text   not null check (wikidata ~ '^Q[0-9]+$'),
  lang         text   not null,                                    -- es. 'it', 'en'
  article      text   not null,                                    -- titolo dell'articolo in quella lingua
  period_start date   not null,
  period_end   date   not null,
  views        bigint not null check (views >= 0),
  fetched_at   timestamptz not null default now(),
  primary key (wikidata, lang, period_end)
);

create index wikidata_pageviews_period_idx on public.wikidata_pageviews (period_end);

comment on table public.wikidata_pageviews is 'Visite Wikipedia per QID, lingua e periodo (solo traffico utente). Dato grezzo: lo score si calcola nella vista osm_objects_scored.';

-- 6) I record tenuti con le visite dell'ultimo periodo disponibile (somma su tutte le lingue).
--    dup_qid > 1: lo stesso luogo compare piu volte in OSM (es. relation + way). Vale NULL se l'oggetto non ha QID.
create view public.osm_objects_scored with (security_invoker = true) as
with ultimo as (select max(period_end) as period_end from public.wikidata_pageviews),
visite as (
  select p.wikidata, sum(p.views) as views
  from public.wikidata_pageviews p join ultimo u on p.period_end = u.period_end
  group by p.wikidata
)
select c.*,
       v.views,
       case when c.wikidata is not null
            then count(*) over (partition by c.city_wikidata, c.wikidata) end as dup_qid
from public.osm_objects_clean c
left join visite v on v.wikidata = c.wikidata;

-- RLS attiva e nessuna regola, come per wikidata_cities: il loader usa la chiave service_role, che la aggira.
alter table public.osm_objects        enable row level security;
alter table public.wikidata_pageviews enable row level security;
