"""Regler-Tests ohne Hardware und ohne cbpi:  python -m pytest tests/ -q"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cbpi4_ventil"))
import regler as R  # noqa: E402

IPL = 387


class Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def bauen(fluss=10.0, leck=0.0, **grenzen):
    uhr = Uhr()
    b = R.SimBackend(IPL, fluss_lpm=fluss, leck_lpm=leck, uhr=uhr)
    meldungen = []
    r = R.Regler(b, IPL, R.Grenzen(**grenzen), melden=lambda a, t: meldungen.append((a, t)),
                 uhr=uhr, threads=False)
    r.schritt()
    return r, b, uhr, meldungen


def laufen(r, uhr, sekunden, takt=0.05):
    for _ in range(int(sekunden / takt)):
        uhr.t += takt
        r.schritt()


def test_dosiert_genau_und_schliesst():
    r, b, uhr, m = bauen(fluss=12.0)
    r.starten(5)
    assert b.offen and r.status()["zustand"] == R.DOSIERT
    laufen(r, uhr, 30)
    s = r.status()
    assert s["zustand"] == R.FERTIG and not b.offen
    assert 5.0 <= s["liter"] < 5.0 + 12.0 / 60 * 0.05 + 0.01  # hoechstens ein Takt zu viel
    assert m[-1][0] == "info"


def test_vorhalt_schliesst_frueher():
    r, b, uhr, _ = bauen(fluss=12.0, vorhalt_l=0.2)
    r.starten(5)
    laufen(r, uhr, 30)
    assert 4.8 <= r.status()["liter"] < 4.85


@pytest.mark.parametrize("menge", [0, -1, 40.01, "abc", None])
def test_ungueltige_menge_oeffnet_nicht(menge):
    r, b, _, _ = bauen()
    with pytest.raises(R.ReglerFehler):
        r.starten(menge)
    assert not b.offen and r.status()["zustand"] == R.BEREIT


def test_doppelstart_abgelehnt():
    r, b, uhr, _ = bauen()
    r.starten(2)
    with pytest.raises(R.ReglerFehler):
        r.starten(2)
    with pytest.raises(R.ReglerFehler):
        r.hand_auf()
    laufen(r, uhr, 20)
    assert 2.0 <= r.status()["liter"] < 2.02  # kein doppeltes Zaehlen


def test_kein_anlauf_schliesst():
    r, b, uhr, m = bauen(fluss=0.0)
    r.starten(10)
    laufen(r, uhr, 4.9)
    assert b.offen
    laufen(r, uhr, 0.2)
    s = r.status()
    assert s["zustand"] == R.FEHLER and not b.offen and "Kein Durchfluss" in s["meldung"]
    assert m[-1][0] == "fehler"


def test_fluss_reisst_ab():
    r, b, uhr, _ = bauen(fluss=10.0)
    r.starten(10)
    laufen(r, uhr, 10)
    b.fluss_lpm = 0.0  # Wasserhahn zu oder Sensor ab
    laufen(r, uhr, 5.2)
    s = r.status()
    assert s["zustand"] == R.FEHLER and not b.offen and "ausgeblieben" in s["meldung"]


def test_zeitgrenze_bei_zu_langsamem_fluss():
    # 10 L bei min 2 L/min -> Grenze 5 + 300 x 1,5 = 455 s; es fliessen nur 1 L/min
    r, b, uhr, _ = bauen(fluss=1.0)
    r.starten(10)
    laufen(r, uhr, 454, takt=0.5)
    assert b.offen
    laufen(r, uhr, 2, takt=0.5)
    s = r.status()
    assert s["zustand"] == R.FEHLER and "Zeitgrenze" in s["meldung"] and not b.offen


def test_stoerimpulse_schliessen():
    r, b, uhr, _ = bauen(fluss=80.0)
    r.starten(30)
    laufen(r, uhr, 1.5)
    s = r.status()
    assert s["zustand"] == R.FEHLER and "unplausibel" in s["meldung"] and not b.offen


def test_hand_mit_grenzen():
    r, b, uhr, _ = bauen(fluss=10.0, hand_max_s=30, hand_max_l=100)
    r.hand_auf()
    laufen(r, uhr, 29.9)
    assert b.offen
    laufen(r, uhr, 0.2)
    assert r.status()["zustand"] == R.ABGEBROCHEN and not b.offen
    r2, b2, uhr2, _ = bauen(fluss=10.0, hand_max_s=1000, hand_max_l=1.0)
    r2.hand_auf()
    laufen(r2, uhr2, 10)
    assert not b2.offen and 1.0 <= r2.status()["liter"] < 1.02


def test_stoppen_und_neustart():
    r, b, uhr, _ = bauen()
    r.starten(10)
    laufen(r, uhr, 3)
    r.stoppen("Test")
    assert r.status()["zustand"] == R.ABGEBROCHEN and not b.offen
    r.starten(1)
    laufen(r, uhr, 10)
    assert r.status()["zustand"] == R.FERTIG


def test_leck_bei_geschlossenem_ventil():
    r, b, uhr, m = bauen(fluss=10.0)
    r.starten(1)
    laufen(r, uhr, 10)
    assert r.status()["zustand"] == R.FERTIG
    b.leck_lpm = 1.0  # Ventil haengt offen
    laufen(r, uhr, 30)
    s = r.status()
    assert s["zustand"] == R.FEHLER and "obwohl das Ventil zu ist" in s["meldung"]
    assert sum(1 for a, _ in m if a == "fehler") == 1  # nur einmal melden


def test_nachlauf_wird_mitgezaehlt():
    r, b, uhr, _ = bauen(fluss=10.0)
    r.starten(2)
    laufen(r, uhr, 15)
    vorher = r.status()["liter"]
    r2, b2, uhr2, _ = bauen(fluss=10.0)
    r2.starten(2)
    while r2.status()["zustand"] == R.DOSIERT:
        uhr2.t += 0.05
        r2.schritt()
    b2.leck_lpm = 3.0  # Restwasser laeuft nach dem Schliessen
    laufen(r2, uhr2, 2)
    assert r2.status()["liter"] > vorher + 0.05
    assert r2.status()["zustand"] == R.FERTIG


def test_schreibfehler_beim_oeffnen():
    r, b, uhr, _ = bauen()
    b.schreibfehler = True
    with pytest.raises(R.ReglerFehler):
        r.starten(1)
    assert r.status()["zustand"] == R.FEHLER


def test_waechter_schliesst_bei_stillstand():
    r, b, uhr, m = bauen()
    r.starten(10)
    laufen(r, uhr, 1)
    uhr.t += 1.5  # Regelthread kam 1,5 s nicht dran
    r.waechter()
    assert not b.offen and r.status()["zustand"] == R.FEHLER
    assert "Wächter" in r.status()["meldung"]


def test_waechter_schliesst_auch_wenn_lock_haengt():
    r, b, uhr, _ = bauen()
    r.starten(10)
    laufen(r, uhr, 1)
    haelt = threading.Event()
    frei = threading.Event()

    def blockieren():
        with r.lock:
            haelt.set()
            frei.wait(5)
    t = threading.Thread(target=blockieren)
    t.start()
    haelt.wait(2)
    uhr.t += 2
    r.waechter()
    assert not b.offen  # Ventil zu, obwohl der Lock belegt ist
    frei.set()
    t.join()


def test_beenden_schliesst_und_gibt_frei():
    r, b, uhr, _ = bauen()
    r.starten(10)
    laufen(r, uhr, 1)
    r.beenden()
    r.beenden()
    assert not b.offen and b.freigegeben and r.status()["zustand"] == R.ABGEBROCHEN
    with pytest.raises(R.ReglerFehler):
        r.starten(1)


def test_mit_echten_threads():
    b = R.SimBackend(IPL, fluss_lpm=60.0)  # 1 L/s
    r = R.Regler(b, IPL, R.Grenzen(), takt_s=0.02)
    try:
        time.sleep(0.1)
        r.starten(0.5)
        t0 = time.monotonic()
        while r.aktiv and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        s = r.status()
        assert s["zustand"] == R.FERTIG and not b.offen
        assert 0.5 <= s["liter"] < 0.56
    finally:
        r.beenden()


def test_mit_echten_threads_haengender_regelschritt():
    """Haengt der Regelschritt (z. B. im Backend), schliesst der Waechter."""
    b = R.SimBackend(IPL, fluss_lpm=6.0)
    r = R.Regler(b, IPL, R.Grenzen(waechter_s=0.3), takt_s=0.02)
    try:
        time.sleep(0.1)
        r.starten(10)
        orig = b.impulse
        b.impulse = lambda: (time.sleep(3), orig())[1]  # Regelthread blockiert
        time.sleep(1.0)
        assert not b.offen
    finally:
        b.impulse = orig
        r.beenden()


def test_lgpio_fehlt_ist_ein_fehler(monkeypatch):
    monkeypatch.setitem(sys.modules, "lgpio", None)
    with pytest.raises(R.ReglerFehler, match="lgpio fehlt"):
        R.LgpioBackend(19, 24)


class FakeLgpio:
    """Nachbildung von lgpio 0.2.2 (Signaturen aus dem Quelltext lgpio_extra.py)."""
    FALLING_EDGE, SET_PULL_UP = 2, 32

    class error(Exception):
        pass

    def __init__(self, belegt=()):
        self.pegel, self.aufrufe, self.belegt, self.cb = {}, [], set(belegt), None

    def gpiochip_open(self, chip):
        self.aufrufe.append(("open", chip))
        return 7

    def gpio_claim_output(self, h, gpio, level=0, lFlags=0):
        if gpio in self.belegt:
            raise self.error("GPIO busy")
        self.pegel[gpio] = level
        self.aufrufe.append(("out", gpio, level))
        return 0

    def gpio_set_debounce_micros(self, h, gpio, us):
        return 0

    def gpio_claim_alert(self, h, gpio, eFlags, lFlags=0, notify_handle=None):
        self.aufrufe.append(("alert", gpio, eFlags, lFlags))
        return 0

    def callback(self, h, gpio, edge=1, func=None):
        lg = self

        class Cb:
            def cancel(self):
                lg.aufrufe.append(("cancel", gpio))
        self.cb = func
        return Cb()

    def gpio_write(self, h, gpio, level):
        self.pegel[gpio] = level
        return 0

    def gpio_read(self, h, gpio):
        return self.pegel[gpio]

    def gpio_free(self, h, gpio):
        self.aufrufe.append(("free", gpio))
        return 0

    def gpiochip_close(self, h):
        self.aufrufe.append(("close", h))
        return 0

    def error_text(self, rc):
        return f"Fehler {rc}"


@pytest.mark.parametrize("aktiv_high,auf,zu", [(True, 1, 0), (False, 0, 1)])
def test_lgpio_backend(monkeypatch, aktiv_high, auf, zu):
    lg = FakeLgpio()
    monkeypatch.setitem(sys.modules, "lgpio", lg)
    b = R.LgpioBackend(19, 24, aktiv_high=aktiv_high)
    assert ("out", 19, zu) in lg.aufrufe  # sofort im Zu-Zustand belegt
    assert ("alert", 24, lg.FALLING_EDGE, lg.SET_PULL_UP) in lg.aufrufe
    b.ventil(True)
    assert lg.pegel[19] == auf and b.ventil_ist_offen()
    for _ in range(387):
        lg.cb(0, 24, 0, 0)
    lg.cb(0, 24, 2, 0)  # Watchdog-Ereignis zaehlt nicht
    assert b.impulse() == 387
    b.freigeben()
    assert lg.pegel[19] == zu
    assert [a[0] for a in lg.aufrufe[-4:]] == ["cancel", "free", "free", "close"]


def test_lgpio_belegter_pin_gibt_frei(monkeypatch):
    lg = FakeLgpio(belegt={19})
    monkeypatch.setitem(sys.modules, "lgpio", lg)
    with pytest.raises(FakeLgpio.error):
        R.LgpioBackend(19, 24)
    assert ("close", 7) in lg.aufrufe


def test_lgpio_rueckleseprobe(monkeypatch):
    lg = FakeLgpio()
    monkeypatch.setitem(sys.modules, "lgpio", lg)
    b = R.LgpioBackend(19, 24)
    lg.gpio_write = lambda h, g, l: 0  # Schreiben wirkt nicht
    with pytest.raises(R.ReglerFehler, match="gelesen"):
        b.ventil(True)
