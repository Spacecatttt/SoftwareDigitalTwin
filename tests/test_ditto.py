"""Unit tests of the Eclipse Ditto integration (lab 4): graph vs. WoT Thing Models, mapping, adapter, monitor.
Run:  python -m unittest discover -s tests -v       (Ditto itself is not needed)"""
import json
import unittest
from pathlib import Path

from ditto_adapter import Adapter
from ditto_monitor import changed_values
from twin.config import DITTO_MODELS_URL
from twin.ditto_client import DittoError
from twin.ditto_graph import RELATIONS, farm_thing, printer_graph
from twin.ditto_mapping import MAPPING, merge_patches, snapshot_to_properties
from twin.state_store import TwinStore

MODELS = Path(__file__).resolve().parent.parent / "ditto" / "models"
T0 = 1_800_000_000.0


def load_tm(url: str) -> dict:
    assert url.startswith(DITTO_MODELS_URL), url
    return json.loads((MODELS / url[len(DITTO_MODELS_URL) + 1:]).read_text(encoding="utf-8"))


def resolved_properties(tm: dict) -> dict:
    """Thing-level properties of a TM including the ones inherited via tm:extends."""
    props = {}
    for link in tm.get("links", []):
        if link["rel"] == "tm:extends":
            props.update(resolved_properties(load_tm(link["href"])))
    props.update(tm.get("properties", {}))
    return props


def submodels(tm: dict) -> dict:
    return {l["instanceName"]: load_tm(l["href"]) for l in tm.get("links", []) if l["rel"] == "tm:submodel"}


def pipeline_snapshot() -> dict:
    """A real committed state of the lab 3 pipeline (same shape as published on factory/{id}/twin/state)."""
    store = TwinStore()
    d = store.device("printer_01")
    m = d.model
    m.set_connectivity("ONLINE", T0)
    for i in range(12):
        m.apply_measurement("nozzle_temperature", T0 + i, 210.0 + 0.1 * i, False)
        m.apply_measurement("extruder_feed_rate", T0 + i, 50.0, False)
        m.apply_measurement("stepper_current_x", T0 + i, 1.2, False)
        m.apply_measurement("bed_temperature", T0 + i, 60.0, False)
    m.refresh(T0 + 12)
    return store.commit(d, "PERIODIC", None, T0 + 12).snapshot


class GraphModelTest(unittest.TestCase):
    def test_every_thing_conforms_to_its_thing_model(self):
        things = [farm_thing()] + printer_graph("printer_01") + printer_graph("printer_02", 2)
        ids = {tid for tid, _ in things}
        for tid, thing in things:
            tm = load_tm(thing["definition"])
            props = resolved_properties(tm)
            # Ditto forbids non-modeled attributes: every attribute must be a TM property
            self.assertLessEqual(set(thing["attributes"]), set(props), tid)
            rels = thing["attributes"].get("relations", {})
            schema = props.get("relations", {"properties": {}, "required": []})
            self.assertLessEqual(set(rels), set(schema["properties"]), tid)
            self.assertLessEqual(set(schema.get("required", [])), set(rels), tid)
            for rel, target in rels.items():
                self.assertIn(rel, RELATIONS)
                self.assertIn(target, ids, f"{tid} --{rel}--> dangling {target}")
                # the target twin has the model declared as dt:target of the relation
                target_def = dict(things)[target]["definition"]
                self.assertEqual(schema["properties"][rel]["dt:target"], target_def, f"{tid} {rel}")

    def test_printer_graph_shape(self):
        things = printer_graph("printer_07", 7)
        self.assertEqual(len(things), 9)
        self.assertEqual(things[0][0], "kpi.dt:printer_07")
        self.assertEqual(things[0][1]["attributes"]["serialNumber"], "KPI-FDM-0007")
        edges = sum(len(t["attributes"].get("relations", {})) for _, t in things)
        self.assertEqual(edges, 22)


