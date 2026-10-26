"""Ventilregler fuer die Wasserdosierung, unabhaengig von CraftBeerPi.

Der Regler laeuft in einem eigenen Betriebssystem-Thread und nicht in der
asyncio-Schleife von cbpi: Haengt cbpi, schliesst das Ventil trotzdem.
Ein zweiter Thread (Waechter) schliesst es, wenn der Regelthread selbst
stehen bleibt.

Grundsatz: Im Zweifel ist das Ventil zu. Jeder Weg, der das Ventil oeffnet,
hat eine Mengen- und eine Zeitgrenze.
"""
import atexit
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("cbpi4_ventil")

BEREIT = "BEREIT"
DOSIERT = "DOSIERT"
HAND = "HAND"
FERTIG = "FERTIG"
FEHLER = "FEHLER"
ABGEBROCHEN = "ABGEBROCHEN"
AKTIV = (DOSIERT, HAND)


class ReglerFehler(Exception):
    pass


@dataclass
class Grenzen:
    max_liter: float = 40.0          # groesste erlaubte Zielmenge
    hand_max_s: float = 120.0        # Handbetrieb: laengste Oeffnungszeit
    hand_max_l: float = 10.0         # Handbetrieb: groesste Menge
    anlauf_s: float = 5.0            # so lange darf nach dem Oeffnen nichts fliessen
    kein_fluss_s: float = 5.0        # Pause ohne Impuls waehrend des Laufs
    min_lpm: float = 2.0             # langsamster plausibler Durchfluss (fuer die Zeitgrenze)
    max_lpm: float = 40.0            # schneller ist es nicht, sondern eine Stoerung
    max_dauer_s: float = 1800.0      # absolute Zeitgrenze je Oeffnung
    vorhalt_l: float = 0.0           # so viel vor dem Ziel schliessen (Nachlauf des Ventils)
    nachlauf_s: float = 3.0          # nach dem Schliessen noch mitzaehlen
    leck_l: float = 0.3              # Durchfluss bei geschlossenem Ventil, ab dem gewarnt wird
    waechter_s: float = 1.0          # Regelthread steht laenger -> Ventil zu


class SimBackend:
    """Nachbildung von Ventil und Durchflussmesser fuer Tests und Trockenlauf.

    Ist das Ventil offen, fliesst `fluss_lpm`; `leck_lpm` fliesst immer.
    Die Zeit kommt von `uhr`, damit Tests ohne Warten laufen."""

    name = "Simulation"

    def __init__(self, impulse_pro_liter, fluss_lpm=10.0, leck_lpm=0.0, uhr=time.monotonic):
        self.ipl = impulse_pro_liter
        self.fluss_lpm = fluss_lpm
        self.leck_lpm = leck_lpm
        self.uhr = uhr
        self.offen = False
        self._rest = 0.0
        self._n = 0
        self._t = uhr()
        self.letzter_impuls = None
        self.freigegeben = False
        self.schreibfehler = False

    def _fortschreiben(self):
        t = self.uhr()
        dt = max(0.0, t - self._t)
        self._t = t
        lpm = (self.fluss_lpm if self.offen else 0.0) + self.leck_lpm
        self._rest += lpm / 60.0 * dt * self.ipl
        neu = int(self._rest)
        if neu:
            self._rest -= neu
            self._n += neu
            self.letzter_impuls = t

    def ventil(self, auf):
        if self.schreibfehler:
            raise OSError("simulierter Schreibfehler")
        self._fortschreiben()
        self.offen = bool(auf)

    def ventil_ist_offen(self):
        return self.offen

    def impulse(self):
        self._fortschreiben()
        return self._n

    def freigeben(self):
        self.offen = False
        self.freigegeben = True


