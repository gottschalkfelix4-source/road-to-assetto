# Dockerbetrieb

Das Image `ghcr.io/gottschalkfelix4-source/road-to-assetto:latest` enthält den
Generator, die Weboberfläche, Blender 4.5.9 und das vorbereitete Altenhain-Datenpaket.
Es unterstützt Linux **amd64** und rendert auf der CPU. Assetto Corsa wird nur auf
dem Rechner benötigt, auf dem die erzeugte Strecke gefahren wird.

Für Unraid: [Vorlage und Anleitung](unraid/README.md).

## Compose starten

```sh
docker compose pull
docker compose up -d
```

Standard: http://127.0.0.1:8766. Das Hostport-Mapping kann in einer lokalen `.env`
geändert werden:

```dotenv
ROADTOASSETTO_BIND_IP=0.0.0.0
ROADTOASSETTO_PORT=8766
```

Dann ist die Oberfläche unter `http://SERVER-IP:8766` erreichbar. Die Anwendung
hat keine eigene Anmeldung; einen externen Zugriff am vorhandenen Proxy schützen.
TLS und Zertifikate verwaltet weiterhin dieser Proxy.

## Persistenz und Ressourcen

| Ort | Inhalt |
| --- | --- |
| `/app/data` im Image | Vorbereitete Geodaten, Quellenmanifeste, Materialien |
| Volume `roadtoassetto-data` an `/data` | Aufträge, gespeicherte Routen, SQLite-Verlauf, Ergebnisse, API-Einstellungen und Cache |
| `/tmp` | Temporäre Dateien im begrenzten tmpfs |

Compose verwendet vier CPU-Kerne, 8 GiB RAM und einen gleichzeitigen Bauauftrag.
Die Grenzen lassen sich in `compose.yaml` anpassen. Vorschauen können je nach CPU
mehrere Minuten brauchen. Platzbedarf wächst mit der Zahl gespeicherter Aufträge;
für erste kurze Tests etwa 20 GiB freien Speicher einplanen. Lange Routen werden
abschnittsweise gebaut, benötigen aber weiterhin Speicher für alle Quellen,
Modelle und das fertige ZIP. Vor einem langen Auftrag die grobe Speicherschätzung
in der Routenvorschau und den tatsächlich freien Platz prüfen. 20 GiB sind
keine ausreichend bemessene Vorgabe für Strecken über 50 km.

Das Image läuft standardmäßig als `10001:10001`. Bei einem Hostordner statt des
benannten Volumes müssen dessen Rechte zum Containerbenutzer passen. Die Unraid-
Vorlage verwendet dafür `99:100` (`nobody:users`). Es gibt keine PUID/PGID-Variablen.
Rootdateisystem und Quelldaten bleiben schreibgeschützt; `/data` ist schreibbar.

Optional kann ein eigenes vorbereitetes Datenverzeichnis zusätzlich schreibgeschützt
nach `/app/data` eingebunden werden. Es ersetzt dann den mitgelieferten Datensatz und
muss dessen vollständige Struktur enthalten; ein leerer Ordner ist nicht ausreichend.

## Aktualisieren und sichern

```sh
docker compose pull
docker compose up -d
docker compose logs --tail=100 roadtoassetto
```

`docker compose down` erhält das Datenvolume. **Kein `down -v` für normale Updates**,
da dies die gespeicherten Aufträge und Einstellungen löschen würde. Vor einem Update
laufende Bauten abschließen lassen. Für eine konsistente Sicherung den Container
stoppen und das gesamte `/data`-Volume sichern. API-Einstellungen in der Sicherung
vertraulich behandeln. Unraid verwendet den konfigurierten Appdata-Ordner.

Ein neueres Image aktualisiert den eingebauten Datensatz. Bereits erzeugte Aufträge,
gespeicherte Routenauswahlen und deren Strecken bleiben im persistenten Volume erhalten.

## Straßenkarte, Suche und neue Hessen-Routen

