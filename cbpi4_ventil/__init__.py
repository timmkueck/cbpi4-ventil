"""CraftBeerPi4-Plugin: Brauwasser abmessen (Magnetventil + Durchflussmesser).

Aktor "VentilDosierer": besitzt Ventil und Durchflussmesser. Einschalten in
der Oberflaeche = Handbetrieb mit Zeit- und Mengengrenze.
Sensor "VentilDurchfluss": zeigt Menge (L) oder Durchfluss (L/min).
Schritt "WasserDosieren": oeffnet, bis die Menge erreicht ist.

Die Regelung selbst steckt in regler.py und laeuft in eigenen Threads.
"""
import asyncio
import logging
import queue

from cbpi.api import (CBPiActor, CBPiSensor, CBPiStep, Property, action,
                      parameters)
from cbpi.api.dataclasses import NotificationType

from .regler import (DOSIERT, FEHLER, FERTIG, ABGEBROCHEN, Grenzen,
                     LgpioBackend, Regler, ReglerFehler, SimBackend)

log = logging.getLogger("cbpi4_ventil")

GPIOS = list(range(2, 28))
NOTE = {"info": NotificationType.SUCCESS, "warnung": NotificationType.WARNING,
        "fehler": NotificationType.ERROR}

# Ein Regler je Ventil-GPIO, damit ein neu angelegter Aktor den alten ersetzt
# statt ihm die Leitung streitig zu machen.
_REGLER = {}


def _zahl(props, name, vorgabe):
    try:
        wert = props.get(name, vorgabe)
        return float(vorgabe if wert in (None, "") else wert)
    except (TypeError, ValueError):
        return float(vorgabe)


def fremde_gpio_aktoren(cbpi, gpio, eigene_id):
    """Namen anderer Aktoren, die denselben GPIO schalten wollen."""
    namen = []
    for item in getattr(cbpi.actor, "data", []):
        if item.id == eigene_id:
            continue
        props = item.props.to_dict() if hasattr(item.props, "to_dict") else dict(item.props or {})
        for schluessel in ("GPIO", "GPIO Ventil", "GPIO Durchfluss"):
            if str(props.get(schluessel, "")) == str(gpio):
                namen.append(item.name)
    return namen


