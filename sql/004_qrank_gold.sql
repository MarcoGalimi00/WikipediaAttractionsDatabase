-- Migrazione 004: punteggio QRank e livello gold delle attrazioni.
-- Applicata sul progetto Supabase (verificato il 17 novembre 2026: wikidata_qrank, le due viste materializzate e i loro indici presenti).
--
-- Come si legge (da sinistra a destra):
--   osm_objects          bronze: tutti gli oggetti scaricati (invariata, sql/002)
--   osm_objects_noise    gli scartati col motivo (invariata, sql/002): vista semplice, serve solo a controllare le regole
--   wikidata_qrank       NUOVA tabella: visite delle ultime 52 settimane per QID, da QRank (scritta da refresh_scores.py)
--   osm_objects_clean    ora VISTA MATERIALIZZATA, gold: senza rumore, solo oggetti con QID, una riga per (citta, QID)
--   osm_objects_scored   ora VISTA MATERIALIZZATA: gold + colonna qrank. È quello che legge l'Itinerario AI
--
-- Chi aggiorna che cosa (refresh_scores.py):
--   dopo il caricamento di una città   refresh clean -> ricarica wikidata_qrank -> refresh scored   (--clean)
--   aggiornamento periodico di QRank   ricarica wikidata_qrank -> refresh scored
-- I refresh sono CONCURRENTLY: chi legge durante il refresh vede la versione precedente, intera.
--
-- Cosa cambia rispetto a sql/002:
--   - gli oggetti SENZA QID escono dal gold (restano in osm_objects): non possono avere un punteggio
--   - lo stesso luogo in più copie OSM (relation + way + node) diventa una riga: vince la relation, poi la way, poi il node
--   - dup_qid diventa n_copie (quante copie OSM aveva il luogo in quella città)
--   - la colonna views (da wikidata_pageviews) è sostituita da qrank. La tabella wikidata_pageviews resta, vuota e non usata.

-- 1) Punteggi QRank: una riga per QID. Dato grezzo, sostituito per intero a ogni caricamento.
create table public.wikidata_qrank (
  wikidata       text   primary key check (wikidata ~ '^Q[0-9]+$'),
  qrank          bigint not null check (qrank >= 1),   -- visite delle ultime 52 settimane, tutti i progetti Wikimedia
  source_version date   not null,                      -- "version" di datapackage.json di QRank
  fetched_at     timestamptz not null default now()
);

comment on table public.wikidata_qrank is 'Visite Wikimedia delle ultime 52 settimane per QID, da QRank (qrank.toolforge.org, CC0). Solo i QID di osm_objects_clean. Scritta da refresh_scores.py.';

-- 2) Le viste attuali si tolgono: una vista semplice non può diventare materializzata con lo stesso nome.
--    Senza CASCADE: se qualcos'altro dipende da queste viste, la migrazione si ferma invece di cancellarlo.
drop view public.osm_objects_scored;
drop view public.osm_objects_clean;

