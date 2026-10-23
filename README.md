# Road to Assetto

Ein selbst gehosteter Streckengenerator mit Weboberfläche für **Assetto Corsa**.
Auf der verschiebbaren Straßenkarte lassen sich Orte und Adressen suchen und
Start sowie Ziel für eine eigene Straßenroute in **Hessen** auswählen.
Die vorbereitete, rund 701 m lange **Langstraße in Bad Soden-Altenhain** bleibt
als sofort verfügbare Vorlage mit Teilstrecken ab 120 m enthalten.

Registrierung erfolgt über einmalige Einladungscodes. Jeder Nutzer hat eine
private Streckenbibliothek; auch Vorschauen, Downloads und Aufträge werden
serverseitig auf den Ersteller beschränkt. Das erste Konto übernimmt vorhandene
Aufträge und verwaltet Einladungen sowie den optionalen KI-Anbieter.

Lange Hessen-Routen verwenden automatisch einen lokal zwischengespeicherten
regionalen OSM-Download von Geofabrik. Amtliche Höhendaten, Luftbilder und
Gebäude werden weiterhin abschnittsweise geladen. Abgebrochene Aufträge können
im Browser fortgesetzt werden; passende, geprüfte Zwischenmodelle bleiben
erhalten. Brücken, Tunnel und kreuzende Überführungen erhalten eigene
Kollisionsgeometrie und abgeleitete Höhen. Bauwerksformen sind typisiert,
nicht vermessen; das Spiel muss die Strecke weiterhin praktisch validieren.

![Vorschau der generierten Strecke](docker/preview.png)

## Installation

### Unraid

Die fertige Vorlage und die Installationsschritte stehen in **[unraid/README.md](unraid/README.md)**.
Das Image enthält Blender, die vorbereiteten Altenhain-Geodaten und die Materialien.
Für Aufträge, Einstellungen und Ergebnisse wird nur ein persistenter Appdata-Ordner benötigt.

- Image: `ghcr.io/gottschalkfelix4-source/road-to-assetto:latest`
- Architektur: `linux/amd64`
- Weboberfläche: HTTP auf Containerport **8765**
- Keine GPU oder Assetto-Corsa-Installation auf dem Server erforderlich

### Docker Compose

```sh
git clone https://github.com/gottschalkfelix4-source/road-to-assetto.git
cd road-to-assetto
docker compose pull
docker compose up -d
```

