# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0.

from awsiot import mqtt5_client_builder
from awscrt import mqtt5
import argparse
import threading
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------- ENVIRONMENT LOADING -----------------------------------------
import uuid

try:
    import pyodbc
except ImportError:
    pyodbc = None


def load_env_file(env_path: str = ".env"):
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def get_env(name: str, default=None, required: bool = False):
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def get_env_str(name: str, default: str = "", required: bool = False) -> str:
    value = get_env(name, default=default, required=required)
    return str(value)


def get_env_int(name: str, default: int) -> int:
    return int(str(get_env(name, default=str(default))))


def get_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


load_env_file()


input_endpoint = get_env_str("MQTT_ENDPOINT", required=True)
input_cert = get_env("MQTT_CERT_PATH")
input_key = get_env("MQTT_KEY_PATH")
input_client_id = get_env_str(
    "MQTT_CLIENT_ID", default=f"mqtt5-sample-{uuid.uuid4().hex[:8]}"
)
input_message = get_env_str("MQTT_MESSAGE", default="Hello from mqtt5 sample")
cliente_prefix = get_env_str("CLIENTE_PREFIX", default="cliente")
cliente_start = get_env_int("CLIENTE_START", default=100)
default_client_count = get_env_int("REQUEST_COUNT", default=200)
default_parallel_clients = get_env_int("SIMULTANEOUS_REQUESTS", default=10)
id_dispositivo = get_env_str("ID_DISPOSITIVO", required=True)

generic_cert_path = get_env_str(
    "MQTT_GENERIC_CERT_PATH", default="certs/generic_device_cert.pem"
)
generic_key_path = get_env_str(
    "MQTT_GENERIC_KEY_PATH", default="certs/generic_device_key.pem"
)
generic_public_key_path = get_env_str(
    "MQTT_GENERIC_PUBLIC_KEY_PATH", default="certs/generic_device_public_key.pem"
)
generic_cert_metadata_path = get_env_str(
    "MQTT_GENERIC_CERT_METADATA_PATH", default="certs/generic_device_metadata.json"
)
iot_policy_name = get_env("IOT_POLICY_NAME")
iot_thing_name = get_env("IOT_THING_NAME")

mssql_server = get_env("MSSQL_SERVER")
mssql_database = get_env("MSSQL_DATABASE")
mssql_user = get_env("MSSQL_USER")
mssql_password = get_env("MSSQL_PASSWORD")
mssql_driver = get_env("MSSQL_DRIVER", default="ODBC Driver 18 for SQL Server")

MSSQL_QUERY_EXACT = """
SELECT [FechaMedicion]
    ,[Fase]
    ,[Voltaje]
        ,[Corriente]
        ,[Potencia]
        ,[FactorPotencia]
        ,[Frecuencia]
        ,[EnergiaActiva]
    FROM [IoTData].[dbo].[Vw_IndicadoresEnergia]
 WHERE FechaMedicion = ?
"""

TIMEOUT = 100
message_string = input_message