Neue Routen verwenden ausgehende HTTP(S)-Verbindungen: Nominatim für die
ausdrücklich ausgelöste Ortssuche und Hessen-Prüfung, OSRM für lange
Straßenverbindungen, Overpass für OSM-Straßen und Umgebung sowie die
HVBG-Dienste für DGM1, DOP20 und LoD2-Gebäude. DOM1 wird
zusätzlich geladen, wenn verfügbar. Der Browser lädt die sichtbaren Kartenkacheln
direkt beim konfigurierten Kachelanbieter. Es werden keine Kartenkacheln in die
Assetto-Corsa-Strecke übernommen.

Gespeicherte Routenauswahlen liegen unter `/data/routes`, Such- und OSM-Antworten
sowie vorbereitete Eingaben unter `/data/geo-cache`. Diese Daten gehören zur
Appdata-Sicherung. Ein bereits vorbereiteter Datensatz wird wiederverwendet;
neue Routen benötigen zunächst einen erfolgreichen Download. Bei einem Fehler
bleiben bestehende Aufträge erhalten. Der vorbereitete Altenhain-Datensatz
funktioniert weiterhin ohne diese Downloads.

Die Mindestlänge beträgt 120 m; eine feste maximale Kilometerzahl ist nicht
vorgegeben. Auch Strecken über 50 km werden in kleinere Bauabschnitte aufgeteilt,
deren Quellen und Modelle nacheinander verarbeitet werden. Ein gemeinsames
`models.ini` setzt die Modelldateien zu einer Strecke zusammen. Vorschauen zeigen
einen ausgewählten Abschnitt. Geprüft werden weiterhin höchstens 60 m Abstand
der gewählten Punkte zur Straße, geeignete befahrbare Straßen und vollständige
Geodaten; Brücken und Tunnel ohne unterstützte Geometrie werden abgewiesen.
Die Straßenbreite neuer Routen liegt zwischen 4 und 8 m. Anbietergrenzen,
Speicherplatz, Verarbeitungszeit und Leistung im Spiel sind weiterhin relevant.
Ein Bauabschnitt umfasst normalerweise rund 1 km Strecke plus einen 50-m-
Überlappungsbereich für die Anschlüsse. Pro Datenabruf bleiben die Fläche auf
2 km² und LoD2-Gebäude auf 1.000 begrenzt. Der gesamte Auftrag hat kein starres
30-Minuten-Limit mehr; ohne Fortschritt über 30 Minuten wird die Verarbeitung
abgebrochen. Einzelne Datenabrufe und Renderprozesse haben eigene Zeitgrenzen.