Anschließend [http://localhost:8766](http://localhost:8766) öffnen. Für Zugriff aus dem
Heimnetz eine lokale `.env` mit `ROADTOASSETTO_BIND_IP=0.0.0.0` anlegen.
Details zu Speicher, Updates, Selbstbau und Datenpaket: **[DOCKER.md](DOCKER.md)**.
TLS bleibt bei einem vorhandenen Reverse Proxy. Beim ersten Start den Einrichtungscode
aus `/data/bootstrap-code.txt` verwenden und das Administratorkonto anlegen.
Danach können Freunde mit einem vom Administrator erzeugten Einladungscode ihr
eigenes Konto registrieren. Hinter HTTPS `ROADTOASSETTO_SECURE_COOKIE=1` setzen.

## Strecke erstellen

1. Einen Ort oder eine Adresse suchen, einen Treffer auswählen und Start/Ziel auf
   der Karte setzen. Alternativ die vorbereitete Altenhain-Strecke wählen.
2. Die berechnete Straßenroute prüfen und Name, Straßenbreite sowie Details einstellen.
3. Den Bauplan und die grobe Speicherschätzung prüfen, dann den Auftrag starten.
   Bei langen Routen lädt und verarbeitet der Server die offenen Geodaten
   abschnittsweise; anschließend entstehen das Streckenpaket und die Vorschauen.
4. Das fertige ZIP herunterladen und in Assetto Corsa Content Manager importieren.
5. In **Practice / Freies Training** fahren.

Jeder Auftrag hat eine eigene Track-ID. Er verändert keine vorhandene Strecke und
keine Spiel- oder Lenkradeinstellungen. Die Aufträge werden nacheinander verarbeitet.
Die Vorschau legt eine eigene Route auf dem Server ab. Ein bereits gestarteter
Auftrag behält diese Route, auch wenn danach andere Start-/Zielpunkte gewählt werden.
ZIP-Dateien neuer Routen enthalten unter `provenance/` den OSM-Ausschnitt,
verwendete Straßen-IDs und Abrufnachweise. Das Streckenmanifest dokumentiert
die Quellen und Prüfsummen dieser Dateien.

## Neue Routen und Onlinedienste

Neue Routen beginnen bei **120 m innerhalb Hessens**. Es gibt keine feste
2-km-Grenze; auch Routen über 50 km können ausgewählt werden. Lange Routen werden
in kleinere Bauabschnitte zerlegt, deren Geodaten und Modelle der Server
nacheinander verarbeitet. Das Ergebnis ist **eine zusammenhängende Strecke**:
Das ZIP enthält mehrere Modelldateien und wird nur einmal in Content Manager
installiert. Die gerenderten Vorschauen zeigen bei langen Routen einen
ausgewählten Abschnitt, nicht die gesamte Strecke.

Die Vorschau zeigt die Abschnittsanzahl und eine grobe Speicherschätzung.
Download, freie Festplatte, Bauzeit und Leistung im Spiel bleiben praktische
Grenzen; die Abschnittsverarbeitung ist keine Zusage für beliebig große Exporte
auf jedem Rechner. Die geschätzten Arbeitsdaten enthalten zusätzlich zu den
Quellen auch Platz für Modelle und Verarbeitung. Bereits gespeicherte Aufträge
benötigen daneben weiterhin Speicher.

Start und Ziel müssen höchstens 60 m von einer geeigneten Straße entfernt liegen.
Einbahnrichtungen werden beachtet, private Wege werden abgewiesen. Brücken und
Tunnel erhalten separate Höhenprofile und physische Bauwerksgeometrie. Für neue Routen ist die
eingestellte Straßenbreite ein konstanter Wert zwischen 4 und 8 m. Ein erster
Test mit 200–500 m hilft, Qualität und Leistung des eigenen Servers einzuschätzen.

Für lange Verbindungen nutzt die Routenplanung OSRM auf OpenStreetMap-Daten.
Das Standardprofil berechnet eine Autoroute und **kann Autobahnen enthalten**.
Die Vorschau deshalb vor dem Bau prüfen; die Darstellung einer Route bestätigt
noch keine geprüften Durchfahrtshöhen. Brückenfahrbahnen werden aus DGM-Zufahrten
und robusten DOM1-Beobachtungen abgeleitet, Tunnelfahrbahnen zwischen den Portalen.
Tragplatten, Geländer, Tunnelwände und Decken sind typisierte Nachbildungen.
Unklare Höhenlagen oder zu geringe Durchfahrtshöhen führen weiterhin zu einem
Fehler. `provenance/structures.json` dokumentiert Bauwerke und Schätzmethoden.
Fehlgeschlagene Aufträge können mit **Bau fortsetzen** erneut gestartet werden;
passende, prüfsummenverifizierte Zwischenmodelle und Quelldaten bleiben erhalten. Der öffentliche
[OSRM-Demodienst](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)
ist für gelegentliche, nicht kommerzielle Abfragen vorgesehen. Die Anwendung
begrenzt Anfragen und speichert Ergebnisse zwischen. Für häufige Nutzung einen
eigenen oder geeigneten kompatiblen Dienst konfigurieren; siehe [DOCKER.md](DOCKER.md).
Start- und Zielkoordinaten werden an den Routinganbieter übertragen.

Die neue Suche nutzt standardmäßig den öffentlichen Nominatim-Dienst. **Bitte
die [Nominatim-Nutzungsbedingungen](https://operations.osmfoundation.org/policies/nominatim/)
beachten:** Diese Konfiguration ist für den bewussten, gelegentlichen Einsatz
im privaten Freundeskreis vorgesehen. Die Suche startet nur nach Absenden;
es gibt keine Autovervollständigung. Eine Serverinstanz begrenzt die gemeinsamen
Such- und Ortsprüfungsanfragen auf höchstens eine pro Sekunde und speichert
Antworten sieben Tage. Mehrere Instanzen oder eine öffentliche Nutzung brauchen
eine passende gemeinsame Begrenzung beziehungsweise einen eigenen oder anderen
zulässigen Anbieter. Suchbegriffe und gewählte Koordinaten werden an den
Geodatendienst übertragen; keine vertraulichen Angaben eingeben.

Die Browserkarte lädt nur die sichtbaren OpenStreetMap-Kacheln. Ihre
[Nutzungsbedingungen](https://operations.osmfoundation.org/policies/tiles/)
schließen Massendownloads und Offline-Kachelpakete aus. Die Straßenkarte wird
nicht als Spieltextur exportiert; dafür lädt der Generator die amtlichen DOP20-
Luftbilder. Die öffentlichen Dienste haben keine Verfügbarkeitsgarantie.
Anbieterwechsel und die benötigten Container-Variablen beschreibt [DOCKER.md](DOCKER.md).

## Was der Generator verwendet

- OpenStreetMap-Straßen, amtliches DGM1/DOM1, DOP20 und LoD2-Gebäude aus Hessen.
- Für Altenhain eine am Luftbild nachgeführte Mittelachse mit wechselnder Straßenbreite.
  Dort bezeichnet **100 % / 6 m** das vorbereitete Breitenprofil, keine konstante Breite.
  Neue Routen folgen der berechneten OSM-Straßenachse; ihre Straßenränder werden
  nicht automatisch aus dem Luftbild vermessen.
- Deutsche Straßendetails, dokumentierte Markierungen und Einmündungen,
  Pflasteranschlüsse, Stützmauern, Treppen und geeignete Zufahrten.
- Gemeinsame Geländeplanung und geschlossene Hausunterseiten mit Kellerhüllen und Sockeln.
- Freie Oberflächenmaterialien, generierte Fenster-/Türatlanten und eine zugeordnete
  historische CC0-Fotofassade. Die übrigen Fassadendetails sind abgeleitete Kulisse.
- KN5-Export und Blender-Vorschauen mit derselben generierten Geometrie.

Luftbildkorrekturen, geprüfte Markierungen und die historische Fotofassade gehören
zum vorbereiteten Altenhain-Datensatz. Sie werden nicht auf andere Straßen
übertragen. Neue Routen erhalten eigene amtliche Gelände-, Luftbild- und
Gebäudedaten und die gemeinsamen prozeduralen Bau- und Materialfunktionen.

Die Grundstücksdetails sind **keine Vermessung oder fotografische Rekonstruktion**.
Der Prototyp ist für private Streckentests gedacht; KI erzeugt keine fehlenden
Originalansichten. Alle Quellen und Grenzen sind in **[THIRD_PARTY.md](THIRD_PARTY.md)** dokumentiert.
Google-Street-View-Bilder sind nicht enthalten.

## Optionale KI-Materialien

Anbieter, Images-API-Adresse, Modell und API-Schlüssel können später im Webinterface
unter **KI-Anbieter** eingetragen werden. Ohne Anbieterzugang funktioniert der Generator
mit den mitgelieferten Materialien. Ein ChatGPT-Login ist nicht implementiert.

Die optionale Funktion erzeugt generische Putz-, Dach- und Asphaltmaterialien.
Sie überträgt feste Materialbeschreibungen, keine Geodaten oder Ortsfotos. Anbietergebühren
können anfallen. Schlüssel und Cache bleiben im persistenten `/data`-Volume und gehören
weder in dieses Repository noch in ein Image oder Streckenpaket.

## Entwicklung und Veröffentlichungen

```sh
# Image mit den veröffentlichten, per SHA256 geprüften Geodaten selbst bauen
docker compose build
```

Der Quellcode liegt in `roadtoassetto/`, `webapp/` und `scripts/`. Das versionierte
Datenpaket wird separat als GitHub-Release bereitgestellt; seine URL und Prüfsumme stehen
in `docker/dataset.json`. Aufträge, API-Einstellungen, lokale Programme und große erzeugte
Modelle werden nicht ins Repository übernommen.

GitHub Actions prüft den Code und veröffentlicht das Containerimage für den Hauptbranch
und Versionstags. Die Unraid-Vorlage zeigt auf `latest`; ein Versions-Tag kann für feste
Installationen gewählt werden. Der aktuelle Generatorstand ist **0.14.0**;
das weiterhin enthaltene Altenhain-Datenpaket hat den Stand **0.11.0**.

Validierungsstand für 0.14.0: 293 automatisierte Tests bestanden im Linux-Container
mit 8 GiB RAM und vier CPU-Kernen. Anmeldung, Einladungen und die Trennung privater
Aufträge wurden zusätzlich im Browser geprüft. Kurze echte Brücken- und Tunnelabschnitte
wurden als KN5 und ZIP exportiert. Bei einem im Browser gestarteten 64,8-km-Test
wurden acht von 65 Abschnittsmodellen geprüft; der Lauf wurde auf Nutzerwunsch beendet.
Ein vollständiger Export über 50 km sowie Fahrbarkeit und Bildrate im Spiel sind
damit noch nicht nachgewiesen. Hohe Detailstufen können lange Bauzeiten verursachen.

## Lizenz

Der Projektcode steht unter **[Apache-2.0](LICENSE)**. Die Lizenz gilt nicht pauschal
für mitgelieferte Geodaten und Fremdmaterialien: Diese behalten ihre jeweiligen
Lizenzen und Herkunftsnachweise, insbesondere ODbL, Datenlizenz Deutschland Zero 2.0
und CC0. Siehe [THIRD_PARTY.md](THIRD_PARTY.md).
