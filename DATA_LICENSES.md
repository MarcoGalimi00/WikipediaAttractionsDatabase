# Licenze dei dati

Il codice di questo repo è MIT (vedi `LICENSE`). I dati che gli script scaricano o producono seguono le licenze delle fonti.

| Fonte | Uso | Licenza | Obblighi |
| --- | --- | --- | --- |
| [OpenStreetMap](https://www.openstreetmap.org) (via Overpass API) | luoghi, nomi, coordinate, tag | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) | attribuzione «© OpenStreetMap contributors»; i database derivati vanno condivisi con la stessa licenza (share-alike) |
| [Wikidata](https://www.wikidata.org) | identificativi (QID) | [CC0](https://creativecommons.org/publicdomain/zero/1.0/) | nessuno |
| [QRank](https://qrank.toolforge.org) | punteggio di visite Wikimedia | CC0 | nessuno |

**Conseguenza pratica:** il database prodotto contiene dati OSM, quindi è un *Derivative Database* ai sensi dell'ODbL.
Chi lo ridistribuisce deve citare OpenStreetMap e mantenere la stessa licenza. Questa nota non è una consulenza legale.

Gli script usano le API pubbliche rispettandone le regole (User-Agent dichiarato, nessuna chiamata per singolo luogo,
cache locale delle risposte Overpass). Se forki il progetto, imposta il tuo repo in `config.toml` (`[http] contact`).