@parameters([
    Property.Select("GPIO Ventil", options=GPIOS, description="Ausgang zum Ventilrelais (BCM), bisher 19"),
    Property.Select("GPIO Durchfluss", options=GPIOS, description="Impulse des Durchflussmessers (BCM), bisher 24"),
    Property.Select("Ventil offen bei", options=["HIGH", "LOW"], description="Pegel, bei dem das Relais anzieht"),
    Property.Number("Impulse pro Liter", configurable=True, default_value=387,
                    description="YF-S201: 387 (alter Faktor 6,45 x 60); mit Messbecher einmessen"),
    Property.Number("Max Liter", configurable=True, default_value=40, description="Größte Menge je Dosierung"),
    Property.Number("Hand max Sekunden", configurable=True, default_value=120, description="Handbetrieb: Zeitgrenze"),
    Property.Number("Hand max Liter", configurable=True, default_value=10, description="Handbetrieb: Mengengrenze"),
    Property.Number("Anlauf Sekunden", configurable=True, default_value=5,
                    description="Kein Impuls nach dem Öffnen → zu"),
    Property.Number("Kein Fluss Sekunden", configurable=True, default_value=5,
                    description="Impulse bleiben während des Laufs aus → zu"),
    Property.Number("Min Durchfluss L/min", configurable=True, default_value=2,
                    description="Bestimmt die Zeitgrenze: Menge / Min x 1,5"),
    Property.Number("Max Durchfluss L/min", configurable=True, default_value=40,
                    description="Mehr ist eine Störung → zu"),
    Property.Number("Vorhalt Liter", configurable=True, default_value=0,
                    description="So viel vor dem Ziel schließen"),
    Property.Select("Simulation", options=["Nein", "Ja"], description="Ja = ohne Hardware testen"),
])
class VentilDosierer(CBPiActor):

    def init_regler(self):
        p = self.props
        self.gpio_ventil = int(p.get("GPIO Ventil", 19) or 19)
        self.gpio_fluss = int(p.get("GPIO Durchfluss", 24) or 24)
        ipl = _zahl(p, "Impulse pro Liter", 387)
        g = Grenzen(
            max_liter=_zahl(p, "Max Liter", 40), hand_max_s=_zahl(p, "Hand max Sekunden", 120),
            hand_max_l=_zahl(p, "Hand max Liter", 10), anlauf_s=_zahl(p, "Anlauf Sekunden", 5),
            kein_fluss_s=_zahl(p, "Kein Fluss Sekunden", 5), min_lpm=_zahl(p, "Min Durchfluss L/min", 2),
            max_lpm=_zahl(p, "Max Durchfluss L/min", 40), vorhalt_l=_zahl(p, "Vorhalt Liter", 0))
        if self.gpio_ventil == self.gpio_fluss:
            raise ReglerFehler("Ventil und Durchfluss brauchen verschiedene GPIOs")
        andere = fremde_gpio_aktoren(self.cbpi, self.gpio_ventil, self.id) + \
            fremde_gpio_aktoren(self.cbpi, self.gpio_fluss, self.id)
        if andere:
            raise ReglerFehler(f"GPIO {self.gpio_ventil}/{self.gpio_fluss} wird auch von "
                               f"{', '.join(sorted(set(andere)))} benutzt")
        alt = _REGLER.pop(self.gpio_ventil, None)
        if alt is not None:
            alt.beenden()
        if p.get("Simulation", "Nein") == "Ja":
            backend = SimBackend(ipl)
        else:
            backend = LgpioBackend(self.gpio_ventil, self.gpio_fluss,
                                   aktiv_high=p.get("Ventil offen bei", "HIGH") != "LOW")
        self.regler = Regler(backend, ipl, g, melden=self._melden)
        _REGLER[self.gpio_ventil] = self.regler

    def _melden(self, art, text):
        # kommt aus dem Regelthread; cbpi wird nur aus der eigenen Schleife bedient
        self._meldungen.put((art, text))

    async def start(self):
        self.regler = None
        self._meldungen = queue.Queue()
        self.state = False
        try:
            self.init_regler()
        except Exception as e:
            log.error("Ventil nicht bereit: %s", e)
            self.cbpi.notify("Ventil nicht bereit", str(e), NotificationType.ERROR)
            # Aktor bleibt sichtbar, kann aber nichts oeffnen
            self.regler = None

    async def on(self, power=None, output=None):
        if self.regler is None:
            self.cbpi.notify("Ventil", "Nicht bereit, siehe Log", NotificationType.ERROR)
            return
        try:
            self.regler.hand_auf()
            self.state = True
        except ReglerFehler as e:
            self.cbpi.notify("Ventil", str(e), NotificationType.WARNING)

    async def off(self):
        if self.regler is not None:
            self.regler.stoppen("Von Hand geschlossen")
        self.state = False

    @action("Zähler prüfen (Ventil bleibt zu)", parameters=[])
    async def zaehler_pruefen(self, **kwargs):
        if self.regler is None:
            return
        s = self.regler.status()
        self.cbpi.notify("Ventil", f"Backend {s['backend']}, Durchfluss {s['lpm']} L/min, "
                                   f"Zustand {s['zustand']} {s['meldung']}", NotificationType.INFO)

    @action("Liter einlassen", parameters=[Property.Number("Liter", configurable=True, default_value=5,
                                                           description="Menge durch den Durchflussmesser")])
    async def liter_einlassen(self, Liter=5, **kwargs):
        """Dosieren von Hand aus dem Dashboard, mit denselben Grenzen wie WasserDosieren."""
        if self.regler is None:
            self.cbpi.notify("Ventil", "Nicht bereit, siehe Log", NotificationType.ERROR)
            return
        try:
            self.regler.starten(Liter)
            self.state = True
            self.cbpi.notify("Ventil", f"{float(Liter):g} L werden eingelassen", NotificationType.INFO)
        except ReglerFehler as e:
            self.cbpi.notify("Ventil", str(e), NotificationType.WARNING)

    def get_state(self):
        # Ein bool wie GPIOActor: Die Oberflaeche wertet den Wert direkt aus,
        # ein dict waere dort immer "an" (LED, SVG, Leitungen).
        return self.state

    async def run(self):
        while self.running:
            if self.regler is not None:
                offen = self.regler.aktiv
                if offen != self.state:
                    self.state = offen
                    await self.cbpi.actor.ws_actor_update()
            while not self._meldungen.empty():
                art, text = self._meldungen.get_nowait()
                self.cbpi.notify("Ventil", text, NOTE.get(art, NotificationType.INFO))
            await asyncio.sleep(0.5)

    async def stop(self):
        self._beenden()

    async def on_stop(self):
        self._beenden()

    def _beenden(self):
        r = getattr(self, "regler", None)
        if r is not None:
            r.beenden()
            if _REGLER.get(self.gpio_ventil) is r:
                del _REGLER[self.gpio_ventil]
        self.state = False


def ventil_aus_aktor(cbpi, aktor_id):
    item = cbpi.actor.find_by_id(aktor_id) if aktor_id else None
    inst = getattr(item, "instance", None)
    if not isinstance(inst, VentilDosierer):
        raise ReglerFehler("Kein Aktor vom Typ VentilDosierer gewählt")
    if inst.regler is None:
        raise ReglerFehler(f"Ventil '{item.name}' ist nicht bereit")
    return inst.regler


