import os

import psycopg2


LOCAL_TIMEZONE = "America/Bogota"  # UTC-5


def _table_exists(cur, table_name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and row[0] is not None)


def _delete_old_rows(cur):
    # Keep only one week of operational data in RDS.
    cur.execute(
        """
        DELETE FROM raw_metricas_energia
        WHERE COALESCE(fecha_medicion, event_ts, created_at) < (
            (timezone(%s, NOW()) - INTERVAL '7 days') AT TIME ZONE %s
        )
        """,
        (LOCAL_TIMEZONE, LOCAL_TIMEZONE),
    )
    metricas_deleted = cur.rowcount

    return metricas_deleted


def _delete_old_model_rows(cur):
    # Keep one week of aggregated model data using the partition key (periodo).
    model_table_exists = _table_exists(cur, "zc_cliente_mediciones_energia_modelo")

    if not model_table_exists:
        return 0

    cur.execute(
        """
        DELETE FROM zc_cliente_mediciones_energia_modelo
        WHERE periodo < ((timezone(%s, NOW()))::date - 7)
        """,
        (LOCAL_TIMEZONE,),
    )
    return cur.rowcount


def _optimize_tables(cur):
    # Indexes support both operational queries and retention deletes.
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_metricas_energia_fecha_event
        ON raw_metricas_energia (fecha_medicion, event_ts)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_metricas_energia_cliente_disp_fecha
        ON raw_metricas_energia (id_cliente, id_dispositivo, fecha_medicion)
        """
    )

    model_table_exists = _table_exists(cur, "zc_cliente_mediciones_energia_modelo")

    if model_table_exists:
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_zc_cliente_mediciones_modelo_periodo
            ON zc_cliente_mediciones_energia_modelo (periodo)
            """
        )


def lambda_handler(event, _context):
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
    )

    try:
        with conn:
            with conn.cursor() as cur:
                _optimize_tables(cur)
                metricas_deleted = _delete_old_rows(cur)
                modelo_deleted = _delete_old_model_rows(cur)

        with conn.cursor() as cur:
            conn.autocommit = True
            cur.execute("ANALYZE raw_metricas_energia")
            model_table_exists = _table_exists(
                cur, "zc_cliente_mediciones_energia_modelo"
            )
            if model_table_exists:
                cur.execute("ANALYZE zc_cliente_mediciones_energia_modelo")
    finally:
        conn.close()

    return {
        "ok": True,
        "message": "RDS cleanup completed",
        "raw_metricas_deleted": metricas_deleted,
        "zc_modelo_deleted": modelo_deleted,
        "retention_days": 7,
        "event": event if isinstance(event, dict) else {},
    }
