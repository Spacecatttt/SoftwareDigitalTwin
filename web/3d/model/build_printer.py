"""Blender script: builds a simplified enclosed FDM 3D printer and exports it as glTF binary (printer.glb).

  blender --background --factory-startup --python build_printer.py -- <output.glb>

Every subsystem of the printer is a named node; the names are the suffixes of the Ditto twins of lab 4
(kpi.dt:printer_01.<name>), so the web app binds telemetry to the model by node name:

  printer                      root
  ├── frame                    aluminium frame and the deck
  ├── enclosure                transparent panels (chamber temperature)
  ├── psu                      power supply in the base
  ├── controller               mainboard in the base  └── controller_led (link indicator)
  ├── bed                      heated bed              └── print_part (printed object, grows with extrusion)
  └── axis-x                   X gantry: axis-x_rails, axis-x_motor
      └── carriage             moves along X
          ├── extruder         extruder_body, extruder_gear (rotates with the feed rate)
          ├── hotend           hotend_heatsink, hotend_block, hotend_nozzle (glows with the temperature)
          └── fan              fan_housing, fan_rotor (spins with the fan speed)
"""
import math
import sys

import bpy

OUT = sys.argv[sys.argv.index("--") + 1]

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
COLL = bpy.context.scene.collection


def material(name, rgb, metallic=0.0, roughness=0.5, alpha=1.0, emission=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    if alpha < 1.0:
        bsdf.inputs["Alpha"].default_value = alpha
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"
        if hasattr(mat, "blend_method"):
            mat.blend_method = "BLEND"
    if emission:
        bsdf.inputs["Emission Color"].default_value = (*emission, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 1.0
    return mat


M = {
    "frame": material("frame_aluminium", (0.16, 0.17, 0.19), 0.7, 0.35),
    "glass": material("enclosure_glass", (0.72, 0.86, 1.0), 0.0, 0.05, alpha=0.14),
    "psu": material("psu_steel", (0.72, 0.73, 0.76), 0.8, 0.3),
    "pcb": material("controller_pcb", (0.05, 0.24, 0.13), 0.1, 0.6),
    "led": material("led", (0.1, 0.9, 0.3), 0.0, 0.3, emission=(0.1, 0.9, 0.3)),
    "bed": material("bed_glass", (0.035, 0.035, 0.04), 0.1, 0.18),
    "heater": material("bed_heater", (0.62, 0.17, 0.06), 0.0, 0.7),
    "pla": material("pla_blue", (0.1, 0.42, 0.9), 0.0, 0.45),
    "steel": material("rod_steel", (0.8, 0.8, 0.83), 0.95, 0.2),
    "motor": material("motor_black", (0.05, 0.05, 0.06), 0.4, 0.5),
    "alu": material("carriage_aluminium", (0.78, 0.79, 0.82), 0.85, 0.3),
    "orange": material("extruder_orange", (0.93, 0.42, 0.08), 0.0, 0.45),
    "brass": material("brass", (0.8, 0.58, 0.22), 0.9, 0.3),
    "fan": material("fan_black", (0.07, 0.07, 0.08), 0.0, 0.55),
}


def empty(name, parent=None, loc=(0, 0, 0)):
    obj = bpy.data.objects.new(name, None)
    COLL.objects.link(obj)
    obj.parent = parent
    obj.location = loc
    return obj


def _finish(obj, name, mat, parent, loc, rot=None):
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    obj.name = name
    obj.data.name = name
    obj.data.materials.append(M[mat])
    obj.parent = parent
    obj.location = loc
    if rot:
        obj.rotation_euler = rot
    return obj


def box(name, size, mat, parent=None, loc=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1)
    obj = bpy.context.active_object
    obj.scale = size
    return _finish(obj, name, mat, parent, loc)


def cylinder(name, radius, depth, mat, parent=None, loc=(0, 0, 0), axis="Z", verts=32, radius2=None):
    if radius2 is None:
        bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=depth)
    else:
        bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=radius, radius2=radius2, depth=depth)
    obj = bpy.context.active_object
    obj.rotation_euler = {"X": (math.pi / 2, 0, 0), "Y": (0, math.pi / 2, 0), "Z": (0, 0, 0)}[axis]
    return _finish(obj, name, mat, parent, loc)


def join(name, objects):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = name
    return obj


W, D, H = 0.46, 0.44, 0.60          # outer size, m (Blender: X width, Y depth, Z height)
DECK = 0.09                          # top of the base compartment

printer = empty("printer")

# --- frame: bottom plate, deck, 4 posts, top rectangle -------------------------------------------------
parts = [box("f_bottom", (W, D, 0.01), "frame", None, (0, 0, 0.005)),
         box("f_deck", (W, D, 0.01), "frame", None, (0, 0, DECK - 0.005))]
for sx in (-1, 1):
    for sy in (-1, 1):
        parts.append(box(f"f_post{sx}{sy}", (0.02, 0.02, H), "frame", None, (sx * (W / 2 - 0.01), sy * (D / 2 - 0.01), H / 2)))