-- 3) Gold: record tenuti, con QID, una riga per (città, QID). Stesse colonne di prima più n_copie.
create materialized view public.osm_objects_clean as
select distinct on (o.city_wikidata, o.wikidata)
  o.osm_type, o.osm_id, o.city_wikidata,
  c.country_code,                                   -- nazione della città (da wikidata_cities), sempre compilata
  o.lat, o.lon, o.name, o.wikidata,
  count(*) over (partition by o.city_wikidata, o.wikidata) as n_copie,  -- copie OSM dello stesso luogo in questa città
  o.tags->>'wikipedia'            as wikipedia,
  o.tags->>'wikimedia_commons'    as wikimedia_commons,
  o.tags->>'subject:wikidata'     as subject_wikidata,
  o.tags->>'subject:wikipedia'    as subject_wikipedia,
  -- che cosa è
  o.tags->>'tourism'              as tourism,
  o.tags->>'historic'             as historic,
  o.tags->>'amenity'              as amenity,
  o.tags->>'leisure'              as leisure,
  o.tags->>'man_made'             as man_made,
  o.tags->>'building'             as building,
  o.tags->>'natural'              as "natural",
  o.tags->>'boundary'             as boundary,
  -- sottotipi
  o.tags->>'artwork_type'         as artwork_type,
  o.tags->>'archaeological_site'  as archaeological_site,
  o.tags->>'memorial'             as memorial,
  o.tags->>'ruins'                as ruins,
  o.tags->>'tomb'                 as tomb,
  o.tags->>'museum'               as museum,
  o.tags->>'basilica'             as basilica,
  o.tags->>'tower:type'           as tower_type,
  o.tags->>'religion'             as religion,
  o.tags->>'historic:civilization' as historic_civilization,
  -- descrizione
  o.tags->>'alt_name'             as alt_name,
  o.tags->>'old_name'             as old_name,
  o.tags->>'official_name'        as official_name,
  o.tags->>'description'          as description,
  o.tags->>'period'               as period,
  o.tags->>'architect'            as architect,
  o.tags->>'artist_name'          as artist_name,
  -- informazioni pratiche
  o.tags->>'website'              as website,
  o.tags->>'contact:website'      as contact_website,
  o.tags->>'phone'                as phone,
  o.tags->>'email'                as email,
  o.tags->>'opening_hours'        as opening_hours,
  o.tags->>'fee'                  as fee,
  o.tags->>'wheelchair'           as wheelchair,
  o.tags->>'access'               as access,
  o.tags->>'addr:street'          as addr_street,
  o.tags->>'addr:housenumber'     as addr_housenumber,
  o.tags->>'addr:city'            as addr_city,
  o.tags->>'addr:postcode'        as addr_postcode,
  o.tags->>'addr:country'         as addr_country
from public.osm_objects o
join public.wikidata_cities c on c.wikidata = o.city_wikidata
where o.wikidata is not null
  and public.osm_noise_reason(o.tags) is null
order by o.city_wikidata, o.wikidata,
         case o.osm_type when 'relation' then 1 when 'way' then 2 else 3 end,  -- quale copia tenere
         o.osm_id;

-- L'indice unico serve a REFRESH ... CONCURRENTLY (senza, il refresh blocca le letture).
create unique index osm_objects_clean_pk on public.osm_objects_clean (city_wikidata, wikidata);

comment on materialized view public.osm_objects_clean is 'Gold: oggetti OSM tenuti (senza rumore), solo con QID, una riga per (citta, QID). Si aggiorna con refresh_scores.py --clean dopo il caricamento di una citta.';

-- 4) Gold + punteggio: quello che legge l'Itinerario AI. Il refresh fa solo il join, è leggero.
create materialized view public.osm_objects_scored as
select c.*,
       q.qrank,                                     -- NULL = nessuna visita registrata in QRank
       q.source_version as qrank_version            -- da quale versione di QRank viene il numero
from public.osm_objects_clean c
left join public.wikidata_qrank q on q.wikidata = c.wikidata;

create unique index osm_objects_scored_pk   on public.osm_objects_scored (city_wikidata, wikidata);
create index        osm_objects_scored_rank on public.osm_objects_scored (city_wikidata, qrank desc nulls last);  -- "le più famose di una città"

comment on materialized view public.osm_objects_scored is 'Gold + qrank: una riga per (citta, QID) con il punteggio QRank. Letta dall Itinerario AI. Si aggiorna con refresh_scores.py.';

-- 5) Permessi. Le viste materializzate NON hanno RLS: con i permessi di default di Supabase sarebbero leggibili
--    da chiunque tramite le API (ruoli anon e authenticated). Si tolgono, come per le tabelle (RLS senza regole).
--    Il permesso di lettura per l'app si decide a parte (Punti aperti, PA-39).
revoke all on public.osm_objects_clean  from anon, authenticated;
revoke all on public.osm_objects_scored from anon, authenticated;

alter table public.wikidata_qrank enable row level security;