@parameters([
    Property.Actor("Ventil", description="Aktor vom Typ VentilDosierer"),
    Property.Select("Wert", options=["Menge", "Durchfluss"], description="Menge in L oder Durchfluss in L/min"),
])
class VentilDurchfluss(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        self.value = 0.0

    async def run(self):
        while self.running:
            try:
                s = ventil_aus_aktor(self.cbpi, self.props.get("Ventil")).status()
                wert = s["lpm"] if self.props.get("Wert") == "Durchfluss" else s["liter"]
            except ReglerFehler:
                wert = 0.0
            if wert != self.value:
                self.value = wert
                self.log_data(wert)
            self.push_update(wert)
            await asyncio.sleep(1)

    def get_state(self):
        return dict(value=self.value)

    def get_unit(self):
        return "L/min" if self.props.get("Wert") == "Durchfluss" else "L"


@parameters([
    Property.Number("Liter", configurable=True, description="Menge Brauwasser"),
    Property.Actor("Ventil", description="Aktor vom Typ VentilDosierer"),
])
class WasserDosieren(CBPiStep):
    """Fortschritt steht in den Schritt-Eigenschaften (step_data.json):
    "Dosierstand" = laeuft | unterbrochen | fertig, "Dosiert L" = Menge.
    cbpi startet einen aktiven Schritt nach einem Neustart von selbst; steht
    dann "laeuft" darin, war es ein Absturz -- es wird NICHT erneut geoeffnet."""

    async def _speichern(self, stand, liter):
        self.props["Dosierstand"] = stand
        self.props["Dosiert L"] = round(liter, 2)
        await self.save_props()

    async def on_start(self):
        self.regler = None
        self.eigener_lauf = False
        self.gemeldet = False
        self.basis = 0.0
        self.summary = ""
        stand = self.props.get("Dosierstand")
        dosiert = _zahl(self.props, "Dosiert L", 0)
        ziel = _zahl(self.props, "Liter", 0)
        if stand == "laeuft":
            self.summary = f"Neustart erkannt, {dosiert:g} L gezählt. Kessel prüfen, dann Weiter"
            self.cbpi.notify("Wasser dosieren", "CraftBeerPi wurde während der Dosierung neu gestartet. "
                             f"Gezählt waren {dosiert:g} von {ziel:g} L. Das Ventil bleibt zu. "
                             "Rest von Hand nachfüllen und den Schritt weiterschalten.", NotificationType.ERROR)
            await self.push_update()
            return
        self.basis = dosiert if stand == "unterbrochen" else 0.0
        rest = ziel - self.basis
        try:
            self.regler = ventil_aus_aktor(self.cbpi, self.props.get("Ventil"))
            if rest <= 0:
                raise ReglerFehler(f"Keine Restmenge ({ziel:g} L Ziel, {self.basis:g} L dosiert)")
            await self._speichern("laeuft", self.basis)
            self.regler.starten(rest)
            self.eigener_lauf = True
            self.summary = f"0 / {rest:g} L"
        except ReglerFehler as e:
            await self._speichern("unterbrochen" if self.basis else "", self.basis)
            self.summary = f"FEHLER: {e}"
            self.cbpi.notify("Wasser dosieren", str(e), NotificationType.ERROR)
        await self.push_update()

    async def run(self):
        zuletzt = None
        while self.running:
            await asyncio.sleep(0.5)
            if not self.eigener_lauf:
                continue
            s = self.regler.status()
            gesamt = self.basis + s["liter"]
            text = f"{gesamt:.2f} / {_zahl(self.props, 'Liter', 0):g} L"
            if s["zustand"] == DOSIERT:
                text += f" ({s['lpm']:.1f} L/min)"
                if zuletzt is None or gesamt - zuletzt >= 0.5:
                    zuletzt = gesamt
                    await self._speichern("laeuft", gesamt)
            elif s["zustand"] == FERTIG:
                self.eigener_lauf = False
                await asyncio.sleep(self.regler.g.nachlauf_s)
                gesamt = self.basis + self.regler.status()["liter"]
                await self._speichern("fertig", gesamt)
                self.summary = f"{gesamt:.2f} L dosiert"
                await self.push_update()
                self.cbpi.notify("Wasser dosieren", self.summary, NotificationType.SUCCESS)
                # cbpi schaltet nur bei NEXT weiter; next() bricht diesen Task ab,
                # muss also aus einem eigenen Task kommen (wie bei den eingebauten Schritten)
                asyncio.create_task(self.next())
                continue
            elif s["zustand"] in (FEHLER, ABGEBROCHEN):
                # Nicht weiterschalten: der Mensch entscheidet
                self.eigener_lauf = False
                await self._speichern("unterbrochen", gesamt)
                text = f"{s['zustand']}: {s['meldung']} – {gesamt:.2f} L. Prüfen, dann Stop+Start (Rest) oder Weiter"
            if text != self.summary:
                self.summary = text
                await self.push_update()

    async def on_stop(self):
        r = getattr(self, "regler", None)
        if r is not None and getattr(self, "eigener_lauf", False):
            r.stoppen("Schritt gestoppt")
            self.eigener_lauf = False
            await self._speichern("unterbrochen", self.basis + r.status()["liter"])
        if self.cancel_reason is not None and self.cancel_reason.name == "NEXT" \
                and self.props.get("Dosierstand") != "fertig":
            # Weiterschalten von Hand = Mensch hat den Stand akzeptiert
            await self._speichern("", 0)


def setup(cbpi):
    cbpi.plugin.register("VentilDosierer", VentilDosierer)
    cbpi.plugin.register("VentilDurchfluss", VentilDurchfluss)
    cbpi.plugin.register("WasserDosieren", WasserDosieren)
