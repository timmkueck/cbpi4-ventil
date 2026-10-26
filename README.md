# cbpi4-ventil – Brauwasser in CraftBeerPi4 abmessen

Plugin für CraftBeerPi4. Es öffnet das Magnetventil, zählt die Impulse des Durchflussmessers und
schließt bei der eingestellten Menge. Damit ersetzt es die eigenständige Flask-App `app.py`
im Repo [Ventilsteuerung](https://github.com/timmkueck/Ventilsteuerung) (Zweig `main`), die bisher neben CraftBeerPi lief.

| In CraftBeerPi | Typ | Zweck |
|---|---|---|
| Aktor | **VentilDosierer** | besitzt Ventil und Durchflussmesser. Einschalten = Handbetrieb mit Zeit- und Mengengrenze. Aktion **„Liter einlassen“** dosiert eine Menge von Hand (z. B. aus dem Dashboard) |
| Sensor | **VentilDurchfluss** | zeigt die Menge (L) oder den Durchfluss (L/min) |
| Schritt | **WasserDosieren** | „x Liter in den Kessel“: öffnen, zählen, schließen, weiterschalten |

## Warum es früher nicht lief – und was jetzt anders ist

| Ursache | Jetzt |
|---|---|
| CraftBeerPi installiert `RPi.GPIO` aus PyPI. Das kann unter Bookworm (Kernel 6.6) keine Flanken mehr erkennen: `add_event_detect` scheitert mit „Failed to add edge detection“. Die Flask-App lief, weil das System-Python von Bookworm stattdessen `rpi-lgpio` mitbringt. | Das Plugin nutzt **lgpio** direkt, also die Kernel-Schnittstelle `/dev/gpiochip0`. Die Impulse zählt die Kernel-Ereignisschnittstelle, entprellt mit 200 µs. |
| Fehlt `RPi.GPIO`, ersetzt CraftBeerPi es **still durch eine Attrappe**. Dann zählt nichts, und kein Fehler erscheint. | Fehlt lgpio, meldet der Aktor „Ventil nicht bereit: Python-Modul lgpio fehlt“ und öffnet nie. Eine Attrappe gibt es nur, wenn du ausdrücklich „Simulation = Ja“ einstellst. |
| `pip install -e` (Entwicklerinstallation): CraftBeerPi findet Plugins über `pkgutil.iter_modules` und übersieht solche Installationen. | Normal installieren (siehe unten). Im Log steht dann `Plugin cbpi4_ventil loaded successfully`. |

## Sicherheit

Grundsatz: Im Zweifel ist das Ventil zu. Jeder Weg, der es öffnet, hat eine Mengen- **und** eine Zeitgrenze.

| Schutz | Wirkung |
|---|---|
| Eigener Regelthread (20 Hz) | Hängt CraftBeerPi (asyncio-Schleife blockiert), regelt der Thread trotzdem weiter und schließt. |
| Wächter-Thread | Steht der Regelthread länger als 1 s, schließt der Wächter das Ventil, ohne auf Sperren zu warten. |
| Anlauf | Kommt 5 s nach dem Öffnen kein Impuls (Wasserhahn zu, Sensor ab), schließt das Ventil → FEHLER. |
| Fluss reißt ab | 5 s ohne Impuls während des Laufs → FEHLER. |
| Zeitgrenze | Menge ÷ Mindestdurchfluss (2 L/min) × 1,5 + Anlauf, höchstens 30 min. |
| Störimpulse | Mehr als 40 L/min ist kein Wasser, sondern Störung → FEHLER. |
| Mengen | Zielmenge nur 0 < x ≤ 40 L. Handbetrieb höchstens 10 L und 120 s. |
| Doppelstart | Läuft das Ventil, werden Start und Handbetrieb abgelehnt (die alte App startete dann einen zweiten Zählthread). |
| Leck | Fließen bei geschlossenem Ventil mehr als 0,3 L, kommt eine Fehlermeldung („Ventil hängt?“). |
| Rücklesen | Nach jedem Schalten wird der Pegel des Ausgangs zurückgelesen. |
| Absturz/Neustart | Der Schritt speichert seinen Stand in `step_data.json`. Steht dort nach einem Neustart „läuft“, wird **nicht** erneut geöffnet: Er zeigt die gezählte Menge, du füllst den Rest von Hand und schaltest weiter. |
| Prozessende | Der Zusatz `systemd/ventil-zu.conf` setzt den GPIO vor jedem Start und nach jedem Ende von CraftBeerPi auf „zu“, auch nach `kill -9`. |
| GPIO-Konflikt | Schaltet ein anderer Aktor (z. B. ein GPIOActor „Kühlschrank“) denselben GPIO, startet der Ventil-Aktor nicht. |

Bei FEHLER schaltet der Schritt **nicht** weiter. Du prüfst den Kessel und entscheidest dann:
**Stop, dann Start** dosiert nur die Restmenge. **Weiter** übernimmt den Stand, wie er ist.

**Was Software nicht abdeckt:** Friert der Pi komplett ein (Kernel hängt), bleibt ein offener Ausgang offen.
Dagegen hilft nur Hardware: ein **stromlos geschlossenes** Ventil (NC) und, wenn du weggehst, ein
**Schwimmerschalter** in Reihe zur Ventilspule. Er unterbricht unabhängig von jeder Software, wenn der Kessel voll ist.

## Verdrahtung

**HAT „Biersteuerung“ mit Ventilausgängen** (65 × 96 mm, Zweig `ventile-druck` im Repo Biersteuerung, Stand
26.10.2026):

| Was | Pi | Bemerkung |
|---|---|---|
| Brauwasser-Ventil (12 V, NC) | **GPIO 9** (Pin 21) | Buchse **J20**, Ventil 5 (Pin 1 +12V, Pin 2 V5), low-side AO3400A, offen bei HIGH. Beim Booten Low = zu. |
| Durchflussmesser YF-S201 | **GPIO 6** (Pin 31) | Buchse **J19** (1 GND, 2 +5V, 3 Signal), Pull-up und Schmitt-Trigger 74LVC1G17 auf der Platine: Der Pi sieht nie mehr als 3,3 V. |

GPIO 9 ist ein SPI-Pin: **SPI auf dem Pi nicht aktivieren** (`dtparam=spi` aus lassen).

**Alter HAT ohne Ventilausgänge:**

| Was | Pi | Bemerkung |
|---|---|---|
| Ventilrelais | GPIO 19 (Pin 35) | Ausgang **J13** (bisher „Kühlschrank“, 12 V über BC337). Zieht bei HIGH an. |
| Durchflussmesser YF-S201, Signal | GPIO 24 (Pin 18) | Keine Buchse, direkt an die Stiftleiste. |
| YF-S201 Versorgung | 5 V (Pin 2), GND (Pin 6) | |

**Achtung, Pegel (nur alter HAT):** Der YF-S201 an 5 V liefert ein Signal bis 5 V. Die GPIOs des Pi vertragen 3,3 V.
Das geht oft eine Weile gut, schädigt den Eingang aber auf Dauer. Abhilfe mit einer Diode:

```
YF-S201 Signal ──|<── GPIO 24        Schottky-Diode BAT85 oder BAT43:
                 K   A               Kathode (Ring) zum Sensor, Anode zum Pi
GPIO 24 ──[4,7 kΩ]── 3,3 V (Pin 1)
```

Zieht der Sensor auf LOW, zieht die Diode den GPIO mit. Ist der Sensor HIGH, sperrt die Diode, und der
Widerstand hält den GPIO auf 3,3 V. Gezählt werden fallende Flanken, daran ändert sich nichts.

## Einbau auf dem Brau-Pi

**1. Alte App abschalten.** Sie belegt sonst dieselben GPIOs:

```bash
sudo systemctl disable --now ventil
```

**2. Herausfinden, mit welchem Python CraftBeerPi läuft:**

```bash
systemctl cat craftbeerpi | grep ExecStart
```

Steht dort z. B. `/home/pi/.local/bin/cbpi` oder ein venv-Pfad, gehören die folgenden `pip`-Aufrufe zu genau
diesem Python. Der Kopf des Skripts zeigt es: `head -1 $(which cbpi)`.

**3. lgpio und das Plugin installieren** (Beispiel venv unter `~/cbpi4-venv`, Pfad anpassen):

```bash
sudo apt install -y swig python3-dev liblgpio-dev
cd ~ && git clone https://github.com/timmkueck/cbpi4-ventil.git
~/cbpi4-venv/bin/pip install lgpio ./cbpi4-ventil
~/cbpi4-venv/bin/python -c "import lgpio, cbpi4_ventil; print('ok')"
```

Läuft CraftBeerPi mit dem System-Python, stattdessen `sudo apt install python3-lgpio` und
`sudo pip3 install --break-system-packages ./cbpi4-ventil`.

**4. Systemd-Zusatz einspielen** (Dienstname aus Schritt 2):

```bash
sudo mkdir -p /etc/systemd/system/craftbeerpi.service.d
sudo cp ~/cbpi4-ventil/systemd/ventil-zu.conf /etc/systemd/system/craftbeerpi.service.d/
sudo systemctl daemon-reload
```

**5. Hardware prüfen, bevor CraftBeerPi sie bekommt** (CraftBeerPi dafür kurz stoppen):

```bash
sudo systemctl stop craftbeerpi
~/cbpi4-venv/bin/python -m cbpi4_ventil.pruefen zaehlen 20
```

Das Ventil bleibt zu. Dreh das Flügelrad oder lass Wasser durchlaufen: Die Impulse müssen steigen. Dann mit
einem Messbecher:

```bash
~/cbpi4-venv/bin/python -m cbpi4_ventil.pruefen dosieren 1.0
```

Das Werkzeug gibt am Ende die korrigierte Zahl „Impulse pro Liter“ aus (gezählt ÷ gemessen).

**6. In CraftBeerPi einrichten:**

```bash
sudo systemctl start craftbeerpi
journalctl -u craftbeerpi | grep cbpi4_ventil
```

Erwartet: `Plugin cbpi4_ventil loaded successfully`. Dann in der Oberfläche:

- *Hardware → Aktor hinzufügen*: Typ **VentilDosierer**, GPIO Ventil 9, GPIO Durchfluss 6 (alter HAT: 19 und 24),
  Ventil offen bei HIGH,
  Impulse pro Liter 387 (bzw. der Wert aus Schritt 5), Simulation Nein.
- *Hardware → Sensor hinzufügen*: Typ **VentilDurchfluss**, Ventil = dieser Aktor, Wert = Menge.
- *Maischplan*: Schritt **WasserDosieren**, Liter = Hauptguss, Ventil = dieser Aktor.

Gibt es einen anderen Aktor auf denselben GPIOs (alter HAT: GPIOActor „Kühlschrank“ auf 19), vorher löschen. Sonst
meldet der Ventil-Aktor „GPIO … wird auch von … benutzt“ und bleibt zu.

## Einstellungen des Aktors

| Eigenschaft | Vorgabe | Bedeutung |
|---|---|---|
| Impulse pro Liter | 387 | Alter Faktor 6,45 × 60. Mit dem Messbecher einmessen. |
| Max Liter | 40 | größte Zielmenge |
| Hand max Sekunden / Liter | 120 / 10 | Grenzen im Handbetrieb (Aktor einschalten) |
| Anlauf Sekunden | 5 | ohne Impuls nach dem Öffnen → zu |
| Kein Fluss Sekunden | 5 | Impulse bleiben aus → zu |
| Min Durchfluss L/min | 2 | ergibt die Zeitgrenze |
| Max Durchfluss L/min | 40 | darüber: Störung → zu |
| Vorhalt Liter | 0 | So viel vor dem Ziel schließen. Das gleicht Wasser aus, das nach dem Schließen noch nachläuft. |
| Simulation | Nein | Ja = ohne Hardware (zum Ausprobieren der Oberfläche) |

## Entwickeln und testen ohne Pi

```bash
pip install cbpi4 pytest
python -m pytest tests/ -q
```

38 Tests: Regler mit simulierter Uhr (Menge, alle Grenzen, Wächter, Leck, Doppelstart, Rücklesen),
lgpio-Ansteuerung gegen eine Nachbildung des Moduls und Plugin-Klassen gegen das installierte cbpi4.
Am 25.10.2026: `get_state()` liefert einen bool (mit dem dict zeigte jedes Dashboard das Ventil als offen), neue
Aktion „Liter einlassen“, im Brautag-Dashboard (`Biersteuerung/software/brautag`) mit 2 l geprüft.
Zusätzlich lief das Plugin am 19.10.2026 in einem echten CraftBeerPi 4.7.7 (Simulation, nur an 127.0.0.1).
Dabei wurden 1,5 L und 8 L dosiert, die Schritte schalteten weiter, und Handbetrieb war während der
Dosierung gesperrt. Nach einem `kill -9` mitten in der Dosierung blieb das Ventil zu, und der Schritt
meldete den gezählten Stand.

Dabei aufgefallen (Verhalten von CraftBeerPi 4.7.7, nicht des Plugins):

- Ein Schritt schaltet nur weiter, wenn er `next()` aus einem eigenen Task aufruft. Kehrt `run()` einfach zurück, bleibt er aktiv.
- Ein beim Neustart aktiver Schritt wird **nicht** von selbst fortgesetzt: `find_by_status("A")` vergleicht
  mit Text statt mit dem Enum. Der Schritt steht still, bis du Stop und Start drückst. Das Plugin ist für
  beide Fälle gebaut.

**Nicht getestet mit echter Hardware:** lgpio auf dem Pi 3 unter Bookworm, Zählgenauigkeit des YF-S201,
Diodenschaltung, `pinctrl` im systemd-Zusatz. Dafür sind Schritt 5 und die erste Dosierung mit Messbecher da.

## Brau-Verbund

Gehört zur Brauerei von Timm: Brau-Pi mit HAT [Biersteuerung](https://github.com/timmkueck/Biersteuerung) und CraftBeerPi 4.7.7.
Gesamtbild: `docs/systeme.md` im Repo Biersteuerung.

| Repo | Inhalt |
|---|---|
| [cbpi4-brausicher](https://github.com/timmkueck/cbpi4-brausicher) | sichere Fühler, Aktoren (Heizschutz, Wächter), Kessellogik, Fortsetzen nach Neustart |
| [cbpi4-gaerdruck](https://github.com/timmkueck/cbpi4-gaerdruck) | Druckgärung im FermZilla: Drucksensor, Spunden/Nachdrücken, Kegs, Dashboard 2 |
| [cbpi4-ventil](https://github.com/timmkueck/cbpi4-ventil) | Brauwasser dosieren (Magnetventil + Durchflussmesser) |
| [cbpi4-simulation](https://github.com/timmkueck/cbpi4-simulation) | ganzer Brautag als Simulation, Störfälle, Prüfung der Schutzfunktionen |
| [cbpi4-bme280](https://github.com/timmkueck/cbpi4-bme280) | BME280-Sensor (meldet Fehler statt still leer) |