def fetch_mssql_payload() -> list:
    required_params = {
        "MSSQL_SERVER": mssql_server,
        "MSSQL_DATABASE": mssql_database,
        "MSSQL_USER": mssql_user,
        "MSSQL_PASSWORD": mssql_password,
    }
    missing = [name for name, value in required_params.items() if not value]
    if missing:
        raise ValueError("Missing MSSQL parameters: {}".format(", ".join(missing)))

    if pyodbc is None:
        raise ImportError(
            "pyodbc is not installed. Install it with: python3 -m pip install pyodbc"
        )

    connection_string = (
        f"DRIVER={{{mssql_driver}}};"
        f"SERVER={mssql_server};"
        f"DATABASE={mssql_database};"
        f"UID={mssql_user};"
        f"PWD={mssql_password};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )

    connection = pyodbc.connect(connection_string, timeout=10)
    try:
        # Use current clock minute minus 2 minutes, with seconds set to 00.
        current_measurement_time = datetime.now().replace(
            second=0, microsecond=0
        ) - timedelta(minutes=2)
        print(
            f"MSSQL query timestamp: {current_measurement_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        cursor = connection.cursor()
        cursor.execute(MSSQL_QUERY_EXACT, current_measurement_time)
        rows = cursor.fetchall()

        if not rows:
            raise ValueError("MSSQL query returned no rows for exact lookup")

        columns = [column[0] for column in cursor.description]
        payload = [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()

    return payload


def ensure_generic_certificate(
    cert_path: str,
    key_path: str,
    public_key_path: str,
    metadata_path: str,
    policy_name: str | None = None,
    thing_name: str | None = None,
) -> tuple[str, str]:
    cert_file = Path(cert_path)
    key_file = Path(key_path)
    public_key_file = Path(public_key_path)
    metadata_file = Path(metadata_path)

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    public_key_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.parent.mkdir(parents=True, exist_ok=True)

    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    command = [
        "aws",
        "iot",
        "create-keys-and-certificate",
        "--set-as-active",
        "--certificate-pem-outfile",
        str(cert_file),
        "--public-key-outfile",
        str(public_key_file),
        "--private-key-outfile",
        str(key_file),
        "--output",
        "json",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    command_output = json.loads(result.stdout)

    with open(metadata_file, "w", encoding="utf-8") as metadata_output:
        json.dump(command_output, metadata_output, ensure_ascii=True, indent=2)

    cert_arn = command_output.get("certificateArn")

    if policy_name and cert_arn:
        subprocess.run(
            [
                "aws",
                "iot",
                "attach-policy",
                "--policy-name",
                str(policy_name),
                "--target",
                str(cert_arn),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    if thing_name and cert_arn:
        subprocess.run(
            [
                "aws",
                "iot",
                "attach-thing-principal",
                "--thing-name",
                str(thing_name),
                "--principal",
                str(cert_arn),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    print(f"Generated AWS IoT certificate: {cert_file}")
    print(f"Generated AWS IoT private key: {key_file}")
    print(f"Generated AWS IoT public key: {public_key_file}")
    print(f"Certificate metadata saved at: {metadata_file}")
    if policy_name:
        print(f"Attached policy '{policy_name}' to certificate")
    if thing_name:
        print(f"Attached thing '{thing_name}' to certificate")
    return str(cert_file), str(key_file)


# Callback for the lifecycle event Stopped
def create_connected_mqtt_client(
    endpoint: str,
    cert_path: str,
    key_path: str,
    mqtt_client_id_value: str,
) -> tuple[mqtt5.Client, threading.Event]:
    connection_success_event = threading.Event()
    stop_signal_event = threading.Event()

    def on_lifecycle_stopped(_lifecycle_stopped_data: mqtt5.LifecycleStoppedData):
        stop_signal_event.set()

    def on_lifecycle_attempting_connect(
        _lifecycle_attempting_connect_data: mqtt5.LifecycleAttemptingConnectData,
    ):
        print(f"[{mqtt_client_id_value}] connecting to {endpoint}")

    def on_lifecycle_connection_success(
        lifecycle_connect_success_data: mqtt5.LifecycleConnectSuccessData,
    ):
        connack_packet = lifecycle_connect_success_data.connack_packet
        print(f"[{mqtt_client_id_value}] connected: {repr(connack_packet.reason_code)}")
        connection_success_event.set()

    def on_lifecycle_connection_failure(
        lifecycle_connection_failure: mqtt5.LifecycleConnectFailureData,
    ):
        print(
            f"[{mqtt_client_id_value}] connection failed: {lifecycle_connection_failure.exception}"
        )

    def on_lifecycle_disconnection(
        lifecycle_disconnect_data: mqtt5.LifecycleDisconnectData,
    ):
        disconnect_reason = (
            lifecycle_disconnect_data.disconnect_packet.reason_code
            if lifecycle_disconnect_data.disconnect_packet
            else "None"
        )
        print(f"[{mqtt_client_id_value}] disconnected: {disconnect_reason}")

    client = mqtt5_client_builder.mtls_from_path(
        endpoint=endpoint,
        cert_filepath=cert_path,
        pri_key_filepath=key_path,
        on_lifecycle_stopped=on_lifecycle_stopped,
        on_lifecycle_attempting_connect=on_lifecycle_attempting_connect,
        on_lifecycle_connection_success=on_lifecycle_connection_success,
        on_lifecycle_connection_failure=on_lifecycle_connection_failure,
        on_lifecycle_disconnection=on_lifecycle_disconnection,
        client_id=mqtt_client_id_value,
    )

    client.start()
    if not connection_success_event.wait(TIMEOUT):
        raise TimeoutError(f"Connection timeout for client {mqtt_client_id_value}")

    return client, stop_signal_event


def publish_one_message(
    endpoint: str,
    cert_path: str,
    key_path: str,
    mqtt_client_id_value: str,
    message_topic_value: str,
    payload_message: str,
) -> tuple[str, str, str | None, str | None]:
    client = None
    stop_signal_event = None

    try:
        client, stop_signal_event = create_connected_mqtt_client(
            endpoint=endpoint,
            cert_path=cert_path,
            key_path=key_path,
            mqtt_client_id_value=mqtt_client_id_value,
        )

        publish_future = client.publish(
            mqtt5.PublishPacket(
                topic=message_topic_value,
                payload=payload_message,
                qos=mqtt5.QoS.AT_LEAST_ONCE,
            )
        )
        publish_result = publish_future.result(TIMEOUT)
        return (
            mqtt_client_id_value,
            message_topic_value,
            repr(publish_result.puback.reason_code),
            None,
        )
    except (TimeoutError, RuntimeError, OSError, ValueError) as ex:
        return mqtt_client_id_value, message_topic_value, None, str(ex)
    finally:
        if client is not None:
            client.stop()
            if stop_signal_event is not None:
                stop_signal_event.wait(TIMEOUT)


def publish_stress_messages(
    endpoint: str,
    cert_path: str,
    key_path: str,
    payload_message: str,
    clientes_count: int,
    concurrent_clients: int,
):
    publish_success_count = 0
    publish_failures = []

    with ThreadPoolExecutor(max_workers=concurrent_clients) as executor:
        futures = []
        for index in range(1, clientes_count + 1):
            cliente_num = cliente_start + index
            cliente_id = f"{cliente_prefix}_{cliente_num}"
            mqtt_client_id = f"{input_client_id}-{cliente_num}"
            message_topic = f"energia/{cliente_id}/{id_dispositivo}/metricas"
            futures.append(
                executor.submit(
                    publish_one_message,
                    endpoint,
                    cert_path,
                    key_path,
                    mqtt_client_id,
                    message_topic,
                    payload_message,
                )
            )

        for future in as_completed(futures):
            client_id, message_topic, reason_code, error_text = future.result()
            if error_text is None:
                publish_success_count += 1
                print(f"[OK] {client_id} {message_topic} -> {reason_code}")
            else:
                publish_failures.append((message_topic, error_text))
                print(f"[FAIL] {client_id} {message_topic}: {error_text}")

    return publish_success_count, publish_failures


def parse_runtime_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MQTT stress test publishing same payload to multiple cliente_xxx topics"
    )
    parser.add_argument(
        "--clientes",
        type=int,
        default=default_client_count,
        help="Cantidad de clientes a simular (default: REQUEST_COUNT o 200)",
    )
    parser.add_argument(
        "--simultaneas",
        type=int,
        default=default_parallel_clients,
        help="Cantidad de peticiones simultaneas a ejecutar en paralelo (default: 10)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    print("\nStarting MQTT5 X509 PubSub Sample\n")
    args = parse_runtime_args()
    if args.clientes <= 0:
        raise ValueError("--clientes debe ser mayor a 0")
    if args.simultaneas <= 0:
        raise ValueError("--simultaneas debe ser mayor a 0")

    parallel_clients = max(10, args.simultaneas)

    measurement_payload = None

    use_mssql = all([mssql_server, mssql_database, mssql_user, mssql_password])

    if use_mssql:
        print("==== Querying MSSQL ====")
        try:
            measurement_payload = fetch_mssql_payload()
            print(f"MSSQL payload ready: {measurement_payload}")
        except ValueError as sql_data_error:
            print(f"MSSQL data fallback: {sql_data_error}")
            measurement_payload = None

    cert_to_use = input_cert
    key_to_use = input_key

    if cert_to_use and not key_to_use:
        raise ValueError("MQTT_KEY_PATH is required when MQTT_CERT_PATH is defined")

    if key_to_use and not cert_to_use:
        raise ValueError("MQTT_CERT_PATH is required when MQTT_KEY_PATH is defined")

    if not cert_to_use and not key_to_use:
        cert_to_use, key_to_use = ensure_generic_certificate(
            cert_path=generic_cert_path,
            key_path=generic_key_path,
            public_key_path=generic_public_key_path,
            metadata_path=generic_cert_metadata_path,
            policy_name=iot_policy_name,
            thing_name=iot_thing_name,
        )

    if cert_to_use is None or key_to_use is None:
        raise ValueError("No se pudo determinar el certificado y la clave MQTT")

    if use_mssql:
        metricas = measurement_payload if isinstance(measurement_payload, list) else []
    else:
        metricas = {
            "mensaje": message_string,
        }

    body = {
        "metricas": metricas,
    }

    message = json.dumps(body, ensure_ascii=True, default=str)
    print(
        f"==== Running stress test: {args.clientes} publishes from {cliente_prefix}_{cliente_start + 1} "
        f"to {cliente_prefix}_{cliente_start + args.clientes} ===="
    )

    total_success, total_failures = publish_stress_messages(
        endpoint=input_endpoint,
        cert_path=cert_to_use,
        key_path=key_to_use,
        payload_message=message,
        clientes_count=args.clientes,
        concurrent_clients=parallel_clients,
    )

    print("==== Stress test finished ====")
    print(f"Successful publishes: {total_success}/{args.clientes}")
    if total_failures:
        print(f"Failed publishes: {len(total_failures)}")
        for failed_topic, reason in total_failures:
            print(f" - {failed_topic}: {reason}")
