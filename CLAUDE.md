# CLAUDE.md – cbpi4-ventil

Gilt zusätzlich zur übergeordneten `~/Claude/CLAUDE.md`. CraftBeerPi4-Plugin, das Brauwasser in den Kessel
dosiert: Magnetventil (NC) am HAT-Ausgang J13 (GPIO 19), Durchflussmesser YF-S201 an GPIO 24 (keine HAT-Buchse).
Ersetzt die Flask-App `app.py` im Repo `Ventilsteuerung` (Zweig `main`, dort Port 5000). Teil des **Brau-Verbunds** von Timm. Gesamtbild in `docs/systeme.md` der Repos `Biersteuerung` und
`BrewPiLess-PCB` (gleiche Datei). Nachbar-Repos liegen lokal nebeneinander unter `~/Claude/`:
`cbpi4-brausicher`, `cbpi4-gaerdruck`, `cbpi4-ventil`, `cbpi4-simulation`, `cbpi4-bme280`, `Biersteuerung` (HAT,
Braubrücke, Plugin `cbpi4-kellersensor`).

## Inhalt

- `VentilDosierer` (Aktor, Handbetrieb mit Mengen- und Zeitgrenze, Aktion „Liter einlassen“), `VentilDurchfluss`
  (Sensor: Menge oder L/min), `WasserDosieren` (Schritt mit Stand in den Props, `Dosierstand`). Typnamen nicht
  umbenennen.
- `regler.py`: Zustandsautomat mit eigenen Threads, ohne cbpi; `LgpioBackend` und `SimBackend`. `pruefen.py`:
  Prüfung am Pi. `systemd/ventil-zu.conf`: Ventil beim Start/Stopp von cbpi zu, `LG_WD=/tmp`.
- `cbpi4-simulation` stellt `regler.b.fluss_lpm` des `SimBackend` und liest dessen Zähler `_n`. Diese Namen nicht
  ändern, ohne die Simulation anzupassen.

## Regeln

- **Echtes Wasser, echtes Ventil.** Nie gegen den Pi testen. Lokal nur mit `Simulation = Ja` bzw. `SimBackend`.
- Jeder Weg, der das Ventil öffnet, braucht eine Mengen- **und** eine Zeitgrenze. Keine stille Attrappe bei
  fehlender Hardware (deshalb lgpio und ein klarer Fehler statt RPi.GPIO).
- Regelung gehört in `regler.py`, nicht in die asyncio-Schleife von cbpi.
- Kein zweiter Aktor im cbpi darf GPIO 19 schalten (`SicherAktor` aus `cbpi4-brausicher` prüft das mit).
- Zweig `main`. Tests: `pip install cbpi4 pytest && python -m pytest tests/ -q`.
- UI-Texte mit Umlauten, Code-Kommentare ohne.

## Fallstricke

- `RPi.GPIO` aus PyPI: `add_event_detect` scheitert unter Kernel 6.6. Das System-Python von Bookworm hat
  `rpi-lgpio`, ein venv nicht.
- cbpi 4.7.7 setzt einen aktiven Schritt nach dem Neustart nicht fort; `WasserDosieren` rechnet trotzdem damit
  (Stand in den Props) und öffnet nach einem Absturz nicht von selbst wieder.
- lgpio legt Hilfsdateien im Arbeitsverzeichnis an → `LG_WD=/tmp`.

## cbpi-4.7.7-Fallen (gemessen, gelten für alle Plugins des Verbunds)

- Ein Sensor darf **nie `None`** liefern: Kessellogik, MashStep und Gärschritte vergleichen ungeprüft. Gültigkeit
  steckt im Alter des Werts (`alter()`).
- Aktor-`get_state()` liefert einen **bool**, kein dict, sonst zeigt die Oberfläche LED/SVG/Leitungen immer „an“.
- Schritte schalten nur mit `asyncio.create_task(self.next())` aus `run()` weiter.
- Kessel- und Gärtanklogik setzen im `finally` von `run()` `self.running = False`. cbpi stoppt sie am Schrittende nur
  über `state` und verweigert sonst den nächsten Start („already running“).
- Der eingebaute „Fermenter Autostart“ startet nichts (ruft `start()` statt `start_logic()`).
- `POST /kettle/{id}/off` liefert HTTP 500 (Methode fehlt), Kessel nur per `toggle` schalten.
- cbpi findet Plugins über `pkgutil.iter_modules`: `pip install -e` wird nicht geladen. Normal installieren oder
  `pip install -e . --config-settings editable_mode=compat`.
- Lokal testen: `web.run_app` bindet 0.0.0.0, also über einen Wrapper mit `host="127.0.0.1"` starten, eigener
  Konfigordner und Port (`cbpi --config-folder-path=… start`, Port in `config.yaml`).