class LgpioBackend:
    """Echte Hardware ueber lgpio (Zeichengeraet /dev/gpiochipN).

    RPi.GPIO aus PyPI kann unter Bookworm (Kernel 6.6) keine Flanken mehr
    erkennen ("Failed to add edge detection"); lgpio nutzt die
    Kernel-Schnittstelle, die dafuer vorgesehen ist. Fehlt lgpio, gibt es
    einen Fehler -- nie eine stille Attrappe."""

    name = "lgpio"

    def __init__(self, gpio_ventil, gpio_fluss, aktiv_high=True, chip=0, entprellen_us=200):
        try:
            import lgpio
        except ImportError as e:
            raise ReglerFehler(
                "Python-Modul lgpio fehlt im Python von CraftBeerPi "
                "(pip install lgpio oder apt install python3-lgpio)") from e
        self.lg = lgpio
        self.gv, self.gf = int(gpio_ventil), int(gpio_fluss)
        self.pegel_auf = 1 if aktiv_high else 0
        self.pegel_zu = 0 if aktiv_high else 1
        self._n = 0
        self.letzter_impuls = None
        self._cb = None
        self.h = lgpio.gpiochip_open(chip)
        try:
            # Ausgang sofort im Zu-Zustand belegen
            self._pruefe(lgpio.gpio_claim_output(self.h, self.gv, self.pegel_zu), "Ventil-GPIO belegen")
            self._pruefe(lgpio.gpio_set_debounce_micros(self.h, self.gf, entprellen_us), "Entprellen")
            self._pruefe(lgpio.gpio_claim_alert(self.h, self.gf, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP),
                         "Flankenerkennung Durchfluss-GPIO")
            self._cb = lgpio.callback(self.h, self.gf, lgpio.FALLING_EDGE, self._impuls)
        except Exception:
            self.freigeben()
            raise

    def _pruefe(self, rc, was):
        if isinstance(rc, int) and rc < 0:
            raise ReglerFehler(f"{was}: {self.lg.error_text(rc)}")

    def _impuls(self, chip, gpio, level, tick):
        if level == 0:  # 2 waere ein Watchdog-Ereignis, kein Impuls
            self._n += 1
            self.letzter_impuls = time.monotonic()

    def ventil(self, auf):
        soll = self.pegel_auf if auf else self.pegel_zu
        self._pruefe(self.lg.gpio_write(self.h, self.gv, soll), "Ventil schalten")
        ist = self.lg.gpio_read(self.h, self.gv)
        if ist != soll:
            raise ReglerFehler(f"Ventil-GPIO {self.gv}: geschrieben {soll}, gelesen {ist}")

    def ventil_ist_offen(self):
        return self.lg.gpio_read(self.h, self.gv) == self.pegel_auf

    def impulse(self):
        return self._n

    def freigeben(self):
        # Erst zu, dann loslassen; Fehler beim Aufraeumen nur loggen
        for schritt in (lambda: self.lg.gpio_write(self.h, self.gv, self.pegel_zu),
                        lambda: self._cb and self._cb.cancel(),
                        lambda: self.lg.gpio_free(self.h, self.gf),
                        lambda: self.lg.gpio_free(self.h, self.gv),
                        lambda: self.lg.gpiochip_close(self.h)):
            try:
                schritt()
            except Exception as e:
                log.warning("Freigeben: %s", e)


