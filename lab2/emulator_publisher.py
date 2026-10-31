import paho.mqtt.client as mqtt
import time
import json
import random
import math
from datetime import datetime, timezone
import sys

BROKER_HOST = "localhost"
BROKER_PORT = 1883
# Get printer ID from command-line argument, or default to printer_01
PRINTER_ID = sys.argv[1] if len(sys.argv) > 1 else "printer_01"

# Base physical states (Target values)
base_state = {
    "nozzle_temperature": 210.0,
    "bed_temperature": 60.0,
    "chamber_temperature": 40.0,
    "extruder_feed_rate": 50.0, # mm/s
    "fan_speed_percent": 80.0,
    "stepper_current_x": 1.2
}

def on_connect(client, userdata, flags, return_code, properties=None):
    if return_code == 0:
        print(f"[PUBLISHER] Connected to MQTT broker {BROKER_HOST}:{BROKER_PORT}")
    else:
        print(f"[PUBLISHER] Connection failed with code: {return_code}")

# Create client and configure LWT (Last Will and Testament)
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, f"Publisher_{PRINTER_ID}", clean_session=False)
client.on_connect = on_connect

# Configure Last Will: if the publisher crashes, the broker will send this message
LWT_TOPIC = f"factory/{PRINTER_ID}/status"
client.will_set(LWT_TOPIC, payload=json.dumps({"status": "OFFLINE", "reason": "unexpected_disconnect"}), qos=1, retain=True)

client.connect(BROKER_HOST, BROKER_PORT)
client.loop_start()
# Notify about successful startup
client.publish(LWT_TOPIC, json.dumps({"status": "ONLINE"}), qos=1, retain=True)

## ==========================================
## -- Test QoS levels --
#def test_qos_levels(client, target_qos=1, count=10):
#    """
#    Helper function to test and analyze MQTT QoS levels (0, 1, 2)
#    """
#    print(f"\n--- [QoS TEST] Starting QoS {target_qos} test ({count} messages) ---")
#    test_topic = f"factory/{PRINTER_ID}/test/qos_{target_qos}"
#
#    for i in range(1, count + 1):
#        payload = json.dumps({
#            "test_id": i,
#            "qos_level": target_qos,
#            "timestamp": datetime.now(timezone.utc).isoformat()
#        })
#
#        # Publish payload and retrieve the MQTTMessageInfo object
#        info = client.publish(test_topic, payload, qos=target_qos)
#
#        # For QoS 1 and 2, wait for acknowledgment (PUBACK / PUBCOMP)
#        if target_qos > 0:
#            try:
#                info.wait_for_publish(timeout=2.0)
#            except RuntimeError as e:
#                print(f"⚠️ [QoS {target_qos}] Connection error during publish: {e}")
#
#        # Safely check publication status
#        try:
#            if info.is_published():
#                print(f"[QoS {target_qos}] Message #{i} confirmed by broker.")
#            else:
#                print(f"[QoS {target_qos}] Message #{i} pending/not confirmed.")
#        except RuntimeError:
#            print(f"[QoS {target_qos}] Message #{i} failed: Client disconnected.")
#
#        time.sleep(1.0) # 1-second interval to allow enough time for testing broker restarts
#    print(f"--- [QoS TEST] Completed QoS {target_qos} test ---\n")
#
#test_qos_levels(client, target_qos=0, count=10)
#test_qos_levels(client, target_qos=1, count=10)
#test_qos_levels(client, target_qos=2, count=10)
#
#while True:
#    time.sleep(1)
## ==========================================

step = 0
seq_counters = {} # Counter for message sequence numbers

def publish_metric(client, metric_path, sensor_id, value, unit, qos):
    topic = f"factory/{PRINTER_ID}/telemetry/{metric_path}"
    if topic not in seq_counters:
        seq_counters[topic] = 0
    seq_counters[topic] += 1

    payload = json.dumps({
        "sensor_id": sensor_id,
        "seq_id": seq_counters[topic],
        "value": value,
        "unit": unit,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    client.publish(topic, payload, qos=qos)


try:
    print("[PUBLISHER] Starting telemetry generation and publishing...")
    while True:
        step += 1

        # --- HIGH-FREQUENCY METRICS (10 Hz — executed every 0.1s cycle - QoS 0) ---
        # Extruder Feed Rate: change based on phase of print job (simulate different stages of printing)
        # it not work if we use Liniar advance
        phase = (step // 50) % 4  # each phase lasts 5 seconds
        target_feed = [50.0, 120.0, 0.0, 60.0][phase]
        feed_rate = max(0.0, min(150.0, target_feed + random.gauss(0, 1.2) if target_feed > 0 else 0.0))

        # Stepper Current X: 0.8-1.8 A + noise ±0.05 + anomaly spike
        current_x = base_state["stepper_current_x"] + random.gauss(0, 0.02)
        is_anomaly = (step % 300 == 0) # Anomaly every 30 sec
        if is_anomaly:
            current_x += 0.5  # Simulate jamming / skipped steps
            print(f" [ANOMALY] X-axis motor current spike: {current_x:.2f} A")
        current_x = max(0.8, min(1.8, current_x))

        publish_metric(client, "extruder_feed_rate", "feed_rate_01", round(feed_rate, 2), "mm/s", qos=0)
        publish_metric(client, "stepper_current_x", "current_x_01", round(current_x, 3), "A", qos=0)


        # --- LOW-FREQUENCY METRICS (1 Hz — executed every 10 steps / 1 second) ---
        if step % 10 == 0:
            # Thermal trends (sinusoidal fluctuations around the operating point)
            wave = math.sin(step / 50.0)

            # Nozzle Temp: 180-320 °C, noise ±0.5 °C
            nozzle_temp = base_state["nozzle_temperature"] + wave * 2.0 + random.gauss(0, 0.25)
            is_anomaly = (step % 50 == 0) # Anomaly every 50 sec
            if is_anomaly:
                nozzle_temp += 35.0  # Simulate a sudden thermal runaway
                print(f" [ANOMALY] Nozzle temperature spike: {nozzle_temp:.2f} °C")
            nozzle_temp = max(180.0, min(320.0, nozzle_temp))

            bed_temp = max(30.0, min(110.0, base_state["bed_temperature"] + wave * 0.5 + random.gauss(0, 0.5)))
            chamber_temp = max(30.0, min(70.0, base_state["chamber_temperature"] + random.gauss(0, 0.5)))
            fan_speed = max(0.0, min(100.0, base_state["fan_speed_percent"] + random.gauss(0, 0.5)))

            # Publish low-frequency metrics (QoS 1, but QoS 0 is also acceptable for telemetry)
            publish_metric(client, "nozzle_temperature", "nozzle_temp_01", round(nozzle_temp, 2), "Celsius", qos=1)
            publish_metric(client, "bed_temperature", "bed_temp_01", round(bed_temp, 2), "Celsius", qos=1) # або окремий путін
            publish_metric(client, "chamber_temperature", "chamber_temp_01", round(chamber_temp, 2), "Celsius", qos=1)
            publish_metric(client, "fan_speed_percent", "fan_speed_01", round(fan_speed, 1), "%", qos=1)

        time.sleep(0.1) # 100 ms delay matches the 10 Hz frequency

except KeyboardInterrupt:
    print("[PUBLISHER] Stopped by user...\n")
finally:
    # Graceful shutdown
    client.publish(LWT_TOPIC, json.dumps({"status": "OFFLINE", "reason": "normal_shutdown"}), qos=1, retain=True)
    client.disconnect()
    client.loop_stop()
    print("[PUBLISHER] Work completed.")

