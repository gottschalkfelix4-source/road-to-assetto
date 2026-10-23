# Generatorwerkzeuge

Der normale Einstieg ist das Webinterface beziehungsweise Docker/Unraid.
Die Python-Skripte sind Entwicklungs- und Diagnosewerkzeuge und teilweise auf
einen bereits vorbereiteten lokalen Datensatz angewiesen.

- `build.py`: lokalen vorbereiteten Datensatz in eine Strecke umwandeln.
- `build_release_data.py`: das separat verteilte, geprüfte Quelldatenpaket bauen.
- `blender_source.py`: Blender-Quelldatei und Vorschauen aus einer generierten Szene.
- `fetch_*.py`: Quelldaten und Materialien beschaffen; kein universeller Ortsgenerator.
- `verify_*.py` und `validate.py`: vorhandene Exporte und Geometrie prüfen.
- `register_local_facades_v06.py`: historischer Kalibrierungsschritt, benötigt die
  ursprünglichen Forschungsbilder. Das Datenpaket enthält bereits die registrierte Fassade.

**`test_session.py` ist kein Unit-Test.** Es bereitet ausdrücklich angeforderte
lokale Assetto-Corsa-Tests vor oder stellt die gesicherten Spieleinstellungen wieder
her. `install.py`, `physics_smoke.py` und `record_load_test.py` betreffen ebenfalls
eine lokale Spielinstallation. Sie werden weder vom Webgenerator noch von der CI gestartet.

Deshalb keine pauschale Discovery aller `test_*.py` ausführen. Die explizite Liste
der isolierten Unit-Tests steht in `.github/workflows/container.yml`.
Docker prüft diese in einem temporären Testverzeichnis. KI-Vertragstests verwenden
simulierte lokale APIs und benötigen keinen API-Schlüssel.

Einige ältere Diagnosebefehle erwarten komplette Szenen, Spielprotokolle oder
Prüfbilder unter `test-results/`. Diese lokalen Artefakte gehören nicht ins
öffentliche Repository. Die Herkunft der verteilten Geodaten und Materialien
ist in `THIRD_PARTY.md` und den Metadaten im separaten Datenpaket dokumentiert.
