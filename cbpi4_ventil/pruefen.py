"""Hardwarepruefung am Pi, ohne CraftBeerPi (cbpi vorher stoppen, sonst ist der GPIO belegt).

  python -m cbpi4_ventil.pruefen zaehlen 15     Ventil bleibt zu; 15 s Impulse zaehlen
  python -m cbpi4_ventil.pruefen dosieren 1.0   1 L mit allen Grenzen abmessen (Messbecher!)

Optionen: --ventil 9 --fluss 6 --ipl 387 --low (Relais zieht bei LOW an).
Alter HAT ohne Ventilausgaenge: --ventil 19 --fluss 24
"""
import argparse
import logging
import sys
import time

from .regler import AKTIV, Grenzen, LgpioBackend, Regler, ReglerFehler


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m cbpi4_ventil.pruefen")
    ap.add_argument("was", choices=["zaehlen", "dosieren"])
    ap.add_argument("wert", type=float)
    ap.add_argument("--ventil", type=int, default=9)
    ap.add_argument("--fluss", type=int, default=6)
    ap.add_argument("--ipl", type=float, default=387)
    ap.add_argument("--low", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        b = LgpioBackend(a.ventil, a.fluss, aktiv_high=not a.low)
    except ReglerFehler as e:
        print("FEHLER:", e)
        return 2
    r = Regler(b, a.ipl, Grenzen(max_liter=5.0, hand_max_l=5.0))
    try:
        time.sleep(0.2)
        if a.was == "zaehlen":
            n0 = b.impulse()
            ende = time.monotonic() + a.wert
            while time.monotonic() < ende:
                time.sleep(1)
                n = b.impulse() - n0
                print(f"Impulse {n:6d}  = {n / a.ipl:6.3f} L   {r.status()['lpm']:5.1f} L/min")
            print("Ventil war die ganze Zeit zu:", not b.ventil_ist_offen())
        else:
            r.starten(a.wert)
            while r.aktiv:
                time.sleep(0.5)
                s = r.status()
                print(f"{s['liter']:6.3f} / {a.wert:g} L   {s['lpm']:5.1f} L/min")
            time.sleep(r.g.nachlauf_s + 0.2)
            s = r.status()
            print(f"Ergebnis: {s['zustand']} – {s['meldung']} – gezählt {s['liter']:.3f} L")
            print("Mit dem Messbecher vergleichen; Impulse pro Liter neu = "
                  f"{a.ipl:g} × gezählt / gemessen")
    except KeyboardInterrupt:
        print("Abgebrochen")
    finally:
        r.beenden()
    return 0


if __name__ == "__main__":
    sys.exit(main())
