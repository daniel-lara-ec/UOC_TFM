import os
from datetime import datetime, timedelta, timezone

import psycopg2


UTC_MINUS_5 = timezone(timedelta(hours=-5))


def _compute_10m_window_local(now_local):
    # Use 10-minute buckets with a 5-minute offset for schedules: 5,15,25,35,45,55.
    # Example: at 16:05 -> [15:50, 16:00), at 16:15 -> [16:00, 16:10).
    anchor = now_local - timedelta(minutes=5)
    bucket_end = anchor.replace(
        minute=(anchor.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    bucket_start = bucket_end - timedelta(minutes=10)
    return bucket_start, bucket_end


def _ensure_target_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS zc_cliente_mediciones_energia_modelo (
            id BIGSERIAL PRIMARY KEY,
            window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL,
            periodo DATE NOT NULL,
            id_cliente TEXT NOT NULL,
            id_dispositivo TEXT NOT NULL,
            fase INTEGER NOT NULL,
            voltaje_promedio DOUBLE PRECISION,
            corriente_promedio DOUBLE PRECISION,
            potencia_promedio DOUBLE PRECISION,
            factor_potencia_promedio DOUBLE PRECISION,
            frecuencia_promedio DOUBLE PRECISION,
            energia_activa_promedio DOUBLE PRECISION,
            muestras BIGINT NOT NULL,
            fecha_ingesta TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (window_end, id_cliente, id_dispositivo, fase)
        )
        """
    )


def _aggregate_last_10_minutes(
    cur,
    window_start,
    window_end,
    periodo_local,
    fecha_ingesta_local,
):
    cur.execute(
        """
        WITH agg AS (
            SELECT
                id_cliente,
                id_dispositivo,
                fase,
                AVG(voltaje) AS voltaje_promedio,
                AVG(corriente) AS corriente_promedio,
                AVG(potencia) AS potencia_promedio,
                AVG(factor_potencia) AS factor_potencia_promedio,
                AVG(frecuencia) AS frecuencia_promedio,
                AVG(energia_activa) AS energia_activa_promedio,
                COUNT(*) AS muestras
            FROM raw_metricas_energia
            WHERE fecha_medicion >= %s
              AND fecha_medicion < %s
              AND fecha_medicion IS NOT NULL
              AND id_cliente IS NOT NULL
              AND id_dispositivo IS NOT NULL
              AND fase IS NOT NULL
            GROUP BY id_cliente, id_dispositivo, fase
        )
        INSERT INTO zc_cliente_mediciones_energia_modelo (
            window_start,
            window_end,
            periodo,
            id_cliente,
            id_dispositivo,
            fase,
            voltaje_promedio,
            corriente_promedio,
            potencia_promedio,
            factor_potencia_promedio,
            frecuencia_promedio,
            energia_activa_promedio,
            muestras,
            fecha_ingesta
        )
        SELECT
            %s,
            %s,
            %s,
            id_cliente,
            id_dispositivo,
            fase,
            voltaje_promedio,
            corriente_promedio,
            potencia_promedio,
            factor_potencia_promedio,
            frecuencia_promedio,
            energia_activa_promedio,
            muestras,
            %s
        FROM agg
        ON CONFLICT (window_end, id_cliente, id_dispositivo, fase)
        DO UPDATE SET
            periodo = EXCLUDED.periodo,
            voltaje_promedio = EXCLUDED.voltaje_promedio,
            corriente_promedio = EXCLUDED.corriente_promedio,
            potencia_promedio = EXCLUDED.potencia_promedio,
            factor_potencia_promedio = EXCLUDED.factor_potencia_promedio,
            frecuencia_promedio = EXCLUDED.frecuencia_promedio,
            energia_activa_promedio = EXCLUDED.energia_activa_promedio,
            muestras = EXCLUDED.muestras,
            fecha_ingesta = EXCLUDED.fecha_ingesta,
            created_at = EXCLUDED.fecha_ingesta
        RETURNING 1
        """,
        (
            window_start,
            window_end,
            window_start,
            window_end,
            periodo_local,
            fecha_ingesta_local,
        ),
    )

    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def lambda_handler(_event, _context):
    now_local = datetime.now(UTC_MINUS_5)
    window_start, window_end = _compute_10m_window_local(now_local)
    periodo_local = window_start.date()
    fecha_ingesta_local = now_local.replace(second=0, microsecond=0)

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        connect_timeout=5,
        options="-c timezone=America/Bogota",
    )

    try:
        with conn:
            with conn.cursor() as cur:
                _ensure_target_table(cur)
                rows_upserted = _aggregate_last_10_minutes(
                    cur,
                    window_start,
                    window_end,
                    periodo_local,
                    fecha_ingesta_local,
                )
    finally:
        conn.close()

    return {
        "ok": True,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "fecha_ingesta": fecha_ingesta_local.isoformat(),
        "rows_upserted": rows_upserted,
    }