parts += [box("f_top_x0", (W, 0.02, 0.02), "frame", None, (0, -D / 2 + 0.01, H - 0.01)),
          box("f_top_x1", (W, 0.02, 0.02), "frame", None, (0, D / 2 - 0.01, H - 0.01)),
          box("f_top_y0", (0.02, D, 0.02), "frame", None, (-W / 2 + 0.01, 0, H - 0.01)),
          box("f_top_y1", (0.02, D, 0.02), "frame", None, (W / 2 - 0.01, 0, H - 0.01))]
frame = join("frame", parts)
frame.parent = printer

# --- enclosure: back, sides, top and front door made of glass ------------------------------------------
zc, ph = (DECK + H) / 2, H - DECK
glass = [box("g_back", (W - 0.04, 0.004, ph), "glass", None, (0, D / 2 - 0.01, zc)),
         box("g_front", (W - 0.04, 0.004, ph), "glass", None, (0, -D / 2 + 0.01, zc)),
         box("g_left", (0.004, D - 0.04, ph), "glass", None, (-W / 2 + 0.01, 0, zc)),
         box("g_right", (0.004, D - 0.04, ph), "glass", None, (W / 2 - 0.01, 0, zc)),
         box("g_top", (W - 0.04, D - 0.04, 0.004), "glass", None, (0, 0, H - 0.002))]
enclosure = join("enclosure", glass)
enclosure.parent = printer

# --- base compartment: power supply and controller -------------------------------------------------------
box("psu", (0.16, 0.10, 0.06), "psu", printer, (-0.115, 0.06, 0.04))
controller = box("controller", (0.14, 0.10, 0.03), "pcb", printer, (0.12, 0.05, 0.025))
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.006, segments=16, ring_count=8)
_finish(bpy.context.active_object, "controller_led", "led", controller, (0.05, -0.052, 0.008))

# --- heated bed and the printed object ----------------------------------------------------------------
bed = join("bed", [box("bed_plate", (0.30, 0.30, 0.012), "bed", None, (0, 0, 0)),
                   box("bed_heater", (0.28, 0.28, 0.003), "heater", None, (0, 0, -0.0075))])
bed.parent = printer
bed.location = (0, 0, DECK + 0.04)
bpy.ops.mesh.primitive_cylinder_add(vertices=40, radius=0.035, depth=0.05, location=(0, 0, 0.025))
part = bpy.context.active_object
bpy.ops.object.transform_apply(location=True)          # origin at the bottom: scaling in Z = growing part
part.name = part.data.name = "print_part"
part.data.materials.append(M["pla"])
part.parent = bed
part.location = (0, 0, 0.006)

# --- X axis gantry with the carriage ---------------------------------------------------------------------
axis = empty("axis-x", printer, (0, 0, 0.40))
join("axis-x_rails", [cylinder("rail0", 0.005, 0.38, "steel", None, (0, 0.012, 0.0), axis="Y"),
                      cylinder("rail1", 0.005, 0.38, "steel", None, (0, 0.012, 0.03), axis="Y")]).parent = axis
box("axis-x_motor", (0.042, 0.042, 0.042), "motor", axis, (-0.205, 0.012, 0.015))
carriage = empty("carriage", axis, (0, 0, 0))
box("carriage_plate", (0.07, 0.012, 0.07), "alu", carriage, (0, -0.002, 0.015))

extruder = empty("extruder", carriage, (0, -0.032, 0.035))
box("extruder_body", (0.046, 0.04, 0.042), "orange", extruder, (0, 0, 0))
gear = join("extruder_gear", [cylinder("gear_disc", 0.013, 0.006, "brass", None, (0, 0, 0), axis="X", verts=24),
                              box("gear_tooth", (0.004, 0.006, 0.006), "brass", None, (0, 0, 0.013))])
gear.parent = extruder
gear.location = (0, -0.023, 0)

hotend = empty("hotend", carriage, (0, -0.032, -0.03))
cylinder("hotend_heatsink", 0.011, 0.03, "alu", hotend, (0, 0, 0.012))
box("hotend_block", (0.03, 0.024, 0.016), "alu", hotend, (0, 0, -0.012))
cylinder("hotend_nozzle", 0.006, 0.012, "brass", hotend, (0, 0, -0.026), radius2=0.0012)

fan = empty("fan", carriage, (0.036, -0.032, -0.012))
box("fan_housing", (0.01, 0.042, 0.042), "fan", fan, (0, 0, 0))
blades = [cylinder("fan_hub", 0.007, 0.006, "fan", None, (0, 0, 0), axis="Y")]
for i in range(5):
    a = 2 * math.pi * i / 5
    bpy.ops.mesh.primitive_cube_add(size=1)
    b = bpy.context.active_object
    b.scale = (0.003, 0.034, 0.008)
    b.rotation_euler = (a, 0, 0)
    blades.append(_finish(b, f"blade{i}", "fan", None, (0, math.cos(a + math.pi / 2) * 0.0, 0)))
rotor = join("fan_rotor", blades)
rotor.parent = fan
rotor.location = (0.007, 0, 0)

bpy.ops.object.select_all(action="DESELECT")
bpy.ops.export_scene.gltf(filepath=OUT, export_format="GLB", export_yup=True, export_apply=True,
                          export_cameras=False, export_lights=False)
print("exported", OUT, len(bpy.data.objects), "objects")
