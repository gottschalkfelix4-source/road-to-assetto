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
| Volume `roadtoassetto-data` an `/data` | Aufträge, SQLite-Verlauf, Ergebnisse, API-Einstellungen und Cache |
| `/tmp` | Temporäre Dateien im begrenzten tmpfs |

Compose verwendet vier CPU-Kerne, 8 GiB RAM und einen gleichzeitigen Bauauftrag.
Die Grenzen lassen sich in `compose.yaml` anpassen. Vorschauen können je nach CPU
mehrere Minuten brauchen. Platzbedarf wächst mit der Zahl gespeicherter Aufträge;
für den Anfang etwa 20 GiB freien Speicher einplanen.

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

Ein neueres Image aktualisiert den eingebauten Datensatz. Bereits erzeugte Aufträge
und deren Strecken bleiben im persistenten Volume erhalten.

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
herunter und prüft beide mit SHA256. Zur Laufzeit ist für den normalen Streckenbau
kein Download der Geodaten nötig. Das Quellrepository braucht deshalb keine großen
Rasterdateien, installierten Programme oder lokal erzeugten Strecken.

Das Dataset-Release enthält die offenen Quelldaten und Materialien samt
Herkunftsnachweisen. Projektlizenz und Fremdlizenzen stehen in `LICENSE`, `licenses/`
und `THIRD_PARTY.md`. Eigene Datenpakete lassen sich mit
`scripts/build_release_data.py` aus einem vollständig vorbereiteten lokalen Cache
bauen; eine Änderung erfordert eine neue Prüfsumme und einen neuen Imagebuild.
