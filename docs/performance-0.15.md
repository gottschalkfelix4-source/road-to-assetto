# Modellbau und Messwerte in 0.15

Vergleich vom 23. Oktober 2026 gegen Commit
`f7346da4fe70d73a7cf838ab0f1e23392c735d3a` (0.14.0).
Ein bereits heruntergeladener Hessen-Abschnitt wurde mit identischer Konfiguration,
Zufallsfolge, Detailstufe `high` und Vegetationsdichte 0,6 neu aufgebaut.
Die Eingabestrecke einschließlich Überlappung misst 1097,95 m; der zugeschnittene
Bauabschnitt ist ungefähr 1 km lang. Der Abschnitt ist stark bewaldet und enthält
keine Gebäude. Häuser- und Geländealgorithmen wurden nicht vereinfacht.

Die zwei Läufe liefen nacheinander unter Windows/Python 3.12 auf demselben Rechner,
ohne Profiler und ohne parallele Regressionstests. Es handelt sich um je einen
Vergleichslauf, nicht um eine statistische Hardwarebenchmark-Reihe.

| Phase | 0.14 | 0.15 |
|---|---:|---:|
| Modellvorbereitung einschließlich Texturen und Szenendatei | 79,84 s | 54,85 s |
| Zuschnitt des Bauabschnitts | 39,84 s | 27,85 s |
| Summe | 119,68 s | 82,70 s |

Die Einsparung beträgt 30,9 %. Andere Strecken, CPUs, Containerlimits und
Vegetationsanteile können deutlich andere Werte ergeben. Downloads,
Vorschaurendering und ZIP-Erstellung sind in dieser Zeit nicht enthalten.
Der zuvor getrennt ausgeführte Profilervergleich ergab 161,92 → 100,94 s;
wegen des zusätzlichen Messaufwands ist dies nicht die beworbene Beschleunigung.

## Qualitätsnachweis

Beide Läufe erzeugten 185 Meshes, 2.948.723 Vertices und 2.435.089 Dreiecke.
Der kombinierte SHA256 über Namen, Vertices, Flächenindizes, UVs, Normalen,
Materialzuordnung und Kollisionsflags ist identisch:

`f04a3ab387dcdf28d190a6d4c5530cf024002fed4003ec8f1ab1b27f67020a45`

Alle 113 Texturdateien stimmen bytegenau überein. Der optimierte Abschnitt wurde
als 203.376.877 Byte große KN5-Datei exportiert und wieder eingelesen;
Materialreferenzen, Flächenindizes und endliche Geometriewerte sind geprüft.
Das ersetzt keine Fahrprüfung in Assetto Corsa.

`scripts/test_scenery_performance.py` vergleicht zusätzlich hohe und normale
Vegetationsqualität, kleine Sträucher, große Bäume, Zufallsfolge und den Überlauf
eines 63.000-Vertex-Batches gegen Prüfsummen des vorherigen Generators.
Die bestehenden Tests prüfen Zuschnitt, Abschnittsgrenzen und KN5-Serialisierung.

## Anzeige

Die UI misst jeden neuen Bauversuch mit einer monotonen Uhr. Phasenzeiten sind
disjunkt; automatische Wiederholungswartezeit wird separat erfasst. Quellen zeigen
Abrufanzahl, Cache-Zugriffe, empfangene Bytes, HTTP-Headerlatenz, Gesamtzeit und Fehler.
Die Headerlatenz ist keine reine Provider-Warteschlangenmessung: DNS, Verbindung,
Transport und Serverantwort können darin enthalten sein. Eine Serverauslastung
oder eine garantierte Restzeit wird daraus nicht behauptet.

CPU und RAM gehören nur zum Worker und seinen Kindprozessen. CPU wird auf Affinität
und cgroup-v1/v2-Quota normiert. RAM ist die summierte RSS und kann geteilte Seiten
mehrfach zählen. Werte werden sekündlich gespeichert, im Browser beim vorhandenen
Polling aktualisiert und nach sechs Sekunden ohne Aktualisierung als nicht live
gekennzeichnet. Nach Abschluss zeigt die UI Spitzenwerte statt scheinbar laufender
CPU-Nutzung. Die Daten sind über dieselbe Eigentümerprüfung wie der Auftrag geschützt.
Die Messung protokolliert keine URLs, Abfrageparameter, Zugangsdaten oder Antwortinhalte.
