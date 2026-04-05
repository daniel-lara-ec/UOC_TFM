import os
from datetime import datetime, timedelta, timezone

import psycopg2

DEFAULT_FECHA_MEDICION_TZ = timezone(timedelta(hours=-5))


def _extract_topic(event):
    for key in ("topic", "mqtt_topic", "topic_name"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_ids(event, topic):
    id_cliente = event.get("id_cliente")
    id_dispositivo = event.get("id_dispositivo")

    if topic:
        parts = topic.split("/")
        if len(parts) >= 4 and parts[0] == "energia":
            id_cliente = id_cliente or parts[1]
            id_dispositivo = id_dispositivo or parts[2]

    return id_cliente, id_dispositivo


def _extract_event_ts(event):
    raw_ts = event.get("ts")
    if raw_ts is None:
        return datetime.now(timezone.utc)

    try:
        return datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_timestamptz(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return (
            value if value.tzinfo else value.replace(tzinfo=DEFAULT_FECHA_MEDICION_TZ)
        )

    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 1e12:
            epoch = epoch / 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=DEFAULT_FECHA_MEDICION_TZ)
            return parsed
        except ValueError:
            try:
                return _to_timestamptz(float(text))
            except ValueError:
                return None

    return None


def _extract_metric_rows(event, event_ts, topic, id_cliente, id_dispositivo):
    metricas = event.get("metricas", [])
    if isinstance(metricas, dict):
        metricas = [metricas]
    if not isinstance(metricas, list):
        return []

    rows = []
    for metrica in metricas:
        if not isinstance(metrica, dict):
            continue
        rows.append(
            (
                event_ts,
                _to_timestamptz(metrica.get("FechaMedicion")),
                topic,
                id_cliente,
                id_dispositivo,
                _to_int(metrica.get("Fase")),
                _to_float(metrica.get("Voltaje")),
                _to_float(metrica.get("Corriente")),
                _to_float(metrica.get("Potencia")),
                _to_float(metrica.get("FactorPotencia")),
                _to_float(metrica.get("Frecuencia")),
                _to_float(metrica.get("EnergiaActiva")),
            )
        )
    return rows


def _ensure_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_metricas_energia (
            id BIGSERIAL PRIMARY KEY,
            event_ts TIMESTAMPTZ NOT NULL,
            fecha_medicion TIMESTAMPTZ,
            mqtt_topic TEXT,
            id_cliente TEXT,
            id_dispositivo TEXT,
            fase INTEGER,
            voltaje DOUBLE PRECISION,
            corriente DOUBLE PRECISION,
            potencia DOUBLE PRECISION,
            factor_potencia DOUBLE PRECISION,
            frecuencia DOUBLE PRECISION,
            energia_activa DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def lambda_handler(event, _context):
    topic = _extract_topic(event)
    id_cliente, id_dispositivo = _extract_ids(event, topic)
    event_ts = _extract_event_ts(event)
    metric_rows = _extract_metric_rows(
        event, event_ts, topic, id_cliente, id_dispositivo
    )

    if not metric_rows:
        return {
            "ok": True,
            "metric_rows_inserted": 0,
            "id_cliente": id_cliente,
            "id_dispositivo": id_dispositivo,
        }

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        connect_timeout=5,
    )

    try:
        with conn:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.executemany(
                    """
                    INSERT INTO raw_metricas_energia (
                        event_ts,
                        fecha_medicion,
                        mqtt_topic,
                        id_cliente,
                        id_dispositivo,
                        fase,
                        voltaje,
                        corriente,
                        potencia,
                        factor_potencia,
                        frecuencia,
                        energia_activa
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    metric_rows,
                )
    finally:
        conn.close()

    return {
        "ok": True,
        "metric_rows_inserted": len(metric_rows),
        "id_cliente": id_cliente,
        "id_dispositivo": id_dispositivo,
    }