Der öffentliche [OSRM-Demodienst](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)
ist für gelegentliche nicht kommerzielle Nutzung ausgelegt, mit höchstens einer
Anfrage pro Sekunde und ohne Verfügbarkeitsgarantie. Die Anwendung serialisiert
Routinganfragen pro Prozess mit mindestens 1,05 Sekunden Abstand und speichert
Antworten sieben Tage. Start-/Zielkoordinaten gehen an den gewählten Anbieter.
Für viele Exporte, mehrere Instanzen oder verlässlichen Dauerbetrieb einen eigenen
beziehungsweise geeigneten kompatiblen Dienst verwenden. Die vollständigen
[Nutzungsregeln](https://routing.openstreetmap.de/about.html) gelten zusätzlich.

Das Standardprofil liefert eine Autoroute, die Autobahnen enthalten kann.
`ROADTOASSETTO_ROUTER_EXCLUDE` kann bei einem dafür konfigurierten OSRM-Server
zum Beispiel `motorway` ausschließen. Der öffentliche Standarddienst unterstützt
diese Option derzeit nicht. Ein nicht unterstützter Ausschluss führt zu einer
verständlichen Fehlermeldung; die Anwendung umgeht ihn nicht stillschweigend.

**Der öffentliche Nominatim-Dienst ist für diesen gelegentlichen privaten
Einsatz bewusst gewählt. Seine [Nutzungsbedingungen](https://operations.osmfoundation.org/policies/nominatim/)
gelten für alle Nutzer der Anwendung zusammen.** Die Anwendung verwendet eine
gemeinsame Begrenzung von mindestens 1,05 Sekunden zwischen Anfragen pro
Serverprozess, einen identifizierenden User-Agent und sieben Tage Suchcache.
Keine Autovervollständigung, Massenabfragen oder Hintergrundsuche ergänzen.
Für mehrere Instanzen oder mehr Nutzer einen eigenen beziehungsweise geeigneten
Anbieter verwenden. Suchtexte und Start-/Zielkoordinaten werden an diesen Dienst
übertragen.

Anbieter lassen sich ohne Codeänderung über **Container-Umgebungsvariablen**
wechseln. In Compose unter `services.roadtoassetto.environment` ergänzen oder in
Unraid eine zusätzliche Konfiguration vom Typ **Variable** anlegen:

| Variable | Vorgabe / Bedeutung |
| --- | --- |
| `ROADTOASSETTO_GEOCODER_URL` | `https://nominatim.openstreetmap.org` – kompatible Basisadresse ohne `/search` oder `/reverse` |
| `ROADTOASSETTO_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` – kompatibler Overpass-Endpunkt |
| `ROADTOASSETTO_ROUTER_URL` | `https://router.project-osrm.org` – OSRM-kompatible Basisadresse ohne `/route/v1/...` |
| `ROADTOASSETTO_ROUTER_PROFILE` | `driving` – Profilpfad des konfigurierten OSRM-Servers |
| `ROADTOASSETTO_ROUTER_EXCLUDE` | Leer – optionale, kommagetrennte Ausschlüsse, nur wenn vom Routingserver unterstützt |
| `ROADTOASSETTO_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` – Kachel-URL mit den drei Platzhaltern |
| `ROADTOASSETTO_TILE_ATTRIBUTION` | Zusätzlicher Klartext-Quellenhinweis für einen anderen Kachelanbieter |

Danach den Container neu erstellen beziehungsweise in Unraid **Apply** wählen.
Anbieter müssen passende Antworten und Nutzungsrechte bereitstellen; diese
Variablen ändern nicht die auf Hessen begrenzten amtlichen Datenquellen.
Der vorhandene Reverse Proxy muss den für Kartenkacheln nötigen Referrer zulassen
und darf die Content-Security-Policy nicht auf einen alten Kachelanbieter fixieren.
Siehe auch die [OSM-Kachelrichtlinie](https://operations.osmfoundation.org/policies/tiles/).

## KI-Anbieter

Die Einrichtung erfolgt später in der Oberfläche. Einstellungen liegen unter
`/data/ai-settings.json`, generierte Materialien unter `/data/ai-cache`. Es sind
keine API-Schlüssel vorgegeben. `localhost` als Anbieteradresse bezeichnet den
Container; für eine API auf einem anderen Rechner dessen LAN-Adresse verwenden.
Compose richtet zusätzlich `host.docker.internal` für einen Dienst auf dem
Dockerhost ein. Der API-Dienst muss diesen Netzwerkzugriff erlauben.

## Image selbst bauen

```sh
docker compose build --pull
docker compose up -d --no-build
```

Der Build lädt Blender und das in `docker/dataset.json` festgelegte Datenpaket
herunter und prüft beide mit SHA256. Die vorbereitete Altenhain-Strecke braucht
zur Laufzeit keinen Download der Geodaten. Adresssuche, Straßenkarte und neue
Hessen-Routen benötigen dagegen Internetzugriff. Das Quellrepository braucht
keine großen Rasterdateien, installierten Programme oder lokal erzeugten Strecken.

Das Dataset-Release enthält die offenen Quelldaten und Materialien samt
Herkunftsnachweisen. Projektlizenz und Fremdlizenzen stehen in `LICENSE`, `licenses/`
und `THIRD_PARTY.md`. Eigene Datenpakete lassen sich mit
`scripts/build_release_data.py` aus einem vollständig vorbereiteten lokalen Cache
bauen; eine Änderung erfordert eine neue Prüfsumme und einen neuen Imagebuild.