class Regler:
    """Zustandsautomat BEREIT -> DOSIERT/HAND -> FERTIG/FEHLER/ABGEBROCHEN.

    `schritt()` wertet einmal aus; der Regelthread ruft es alle `takt_s`.
    Tests rufen es direkt mit einer eigenen Uhr auf (threads=False)."""

    def __init__(self, backend, impulse_pro_liter, grenzen=None, melden=None,
                 uhr=time.monotonic, takt_s=0.05, threads=True):
        if not impulse_pro_liter or impulse_pro_liter <= 0:
            raise ReglerFehler("Impulse pro Liter muss größer 0 sein")
        self.b = backend
        self.ipl = float(impulse_pro_liter)
        self.g = grenzen or Grenzen()
        self.melden = melden or (lambda art, text: None)
        self.uhr = uhr
        self.takt_s = takt_s
        self.lock = threading.RLock()
        self.zustand = BEREIT
        self.meldung = ""
        self.ziel = 0.0
        self.liter = 0.0
        self.lpm = 0.0
        self._n0 = 0
        self._t_auf = None
        self._t_zu = None
        self._t_letzter = None
        self._n_letzter = 0
        self._grenze_l = 0.0
        self._grenze_s = 0.0
        self._fluss_hist = []
        self._leck_n0 = None
        self._leck_gemeldet = False
        self.herz = uhr()
        self._aus = threading.Event()
        self.b.ventil(False)
        self._threads = []
        if threads:
            for ziel, name in ((self._regelschleife, "ventil-regler"), (self._waechterschleife, "ventil-waechter")):
                t = threading.Thread(target=ziel, name=name, daemon=True)
                t.start()
                self._threads.append(t)
        atexit.register(self.beenden)

    # --- Befehle -------------------------------------------------------

    def starten(self, liter):
        try:
            liter = float(liter)
        except (TypeError, ValueError):
            raise ReglerFehler(f"Menge {liter!r} ist keine Zahl")
        if not (0 < liter <= self.g.max_liter):
            raise ReglerFehler(f"Menge {liter} L außerhalb 0 … {self.g.max_liter} L")
        dauer = min(self.g.max_dauer_s, self.g.anlauf_s + liter / self.g.min_lpm * 60.0 * 1.5)
        self._oeffnen(DOSIERT, liter, liter, dauer)

    def hand_auf(self):
        self._oeffnen(HAND, 0.0, self.g.hand_max_l, self.g.hand_max_s)

    def stoppen(self, grund="Abgebrochen"):
        with self.lock:
            if self.zustand in AKTIV:
                self._schliessen(ABGEBROCHEN, grund)

    def beenden(self):
        """Ventil zu, Threads anhalten, Hardware freigeben. Mehrfach aufrufbar."""
        if self._aus.is_set():
            return
        with self.lock:
            if self.zustand in AKTIV:
                self._schliessen(ABGEBROCHEN, "Regler beendet")
            self._aus.set()
            try:
                self.b.ventil(False)
            except Exception as e:
                log.error("Ventil schliessen beim Beenden: %s", e)
            self.b.freigeben()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        atexit.unregister(self.beenden)

    def status(self):
        with self.lock:
            return dict(zustand=self.zustand, liter=round(self.liter, 3), ziel=self.ziel,
                        lpm=round(self.lpm, 2), meldung=self.meldung, offen=self.zustand in AKTIV,
                        backend=getattr(self.b, "name", "?"))

    @property
    def aktiv(self):
        return self.zustand in AKTIV

    # --- intern --------------------------------------------------------

    def _oeffnen(self, art, ziel, grenze_l, grenze_s):
        with self.lock:
            if self._aus.is_set():
                raise ReglerFehler("Regler ist beendet")
            if self.zustand in AKTIV:
                raise ReglerFehler(f"Ventil läuft bereits ({self.zustand})")
            if self.uhr() - self.herz > self.g.waechter_s:
                raise ReglerFehler("Regelthread reagiert nicht")
            jetzt = self.uhr()
            n = self.b.impulse()
            self.ziel, self.liter, self.lpm = ziel, 0.0, 0.0
            self._n0 = self._n_letzter = n
            self._t_auf = self._t_letzter = jetzt
            self._grenze_l, self._grenze_s = grenze_l, grenze_s
            self._fluss_hist = [(jetzt, n)]
            self._leck_n0 = None
            self.meldung = ""
            self.zustand = art
            try:
                self.b.ventil(True)
            except Exception as e:
                self._schliessen(FEHLER, f"Ventil öffnen fehlgeschlagen: {e}")
                raise ReglerFehler(self.meldung) from e
        log.info("Ventil auf (%s, Ziel %.2f L, Grenze %.1f L / %.0f s)", art, ziel, grenze_l, grenze_s)

    def _schliessen(self, zustand, text):
        # Aufrufer haelt self.lock
        try:
            self.b.ventil(False)
        except Exception as e:
            zustand, text = FEHLER, f"{text}; Schließen fehlgeschlagen: {e}"
        self.zustand = zustand
        self.meldung = text
        self._t_zu = self.uhr()
        self._leck_n0 = None
        self._leck_gemeldet = False
        art = {FERTIG: "info", ABGEBROCHEN: "warnung"}.get(zustand, "fehler")
        (log.error if art == "fehler" else log.info)("Ventil zu: %s (%s, %.2f L)", zustand, text, self.liter)
        try:
            self.melden(art, f"{text} ({self.liter:.2f} L)")
        except Exception as e:
            log.error("Meldung: %s", e)

    def schritt(self):
        with self.lock:
            if self._aus.is_set():
                return
            jetzt = self.uhr()
            self.herz = jetzt
            n = self.b.impulse()
            if n != self._n_letzter:
                self._n_letzter = n
                self._t_letzter = jetzt

            # Durchfluss ueber die letzten 2 s
            self._fluss_hist.append((jetzt, n))
            while len(self._fluss_hist) > 2 and jetzt - self._fluss_hist[0][0] > 2.0:
                self._fluss_hist.pop(0)
            t0, n0 = self._fluss_hist[0]
            self.lpm = (n - n0) / self.ipl / (jetzt - t0) * 60.0 if jetzt > t0 else 0.0

            if self.zustand in AKTIV:
                self.liter = (n - self._n0) / self.ipl
                lauf = jetzt - self._t_auf
                if self.zustand == DOSIERT and self.liter >= self.ziel - self.g.vorhalt_l:
                    self._schliessen(FERTIG, f"{self.ziel:g} L erreicht")
                elif self.liter >= self._grenze_l:
                    self._schliessen(FEHLER if self.zustand == DOSIERT else ABGEBROCHEN,
                                     f"Mengengrenze {self._grenze_l:g} L erreicht")
                elif lauf >= self._grenze_s:
                    self._schliessen(FEHLER if self.zustand == DOSIERT else ABGEBROCHEN,
                                     f"Zeitgrenze {self._grenze_s:.0f} s erreicht")
                elif n == self._n0 and lauf >= self.g.anlauf_s:
                    self._schliessen(FEHLER, f"Kein Durchfluss nach {self.g.anlauf_s:g} s "
                                             "(Wasser zu? Sensor angeschlossen?)")
                elif n != self._n0 and jetzt - self._t_letzter >= self.g.kein_fluss_s:
                    self._schliessen(FEHLER, f"Durchfluss seit {self.g.kein_fluss_s:g} s ausgeblieben")
                elif lauf >= 1.0 and self.lpm > self.g.max_lpm:
                    self._schliessen(FEHLER, f"Durchfluss {self.lpm:.0f} L/min unplausibel "
                                             "(Störimpulse?)")
                return

            # Ventil zu: Nachlauf mitzaehlen, danach auf Leck achten
            if self._t_zu is None:
                self._t_zu = jetzt - self.g.nachlauf_s
            if jetzt - self._t_zu < self.g.nachlauf_s:
                if self.zustand in (FERTIG, ABGEBROCHEN, FEHLER) and self._t_auf is not None:
                    self.liter = (n - self._n0) / self.ipl
                return
            if self._leck_n0 is None:
                self._leck_n0 = n
            leck = (n - self._leck_n0) / self.ipl
            if leck >= self.g.leck_l and not self._leck_gemeldet:
                self._leck_gemeldet = True
                try:
                    self.b.ventil(False)
                except Exception as e:
                    log.error("Ventil schliessen bei Leck: %s", e)
                self.zustand = FEHLER
                self.meldung = f"{leck:.2f} L geflossen, obwohl das Ventil zu ist (Ventil hängt?)"
                log.error(self.meldung)
                self.melden("fehler", self.meldung)

    def waechter(self):
        """Schliesst das Ventil, wenn der Regelthread zu lange nicht lief."""
        if self._aus.is_set():
            return
        if self.uhr() - self.herz <= self.g.waechter_s:
            return
        # Bewusst ohne Warten auf den Lock: der koennte gerade der Grund sein
        try:
            self.b.ventil(False)
        except Exception as e:
            log.critical("Waechter kann Ventil nicht schliessen: %s", e)
        if self.lock.acquire(timeout=0.5):
            try:
                if self.zustand in AKTIV:
                    self._schliessen(FEHLER, "Regelthread stand still, Wächter hat geschlossen")
            finally:
                self.lock.release()

    def _regelschleife(self):
        while not self._aus.wait(self.takt_s):
            try:
                self.schritt()
            except Exception as e:
                log.exception("Regelschritt")
                with self.lock:
                    self._schliessen(FEHLER, f"Interner Fehler: {e}")

    def _waechterschleife(self):
        while not self._aus.wait(self.g.waechter_s / 4):
            try:
                self.waechter()
            except Exception:
                log.exception("Waechter")
