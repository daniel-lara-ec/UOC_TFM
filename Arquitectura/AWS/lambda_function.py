import json
import os
import importlib
from datetime import datetime, timedelta, timezone


DEFAULT_FECHA_MEDICION_TZ = timezone(timedelta(hours=-5))
_boto3 = importlib.import_module("boto3")
s3_client = _boto3.client("s3")


def _extract_topic(event):
    for key in ("topic", "mqtt_topic", "topic_name"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_event_ts(event):
    raw_ts = event.get("ts")
    if raw_ts is None:
        return datetime.now(timezone.utc)

    try:
        # AWS IoT SQL timestamp() is epoch millis.
        return datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _to_timestamptz(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        # FechaMedicion sin tz se interpreta como hora local del medidor (UTC-5).
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


def _extract_fechas_medicion(event):
    metricas = event.get("metricas", [])
    if isinstance(metricas, dict):
        metricas = [metricas]
    if not isinstance(metricas, list):
        return []

    fechas = []
    for metrica in metricas:
        if not isinstance(metrica, dict):
            continue
        fechas.append(_to_timestamptz(metrica.get("FechaMedicion")))

    return fechas


def _resolve_partition_hour(fechas_medicion):
    if not fechas_medicion:
        return None

    first_fecha = None
    for fecha in fechas_medicion:
        if fecha is not None:
            first_fecha = fecha
            break

    if first_fecha is None:
        return None

    partition_hour = first_fecha.astimezone(DEFAULT_FECHA_MEDICION_TZ).replace(
        minute=0, second=0, microsecond=0
    )

    for fecha_medicion in fechas_medicion:
        if fecha_medicion is None:
            continue
        row_hour = fecha_medicion.astimezone(DEFAULT_FECHA_MEDICION_TZ).replace(
            minute=0, second=0, microsecond=0
        )
        if row_hour != partition_hour:
            raise ValueError(
                "FechaMedicion contains multiple hours in the same IoT payload"
            )

    return partition_hour


def _build_raw_s3_key(raw_prefix, topic, partition_hour, event_ts):
    safe_topic = topic or "unknown_topic"
    if partition_hour is not None:
        return (
            f"{raw_prefix}/{safe_topic}/"
            f"{partition_hour.strftime('%Y/%m/%d/%H')}/"
            f"{int(event_ts.timestamp() * 1000)}.json"
        )

    # Fallback when FechaMedicion is missing.
    event_hour = event_ts.astimezone(DEFAULT_FECHA_MEDICION_TZ).replace(
        minute=0, second=0, microsecond=0
    )
    return (
        f"{raw_prefix}/{safe_topic}/"
        f"{event_hour.strftime('%Y/%m/%d/%H')}/"
        f"{int(event_ts.timestamp() * 1000)}.json"
    )


def _store_payload_in_s3(bucket, key, payload):
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to write raw payload to S3 key '{key}'") from exc


def lambda_handler(event, _context):
    topic = _extract_topic(event)
    event_ts = _extract_event_ts(event)
    payload = json.dumps(event, ensure_ascii=True)
    fechas_medicion = _extract_fechas_medicion(event)
    partition_hour = _resolve_partition_hour(fechas_medicion)
    raw_bucket = os.environ["RAW_BUCKET"]
    raw_prefix = os.environ.get("RAW_PREFIX", "raw").strip("/") or "raw"
    raw_key = _build_raw_s3_key(raw_prefix, topic, partition_hour, event_ts)

    _store_payload_in_s3(raw_bucket, raw_key, payload)

    return {
        "ok": True,
        "metric_rows_received": len(fechas_medicion),
        "raw_s3_key": raw_key,
        "topic": topic,
    }
