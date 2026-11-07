"""Twin graph of the print farm in Eclipse Ditto: Things for the farm, the printers and their subsystems.

Ditto has no first-class relationships, so an edge of the graph is an attribute of the source Thing:
attributes/relations/<relation> = thingId of the target Thing. The relations are part of the WoT Thing Models
(ditto/models/things/*.tm.jsonld), so Ditto validates them like any other attribute. Every relation is single-valued
on the source side; the inverse direction ("what does the PSU power?") is a search query:
eq(attributes/relations/poweredBy,"kpi.dt:printer_01.psu").
"""
from dataclasses import dataclass, field

from .config import DITTO_MODELS_URL, DITTO_NAMESPACE, DITTO_POLICY_ID

FARM_ID = f"{DITTO_NAMESPACE}:farm-01"
RELATIONS = ["isPartOf", "controlledBy", "poweredBy", "cooledBy", "fedBy"]
# relations along which a failure propagates from the target to the source (source depends on target)
DEPENDENCIES = ["controlledBy", "poweredBy", "cooledBy", "fedBy"]


def model_url(model: str) -> str:
    return f"{DITTO_MODELS_URL}/things/{model}-1.0.0.tm.jsonld"


def printer_thing_id(printer_id: str) -> str:
    return f"{DITTO_NAMESPACE}:{printer_id}"


def subsystem_thing_id(printer_id: str, suffix: str) -> str:
    return f"{DITTO_NAMESPACE}:{printer_id}.{suffix}"


def printer_of(thing_id: str) -> str:
    """kpi.dt:printer_01.hotend -> printer_01"""
    return thing_id.split(":", 1)[1].split(".", 1)[0]


@dataclass(frozen=True)
class Subsystem:
    suffix: str          # thingId = kpi.dt:<printer>.<suffix>
    kind: str            # attributes/kind, node type of the graph
    model: str           # Thing Model ditto/models/things/<model>-1.0.0.tm.jsonld
    name: str
    attributes: dict = field(default_factory=dict)
    relations: dict = field(default_factory=dict)   # relation -> suffix of the target subsystem ("" = the printer)


SUB = {"isPartOf": "", "controlledBy": "controller", "poweredBy": "psu"}

SUBSYSTEMS: list[Subsystem] = [
    Subsystem("controller", "Controller", "controller", "Controller",
              {"firmware": "emu-fw 1.0 (labs 1-3 emulator)"}, {"isPartOf": "", "poweredBy": "psu"}),
    Subsystem("psu", "PowerSupply", "power-supply", "Power supply 24 V",
              {"ratedPowerW": 350, "outputVoltageV": 24}, {"isPartOf": ""}),
    Subsystem("hotend", "Hotend", "hotend", "Hotend",
              {"nozzleDiameterMm": 0.4, "maxTemperatureC": 300},
              {**SUB, "cooledBy": "fan", "fedBy": "extruder"}),
    Subsystem("bed", "HeatedBed", "heated-bed", "Heated bed", {"sizeMm": "250x210", "maxTemperatureC": 110}, SUB),
    Subsystem("extruder", "Extruder", "extruder", "Extruder", {"driveType": "direct"}, SUB),
    Subsystem("axis-x", "StepperAxis", "stepper-axis", "Axis X", {"axis": "X", "ratedCurrentA": 1.8}, SUB),
    Subsystem("fan", "Fan", "fan", "Cooling fan", {"purpose": "hotend and part cooling"}, SUB),
    Subsystem("enclosure", "Enclosure", "enclosure", "Enclosure", {"volumeL": 120}, {"isPartOf": ""}),
]
SUBSYSTEM_BY_SUFFIX = {s.suffix: s for s in SUBSYSTEMS}


def farm_thing() -> tuple[str, dict]:
    return FARM_ID, {
        "policyId": DITTO_POLICY_ID,
        "definition": model_url("print-farm"),
        "attributes": {"name": "KPI print farm", "kind": "PrintFarm", "site": "Kyiv, KPI, lab 4"},
    }


def printer_graph(printer_id: str, index: int = 1) -> list[tuple[str, dict]]:
    """Things of one printer: the printer itself + its subsystems with relations. Features are not listed:
    Ditto generates them from the tm:submodel links of the Thing Models (WoT skeleton generation)."""
    pid = printer_thing_id(printer_id)
    things = [(pid, {
        "policyId": DITTO_POLICY_ID,
        "definition": model_url("printer"),
        "attributes": {
            "name": f"3D printer {printer_id}", "kind": "Printer",
            "model": "FDM printer (emulated, labs 1-3)", "serialNumber": f"KPI-FDM-{index:04d}",
            "nozzleSetpointC": 210.0,
            "filament": {"material": "PLA", "diameterMm": 1.75, "densityGcm3": 1.24},
            "relations": {"isPartOf": FARM_ID},
        },
    })]
    for s in SUBSYSTEMS:
        attributes = {"name": f"{s.name} {printer_id}", "kind": s.kind, **s.attributes,
                      "relations": {rel: subsystem_thing_id(printer_id, target) if target else pid
                                    for rel, target in s.relations.items()}}
        if s.suffix == "controller":
            attributes["mqttClientId"] = printer_id
        things.append((subsystem_thing_id(printer_id, s.suffix), {
            "policyId": DITTO_POLICY_ID, "definition": model_url(s.model), "attributes": attributes}))
    return things


def policy() -> dict:
    """One policy for all twins. Least privilege: the adapter may write only the features (not the attributes /
    relations, not the alarms maintained by the monitor); the monitor may write only alarms and the farm summary and
    invoke the emergencyCooldown action."""
    def res(grant=(), revoke=()):
        return {"grant": list(grant), "revoke": list(revoke)}
    return {"entries": {
        "owner": {
            "subjects": {"nginx:ditto": {"type": "administrator (basic auth via ditto-nginx)"}},
            "resources": {"policy:/": res(["READ", "WRITE"]), "thing:/": res(["READ", "WRITE"]),
                          "message:/": res(["READ", "WRITE"])},
        },
        "adapter": {
            "subjects": {"nginx:adapter": {"type": "pipeline adapter (ditto_adapter.py)"}},
            "resources": {"thing:/": res(["READ"]), "thing:/features": res(["WRITE"]),
                          "thing:/features/alarms": res(revoke=["WRITE"]),
                          "thing:/features/summary": res(revoke=["WRITE"]),
                          "message:/": res(["READ"])},
        },
        "monitor": {
            "subjects": {"nginx:monitor": {"type": "event monitor (ditto_monitor.py)"}},
            "resources": {"thing:/": res(["READ"]),
                          "thing:/features/alarms": res(["WRITE"]),
                          "thing:/features/summary": res(["WRITE"]),
                          "message:/features/heater/inbox/messages/emergencyCooldown": res(["WRITE"])},
        },
    }}


