import paho.mqtt.client as mqtt
import json
import time
import logging
from datetime import datetime, timezone

BROKER_HOST = "localhost"
BROKER_PORT = 1883
PRINTER_ID = "printer_01"

tracker = {}

# Set up logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("telemetry.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def on_connect(client, userdata, flags, return_code, properties=None):
    if return_code == 0:
        logging.info(f"[SUBSCRIBER] Successfully connected to the broker")

        # Subscribe to telemetry from ALL printers in the factory
        client.subscribe("factory/+/telemetry/#", qos=1)
        client.subscribe("factory/+/status", qos=1)

        # Subscribe to all telemetry and status topics of the given printer
        #client.subscribe(f"factory/{PRINTER_ID}/telemetry/#", qos=1)
        #client.subscribe(f"factory/{PRINTER_ID}/status", qos=1)\
        #logging.info(f"[SUBSCRIBER] Subscribed to topics: factory/{PRINTER_ID}/#")

        ## QoS test =====================================
        #client.subscribe(f"factory/{PRINTER_ID}/test/+", qos=2)
        ## ==========================================
    else:
        logging.error(f"[SUBSCRIBER] Connection failed with code: {return_code}")

def on_message(client, userdata, message):
    topic = message.topic
    payload_raw = message.payload.decode("utf-8")

    try:
        data = json.loads(payload_raw)

        ## QoS test =====================================
        #if "test/qos" in topic:
        #    logging.info(
        #        f"[QoS {message.qos} RECEIVED] Topic: {topic} | "
        #        f"Test ID: {data.get('test_id')} | "
        #        f"Target QoS: {data.get('qos_level')} | "
        #        f"Timestamp: {data.get('timestamp')}"
        #    )
        #    return
        ## ==========================================

        # Check LWT / Status
        if "status" in topic:
            printer_name = topic.split('/')[1]
            if data.get("status") == "OFFLINE":
                logging.warning(f"[LWT ALERT] Printer {printer_name} LOST! Reason: {data.get('reason')}")
            else:
                logging.info(f"[STATUS] Printer {printer_name} is {data.get('status')}")
            return

        # JSON telemetry structure validation
        required_fields = ["sensor_id", "seq_id", "value", "unit", "timestamp"]
        if not all(field in data for field in required_fields):
            logging.info(f"[VALIDATION ERROR] Message from {topic} is missing required fields")
            return

        seq_id = data["seq_id"]
        # Evaluate packet loss for this specific topic
        if topic not in tracker:
            tracker[topic] = {"last_seq": seq_id, "lost_count": 0}
        else:
            expected_seq = tracker[topic]["last_seq"] + 1
            if seq_id > expected_seq:
                missed = seq_id - expected_seq
                tracker[topic]["lost_count"] += missed
                logging.warning(f"[PACKET LOSS] {topic}: Lost {missed} messages! (Expected {expected_seq}, got {seq_id})")
            tracker[topic]["last_seq"] = seq_id

        # Evaluate Latency
        send_time = datetime.fromisoformat(data["timestamp"])
        receive_time = datetime.now(timezone.utc)
        latency_ms = (receive_time - send_time).total_seconds() * 1000.0

        # Format and print log
        time_str = data['timestamp'][11:23]
        metric_name = topic.split('/')[-1]

        logging.info(
            f"[{time_str}] [{metric_name}] ID: {seq_id} | "
            f"Sensor: {data['sensor_id']} | "
            f"Value: {data['value']} {data['unit']} | "
            f"Latency: {latency_ms:.2f} ms | "
            f"Lost Total: {tracker[topic]['lost_count']}"
        )

    except json.JSONDecodeError:
        logging.error(f"[ERROR] Failed to parse JSON from topic {topic}: {payload_raw}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                     "Factory_Central_Subscriber",
                     clean_session=False)
client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER_HOST, BROKER_PORT)
client.loop_start()

try:
    logging.info("[SUBSCRIBER] Listening for telemetry (Ctrl+C to exit)...")
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    logging.info("[SUBSCRIBER] Shutting down...\n")
finally:
    client.disconnect()
    client.loop_stop()