# Road to Assetto auf Unraid

Die Vorlage startet den Browser-Generator mit dem vorbereiteten
**Altenhain–Langstraße-Datensatz**. Das Image enthält Blender, Geodaten und
Materialien. Ein zusätzlicher Daten-Download oder Quellcode-Checkout auf Unraid
ist nicht nötig. Eine freie Ortswahl gehört noch nicht zum Prototyp.

- Image: `ghcr.io/gottschalkfelix4-source/road-to-assetto:latest`
- [Unraid-Vorlage herunterladen](https://raw.githubusercontent.com/gottschalkfelix4-source/road-to-assetto/main/unraid/roadtoassetto.xml)
- [Quellcode und Fehlerberichte](https://github.com/gottschalkfelix4-source/road-to-assetto)

Die XML-Datei lässt sich direkt installieren. Eine Aufnahme in den
Community-Applications-Katalog wird damit nicht vorausgesetzt.

## Voraussetzungen

Unraid mit aktiviertem Docker und einer x86-64-CPU (`amd64`). Vorschauen werden
auf der CPU gerendert; eine GPU und eine Assetto-Corsa-Installation auf dem Server
sind nicht erforderlich. Die Vorlage begrenzt den Container auf vier CPU-Kerne
und 8 GiB RAM. Für Image und erste Testaufträge etwa 20 GiB freien Speicher
einplanen; gespeicherte Aufträge belegen zusätzlich Platz. Ein SSD-Pool für
Appdata verkürzt die Lade- und Schreibzeiten.

## Vorlage installieren

Die folgenden Befehle im **Unraid-Terminal** ausführen. Sie legen nur die
Vorlage und bei Bedarf einen leeren Appdata-Ordner an. Eine vorhandene
Benutzervorlage wird nicht überschrieben.

```sh
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl --fail --location --retry 3 \
  https://raw.githubusercontent.com/gottschalkfelix4-source/road-to-assetto/main/unraid/roadtoassetto.xml \
  --output /tmp/roadtoassetto-template.xml
```

Nach erfolgreichem Download:

```sh
if [ ! -e /boot/config/plugins/dockerMan/templates-user/my-roadtoassetto.xml ]; then
  cp /tmp/roadtoassetto-template.xml /boot/config/plugins/dockerMan/templates-user/my-roadtoassetto.xml
else
  echo "Vorlage existiert bereits; bestehende Einstellungen bleiben erhalten."
fi
if [ ! -e /mnt/user/appdata/roadtoassetto ]; then
  install -d -o 99 -g 100 -m 0770 /mnt/user/appdata/roadtoassetto
fi
```

1. In Unraid **Docker → Add Container** öffnen.
2. Im Feld **Template** die Benutzervorlage **roadtoassetto** wählen.
3. Host-Port und Appdata-Pfad prüfen, anschließend **Apply** wählen.
4. Nach dem Start über das Containersymbol **WebUI** öffnen, standardmäßig
   unter `http://UNRAID-IP:8765/`.
5. Für einen ersten Test im Browser einen kurzen Abschnitt der Langstraße
   erzeugen und danach Vorschauen und Strecken-ZIP öffnen.

Für den Start nach einem Server-Neustart kann im Docker-Tab zusätzlich
**Autostart** aktiviert werden. Die Vorlage ist ein normales Unraid-XML-Template;
ein Compose-Plugin wird nicht benötigt.

## Einstellungen

| Feld | Vorgabe | Bedeutung |
| --- | --- | --- |
| Repository | `ghcr.io/gottschalkfelix4-source/road-to-assetto:latest` | Öffentliches Image mit vorbereitetem Datensatz |
| Netzwerk | `bridge` | HTTP über die Unraid-IP |
| Weboberfläche | Host `8765` → Container `8765/tcp` | Nur den Host-Port ändern, wenn dieser belegt ist |
| Appdata | `/mnt/user/appdata/roadtoassetto` → `/data` | Aufträge, Ergebnisse und Einstellungen |
| Benutzer | `--user=99:100` | Dateibesitz passend zu Unraids `nobody:users` |
| Ressourcen | `--cpus=4 --memory=8g` | In der erweiterten Ansicht unter Extra Parameters anpassbar |

Die Quelldaten liegen im Image unter `/app/data`. Diesen Pfad nicht mit einem
leeren Hostordner überdecken. Schreibzugriffe erfolgen in `/data` und im
temporären `/tmp`. Das übrige Dateisystem bleibt schreibgeschützt. Es werden
weder privilegierter Betrieb noch Docker-Socket, GPU oder Hostgeräte benötigt.

Die Benutzer-ID wird über Docker gesetzt; das Image wertet keine
`PUID`-/`PGID`-Variablen aus. Bei einem eigenen Appdata-Pfad diesen vor dem
ersten Start für UID 99/GID 100 anlegen. Ein zuvor unter UID 10001 betriebenes
Datenverzeichnis braucht eine Anpassung des Eigentümers oder ein dazu passendes
`--user`-Argument. Vor einer solchen Migration den Container stoppen und den
vorhandenen Appdata-Ordner sichern.

## Optionale KI und Reverse Proxy

Der Generator funktioniert mit den enthaltenen Materialien ohne API-Schlüssel.
Anbieter, URL, Modell und Schlüssel können später in den KI-Einstellungen der
Weboberfläche eingetragen werden. Die Einstellungen liegen im Appdata-Ordner
unter `ai-settings.json`; API-Schlüssel gehören nicht in die XML-Vorlage.

Für einen Bilddienst auf einem anderen Rechner dessen LAN-Adresse verwenden.
Ein Dienst auf dem Unraid-Host kann über `host.docker.internal` angesprochen
werden; die Vorlage richtet die Host-Gateway-Zuordnung ein. Der Bilddienst muss
Verbindungen aus dem Docker-Netz annehmen. `localhost` bezeichnet im Container
den Container selbst.

Der bestehende Reverse Proxy kann HTTP an `UNRAID-IP:8765` weiterreichen.
TLS und Zertifikate bleiben beim Proxy. Die Anwendung hat keine eigene
Benutzeranmeldung; der Zugang ist für ein vertrautes Netz beziehungsweise einen
entsprechend geschützten Proxy vorgesehen.

## Updates, Sicherung und Diagnose

Updates über **Docker → Check for Updates → Update** installieren. Der
Appdata-Pfad bleibt dabei gleich. Für eine feste Version im Repository-Feld
statt `:latest` einen tatsächlich veröffentlichten Versionstag wählen.
Die gespeicherte Benutzervorlage nicht bei jedem Update neu herunterladen,
damit Port und eigene Pfade erhalten bleiben.

Für eine vollständige Sicherung den Container stoppen und den gesamten
Appdata-Ordner sowie die Benutzervorlage sichern. Appdata enthält gegebenenfalls
auch den selbst eingetragenen API-Schlüssel. Das Entfernen oder Neuerstellen des
Containers löscht die separat gespeicherten Aufträge nicht.

```sh
docker logs --tail 100 roadtoassetto
docker inspect --format '{{.State.Health.Status}}' roadtoassetto
curl --fail http://127.0.0.1:8765/api/health
```

Bei einem anderen Host-Port die letzte Adresse anpassen. `healthy` bestätigt
den HTTP-Dienst. Einen vollständigen Funktionstest liefert erst ein im Browser
erzeugter Auftrag mit fertiger ZIP-Datei und Vorschauen. Meldet das Startprotokoll
einen nicht beschreibbaren Datenordner, Eigentümer und Rechte des ausgewählten
Appdata-Pfades prüfen. Nach einem normalen Containerstopp bleiben Ergebnisse
erhalten; ein unterbrochener laufender Auftrag wird beim nächsten Start als
fehlgeschlagen angezeigt und kann neu angelegt werden.

## Format der Vorlage

Die Datei verwendet das Docker-Templateformat `Container version="2"` mit
einem Port- und einem Pfad-Eintrag. Grundlage sind die offizielle
[Unraid-Template-Dokumentation](https://ca.unraid.net/submit/help/repository-xml),
der [XML-Feldkatalog](https://ca.unraid.net/submit/help/xml-field-reference)
und die [Unraid-Dokumentation zu gespeicherten Benutzervorlagen](https://docs.unraid.net/unraid-os/manual/applications/).
