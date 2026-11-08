"""Unit tests of the 3D viewer backend (lab 5): relay of Ditto events to the browsers, bounded client queues,
the glTF model and its binding to the twins (Ditto is not needed)."""
import json
import struct
import time
import unittest
from pathlib import Path

from twin.ditto_graph import SUBSYSTEMS
from viz_server import TwinRelay, _put

GLB = Path(__file__).resolve().parent.parent / "web" / "3d" / "models" / "printer.glb"


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.relay = TwinRelay("viewer", None, "http://localhost:1")     # nothing is started, no connection
        self.q = self.relay.subscribe()

    def test_twin_event_is_relayed_with_latency(self):
        sent = time.monotonic_ns() - 15_000_000                           # adapter sent it 15 ms ago
        self.relay.on_event({"topic": "kpi.dt/printer_02.hotend/things/twin/events/merged", "path": "/",
                             "value": {"features": {"heater": {"properties": {"thermalState": "CRITICAL"}}}},
                             "revision": 576, "headers": {"correlation-id": f"adapter:{sent}:abc"}})
        name, msg = self.q.get_nowait()
        self.assertEqual(name, "twin")
        self.assertEqual(msg["thingId"], "kpi.dt:printer_02.hotend")
        self.assertEqual(msg["action"], "merged")
        self.assertEqual(msg["value"]["features"]["heater"]["properties"]["thermalState"], "CRITICAL")
        self.assertGreaterEqual(msg["latencyMs"], 15)

    def test_other_signals_are_ignored_and_unsubscribe(self):
        self.relay.on_event({"topic": "kpi.dt/printer_02.hotend/things/live/messages/emergencyCooldown"})
        self.assertTrue(self.q.empty())
        self.relay.unsubscribe(self.q)
        self.relay.on_event({"topic": "kpi.dt/printer_01/things/twin/events/merged", "path": "/", "value": {}})
        self.assertTrue(self.q.empty())

    def test_slow_browser_drops_the_oldest(self):
        import queue
        q = queue.Queue(maxsize=3)
        for i in range(5):
            _put(q, i)
        self.assertEqual([q.get_nowait() for _ in range(3)], [2, 3, 4])


class ModelTest(unittest.TestCase):
    def test_every_twin_of_a_printer_has_a_node(self):
        """The viewer binds telemetry by node name: kpi.dt:<printer>.<node> <-> node <node> of printer.glb."""
        data = GLB.read_bytes()
        n = struct.unpack_from("<I", data, 12)[0]
        nodes = {nd["name"] for nd in json.loads(data[20:20 + n])["nodes"]}
        for s in SUBSYSTEMS:
            self.assertIn(s.suffix, nodes)
        self.assertLessEqual({"frame", "carriage", "fan_rotor", "extruder_gear", "controller_led", "print_part"}, nodes)


if __name__ == "__main__":
    unittest.main()
