"""Plugin-Tests gegen das installierte cbpi4 mit nachgebildetem cbpi-Kern
und dem Simulations-Backend:  pip install cbpi4 pytest && python -m pytest tests/ -q"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("cbpi")
from cbpi.api.dataclasses import Props  # noqa: E402
from cbpi.api.step import StepResult  # noqa: E402

import cbpi4_ventil as plugin  # noqa: E402
from cbpi4_ventil import regler as R  # noqa: E402


class FakeCbpi:
    def __init__(self):
        self.notes, self.registered, self.gespeichert = [], {}, 0
        self.log = types.SimpleNamespace(log_data=lambda id, v: None)
        self.ws = types.SimpleNamespace(send=lambda d, s=False: None)
        self.plugin = types.SimpleNamespace(register=lambda n, c: self.registered.__setitem__(n, c))
        self.actor = types.SimpleNamespace(data=[], find_by_id=self._find, ws_actor_update=self._noop)

        async def save():
            self.gespeichert += 1
        self.step = types.SimpleNamespace(push_udpate=lambda: None, save=save)

    async def _noop(self):
        return True

    def _find(self, id):
        return next((a for a in self.actor.data if a.id == id), None)

    def push_update(self, topic, data, retain=False):
        pass

    def notify(self, title, message, type=None, action=[]):
        self.notes.append((title, message, type))


def aktor_anlegen(c, id="v1", name="Ventil", **props):
    p = {"GPIO Ventil": 19, "GPIO Durchfluss": 24, "Impulse pro Liter": 387, "Simulation": "Ja", **props}
    inst = plugin.VentilDosierer(c, id, Props(p))
    item = types.SimpleNamespace(id=id, name=name, props=Props(p), instance=inst)
    c.actor.data.append(item)
    return inst


async def bis(bedingung, sek=10):
    for _ in range(int(sek / 0.02)):
        if bedingung():
            return True
        await asyncio.sleep(0.02)
    return False


def test_setup_registriert():
    c = FakeCbpi()
    plugin.setup(c)
    assert set(c.registered) == {"VentilDosierer", "VentilDurchfluss", "WasserDosieren"}


def test_schritt_dosiert_und_schaltet_weiter():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        a.regler.b.fluss_lpm = 60.0
        a.regler.g.max_lpm = 100.0
        a.regler.g.nachlauf_s = 0.2
        s = plugin.WasserDosieren(c, "s1", "Wasser", Props({"Liter": 1.5, "Ventil": "v1"}), None)
        await s.start()
        assert await bis(lambda: a.regler.b.offen)
        ergebnis = await asyncio.wait_for(s.task, 10)
        await a.stop()
        return c, s, ergebnis
    c, s, ergebnis = asyncio.run(go())
    assert ergebnis == StepResult.NEXT  # nur NEXT laesst cbpi weiterschalten
    assert s.props["Dosierstand"] == "fertig" and 1.5 <= s.props["Dosiert L"] < 1.6
    assert "dosiert" in s.summary and c.gespeichert >= 2


def test_fehler_schaltet_nicht_weiter_und_meldet():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c, **{"Anlauf Sekunden": 0.3})
        await a.start()
        a.regler.b.fluss_lpm = 0.0
        runner = asyncio.create_task(a.run())
        a.running = True
        s = plugin.WasserDosieren(c, "s1", "Wasser", Props({"Liter": 5, "Ventil": "v1"}), None)
        await s.start()
        assert await bis(lambda: "FEHLER" in s.summary)
        await asyncio.sleep(1.0)
        noch_aktiv = not s.task.done()
        offen = a.regler.b.offen
        await s.stop()
        a.running = False
        await runner
        await a.stop()
        return c, s, noch_aktiv, offen
    c, s, noch_aktiv, offen = asyncio.run(go())
    assert noch_aktiv and not offen
    assert s.props["Dosierstand"] == "unterbrochen"
    assert any("Kein Durchfluss" in n[1] for n in c.notes)


def test_neustart_waehrend_dosierung_oeffnet_nicht():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        props = Props({"Liter": 20, "Ventil": "v1", "Dosierstand": "laeuft", "Dosiert L": 7.5})
        s = plugin.WasserDosieren(c, "s1", "Wasser", props, None)
        await s.start()
        await asyncio.sleep(0.3)
        offen = a.regler.b.offen
        await s.next()
        await a.stop()
        return c, s, offen
    c, s, offen = asyncio.run(go())
    assert not offen
    assert "Neustart" in s.summary
    assert s.props["Dosierstand"] == ""  # Weiter = quittiert


def test_stop_und_fortsetzen_dosiert_nur_den_rest():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        a.regler.b.fluss_lpm = 60.0
        a.regler.g.max_lpm = 100.0
        a.regler.g.nachlauf_s = 0.1
        props = Props({"Liter": 3, "Ventil": "v1"})
        s = plugin.WasserDosieren(c, "s1", "Wasser", props, None)
        await s.start()
        assert await bis(lambda: a.regler.status()["liter"] >= 1.0)
        await s.stop()
        zwischen = props["Dosiert L"]
        offen_nach_stop = a.regler.b.offen
        s2 = plugin.WasserDosieren(c, "s1", "Wasser", props, None)
        await s2.start()
        assert await bis(lambda: a.regler.aktiv)
        rest = a.regler.status()["ziel"]
        await asyncio.wait_for(s2.task, 10)
        await a.stop()
        return props, zwischen, offen_nach_stop, rest
    props, zwischen, offen_nach_stop, rest = asyncio.run(go())
    assert not offen_nach_stop and props["Dosierstand"] == "fertig"
    assert abs(rest - (3 - zwischen)) < 0.01
    assert 3.0 <= props["Dosiert L"] < 3.1


def test_falscher_aktor_oeffnet_nichts():
    async def go():
        c = FakeCbpi()
        c.actor.data.append(types.SimpleNamespace(id="x", name="Heizung", props=Props({"GPIO": 5}),
                                                  instance=object()))
        s = plugin.WasserDosieren(c, "s1", "Wasser", Props({"Liter": 5, "Ventil": "x"}), None)
        await s.start()
        await asyncio.sleep(0.2)
        aktiv = not s.task.done()
        await s.stop()
        return c, s, aktiv
    c, s, aktiv = asyncio.run(go())
    assert aktiv and s.summary.startswith("FEHLER") and "VentilDosierer" in s.summary


def test_gpio_doppelt_belegt_wird_abgelehnt():
    async def go():
        c = FakeCbpi()
        c.actor.data.append(types.SimpleNamespace(id="k", name="Kuehlschrank",
                                                  props=Props({"GPIO": 19}), instance=None))
        a = aktor_anlegen(c)
        await a.start()
        await a.on(100)
        return c, a
    c, a = asyncio.run(go())
    assert a.regler is None and a.state is False
    assert any("Kuehlschrank" in n[1] for n in c.notes)


def test_aktor_hand_und_ersetzen():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        await a.on(100)
        alt = a.regler
        assert a.state and alt.b.offen
        await a.off()
        assert not alt.b.offen
        await a.on(100)
        # Eigenschaften geaendert: cbpi stoppt den alten und startet einen neuen Aktor
        await a.stop()
        b = plugin.VentilDosierer(c, "v1", a.props)
        await b.start()
        ergebnis = (alt.b.offen, alt.b.freigegeben, b.regler is not alt, plugin._REGLER[19] is b.regler)
        await b.stop()
        return ergebnis
    assert asyncio.run(go()) == (False, True, True, True)


def test_liter_einlassen_aus_dem_dashboard():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        a.regler.b.fluss_lpm = 60.0
        a.regler.g.max_lpm = 100.0
        assert a.get_state() is False  # bool, sonst zeigt die Oberflaeche "an"
        await a.liter_einlassen(Liter=0.5)
        offen = a.get_state()
        assert await bis(lambda: not a.regler.aktiv, 5)
        liter = a.regler.status()["liter"]
        await a.liter_einlassen(Liter=999)  # ueber Max Liter: abgelehnt, bleibt zu
        zu = not a.regler.aktiv
        await a.stop()
        return offen, liter, zu
    offen, liter, zu = asyncio.run(go())
    assert offen is True and liter >= 0.5 and zu


def test_sensor_zeigt_menge():
    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c)
        await a.start()
        a.regler.b.fluss_lpm = 60.0
        a.regler.g.max_lpm = 100.0
        sen = plugin.VentilDurchfluss(c, "f1", Props({"Ventil": "v1", "Wert": "Menge"}))
        sen.running = True
        t = asyncio.create_task(sen.run())
        a.regler.starten(0.5)
        assert await bis(lambda: sen.value >= 0.5, 5)
        sen.running = False
        await t
        await a.stop()
        return sen
    sen = asyncio.run(go())
    assert sen.get_unit() == "L" and sen.get_state()["value"] >= 0.5


def test_ohne_lgpio_kein_stiller_ersatz(monkeypatch):
    monkeypatch.setitem(sys.modules, "lgpio", None)

    async def go():
        c = FakeCbpi()
        a = aktor_anlegen(c, Simulation="Nein")
        await a.start()
        await a.on(100)
        return c, a
    c, a = asyncio.run(go())
    assert a.regler is None and not a.state
    assert any("lgpio fehlt" in n[1] for n in c.notes)