class MappingTest(unittest.TestCase):
    def test_mapping_targets_are_modeled(self):
        """Every (thing, feature, property) of the mapping exists in the Thing Models: otherwise Ditto would
        reject the adapter's patches with 400 wot:payload.validation.error."""
        defs = {tid: t["definition"] for tid, t in printer_graph("printer_01")}
        for suffix, feature, prop, _ in MAPPING:
            tid = "kpi.dt:printer_01" + (f".{suffix}" if suffix else "")
            features = submodels(load_tm(defs[tid]))
            self.assertIn(feature, features, f"{tid}/{feature}")
            self.assertIn(prop, features[feature]["properties"], f"{tid}/{feature}/{prop}")

    def test_snapshot_to_patches(self):
        props = snapshot_to_properties(pipeline_snapshot())
        self.assertEqual(props[("kpi.dt:printer_01.hotend", "heater", "temperature")], 211.1)
        self.assertEqual(props[("kpi.dt:printer_01.hotend", "heater", "thermalState")], "NORMAL")
        self.assertEqual(props[("kpi.dt:printer_01.controller", "connectivity", "status")], "ONLINE")
        self.assertTrue(props[("kpi.dt:printer_01.hotend", "heater", "measuredAt")].endswith("Z"))
        # no fan measurement yet: the property is not sent at all (null would delete it in a merge patch)
        self.assertNotIn(("kpi.dt:printer_01.fan", "fan", "speedPercent"), props)
        self.assertEqual(props[("kpi.dt:printer_01.fan", "fan", "quality")], "UNKNOWN")
        patches = merge_patches(props)
        self.assertEqual(patches["kpi.dt:printer_01.axis-x"]["features"]["motor"]["properties"]["loadState"], "NORMAL")
        self.assertNotIn(None, [v for _, v in props.items()])


class FakeDitto:
    def __init__(self, fail_with=None):
        self.calls, self.fail_with = [], fail_with

    def merge_thing(self, thing_id, patch, path="", correlation_id=None, if_match=None):
        if self.fail_with and thing_id in self.fail_with:
            raise self.fail_with[thing_id]
        self.calls.append((thing_id, patch))
        return 204


class AdapterTest(unittest.TestCase):
    def test_only_changes_are_sent(self):
        fake = FakeDitto()
        a = Adapter(lambda: fake, flush_s=0.25, workers=1)
        snap = pipeline_snapshot()
        a.apply(snap)
        a.flush()
        first = len(fake.calls)
        self.assertEqual(first, 8)                     # every twin with telemetry (all but the PSU) gets its values
        a.apply(snap)                                  # same state again: nothing to send
        a.flush()
        self.assertEqual(len(fake.calls), first)
        snap["measurements"]["nozzle_temperature"]["value"] = 230.0
        a.apply(snap)
        a.flush()
        thing_id, patch = fake.calls[-1]
        self.assertEqual(thing_id, "kpi.dt:printer_01.hotend")
        self.assertEqual(patch, {"features": {"heater": {"properties": {"temperature": 230.0}}}})

    def test_coalescing_keeps_the_latest_value(self):
        fake = FakeDitto()
        a = Adapter(lambda: fake, flush_s=0.25, workers=1)
        snap = pipeline_snapshot()
        a.apply(snap)
        a.flush()
        for t in (215.0, 220.0, 225.0):                # three states within one flush interval
            snap["measurements"]["nozzle_temperature"]["value"] = t
            a.apply(snap)
        a.flush()
        self.assertEqual(fake.calls[-1][1]["features"]["heater"]["properties"]["temperature"], 225.0)

    def test_not_provisioned_and_unreachable(self):
        import requests
        fake = FakeDitto(fail_with={"kpi.dt:printer_01.fan": DittoError(412, "precondition failed"),
                                    "kpi.dt:printer_01.bed": requests.ConnectionError("down")})
        a = Adapter(lambda: fake, flush_s=0.25, workers=2)
        a.apply(pipeline_snapshot())
        self.assertFalse(a.flush())                    # Ditto unreachable for one Thing -> retry later
        self.assertIn("kpi.dt:printer_01.fan", a.not_provisioned)
        self.assertTrue(all(k[0] == "kpi.dt:printer_01.bed" for k in a.pending))


class MonitorTest(unittest.TestCase):
    def test_changed_values_of_a_merge_event(self):
        ev = {"topic": "kpi.dt/printer_01.hotend/things/twin/events/merged", "path": "/",
              "value": {"features": {"heater": {"properties": {"thermalState": "CRITICAL", "temperature": 226.1}}}}}
        self.assertEqual(changed_values(ev), {"/features/heater/properties/thermalState": "CRITICAL",
                                              "/features/heater/properties/temperature": 226.1})
        ev = {"path": "/features/heater/properties/thermalState", "value": "NORMAL"}
        self.assertEqual(changed_values(ev), {"/features/heater/properties/thermalState": "NORMAL"})


if __name__ == "__main__":
    unittest.main()
