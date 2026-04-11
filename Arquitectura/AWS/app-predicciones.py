import os
from datetime import datetime, timedelta, timezone
import sys
import traceback

import pandas as pd
import psycopg2
import torch
from chronos import Chronos2Pipeline


RDS_TABLE = "zc_cliente_predicciones_horaria"
SOURCE_TABLE = "zc_cliente_mediciones_energia_modelo"
MODEL_ID = "autogluon/chronos-2-small"
TZ_UTC_MINUS_5 = timezone(timedelta(hours=-5))
FREQ = "10min"
PREDICTION_LENGTH = 6
MIN_POINTS_REQUIRED = 504  # 3.5 dias * 24 horas * 6 puntos por hora


def get_connection():
    return psycopg2.connect(
        host=os.getenv(
            "RDS_HOST",
            "uoc-tfm-matias-lara-dev-db.cahgs2aaisut.us-east-1.rds.amazonaws.com",
        ),
        database=os.getenv("RDS_DB", "iotdb"),
        user=os.getenv("RDS_USER", "iotadmin"),
        password=os.getenv("RDS_PASSWORD", "qpanoq201ndlw"),
        port=os.getenv("RDS_PORT", "5432"),
    )


def ensure_table(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RDS_TABLE} (
            id SERIAL PRIMARY KEY,
            id_cliente VARCHAR(100) NOT NULL,
            id_dispositivo VARCHAR(100) NOT NULL,
            fase VARCHAR(10),
            fecha_prediccion TIMESTAMP NOT NULL,
            valor_predicho FLOAT NOT NULL,
            intervalo_0_1 FLOAT,
            intervalo_0_9 FLOAT,
            fecha_proceso TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT unique_prediccion UNIQUE(id_cliente, id_dispositivo, fase, fecha_prediccion)
        )
        """
    )
    cursor.execute(
        f"""
        ALTER TABLE {RDS_TABLE}
        ADD COLUMN IF NOT EXISTS intervalo_0_1 FLOAT,
        ADD COLUMN IF NOT EXISTS intervalo_0_9 FLOAT
        """
    )


def load_source_data(conn, start_dt, end_dt):
    query = f"""
    SELECT DISTINCT
        zcmem.id_cliente,
        zcmem.id_dispositivo,
        zcmem.fase,
        zcmem.window_start AS fecha_hora,
        zcmem.potencia_promedio
    FROM {SOURCE_TABLE} zcmem
    WHERE zcmem.window_start >= %(start_dt)s
      AND zcmem.window_start <= %(end_dt)s
    ORDER BY zcmem.id_cliente, zcmem.id_dispositivo, zcmem.fase, zcmem.window_start
    """
    return pd.read_sql_query(
        query, conn, params={"start_dt": start_dt, "end_dt": end_dt}
    )


def prepare_dataframe(df):
    df = df.copy()
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])

    if df["fecha_hora"].dt.tz is None:
        df["fecha_hora"] = (
            df["fecha_hora"].dt.tz_localize("UTC").dt.tz_convert(TZ_UTC_MINUS_5)
        )
    else:
        df["fecha_hora"] = df["fecha_hora"].dt.tz_convert(TZ_UTC_MINUS_5)

    df["serie_id"] = (
        df["id_cliente"].astype(str)
        + "-"
        + df["id_dispositivo"].astype(str)
        + "-"
        + df["fase"].astype(str)
    )

    df = df.rename(
        columns={"fecha_hora": "FechaMedicion", "potencia_promedio": "Potencia"}
    )

    normalized_series = []

    for serie_id, serie_df in df.groupby("serie_id", sort=False):
        serie_df = serie_df.sort_values("FechaMedicion").copy()
        serie_df = serie_df.set_index("FechaMedicion")

        serie_df["Potencia"] = pd.to_numeric(serie_df["Potencia"], errors="coerce")
        serie_df = serie_df.groupby(level=0).mean(numeric_only=True)
        serie_df = serie_df.asfreq(FREQ)
        serie_df["Potencia"] = serie_df["Potencia"].interpolate(
            method="time", limit_direction="both"
        )

        source_row = df.loc[df["serie_id"] == serie_id].iloc[0]
        serie_df["id_cliente"] = source_row["id_cliente"]
        serie_df["id_dispositivo"] = source_row["id_dispositivo"]
        serie_df["fase"] = source_row["fase"]
        serie_df["serie_id"] = serie_id

        serie_df = serie_df.reset_index().rename(columns={"index": "FechaMedicion"})
        normalized_series.append(serie_df)

    df = pd.concat(normalized_series, ignore_index=True)
    df = df.sort_values(["serie_id", "FechaMedicion"]).reset_index(drop=True)
    return df


def build_future_df(execution_dt, serie_id):
    base_hour = execution_dt.replace(minute=0, second=0, microsecond=0)
    future_dates = pd.date_range(
        start=base_hour + timedelta(minutes=10),
        periods=PREDICTION_LENGTH,
        freq=FREQ,
    )
    return pd.DataFrame({"FechaMedicion": future_dates, "serie_id": serie_id})


def predict_by_series(df, pipeline, execution_dt):
    predictions = []

    for serie_id, serie_df in df.groupby("serie_id", sort=False):
        serie_df = serie_df[
            [
                "id_cliente",
                "id_dispositivo",
                "fase",
                "serie_id",
                "FechaMedicion",
                "Potencia",
            ]
        ].copy()

        if len(serie_df) < MIN_POINTS_REQUIRED:
            print(
                f"Serie omitida por datos insuficientes (<{MIN_POINTS_REQUIRED}): {serie_id}"
            )
            continue

        id_cliente = serie_df["id_cliente"].iloc[0]
        id_dispositivo = serie_df["id_dispositivo"].iloc[0]
        fase = str(serie_df["fase"].iloc[0])

        train_df = serie_df[["serie_id", "FechaMedicion", "Potencia"]].copy()
        future_df = build_future_df(execution_dt, serie_id)

        pred_df = pipeline.predict_df(
            train_df,
            future_df=future_df,
            prediction_length=PREDICTION_LENGTH,
            quantile_levels=[0.1, 0.5, 0.9],
            id_column="serie_id",
            timestamp_column="FechaMedicion",
            target="Potencia",
            validate_inputs=False,
        )

        value_col = "0.5" if "0.5" in pred_df.columns else "predictions"
        low_col = "0.1" if "0.1" in pred_df.columns else value_col
        high_col = "0.9" if "0.9" in pred_df.columns else value_col
        fecha_proceso = datetime.now(TZ_UTC_MINUS_5).replace(tzinfo=None)

        pred_df = pred_df.head(PREDICTION_LENGTH)

        for _, row in pred_df.iterrows():
            predictions.append(
                {
                    "id_cliente": id_cliente,
                    "id_dispositivo": id_dispositivo,
                    "fase": fase,
                    "fecha_prediccion": row["FechaMedicion"]
                    .to_pydatetime()
                    .replace(tzinfo=None),
                    "valor_predicho": float(row[value_col]),
                    "intervalo_0_1": float(row[low_col]),
                    "intervalo_0_9": float(row[high_col]),
                    "fecha_proceso": fecha_proceso,
                }
            )

    return predictions


def save_predictions(cursor, rows):
    upsert_sql = f"""
        INSERT INTO {RDS_TABLE}
        (id_cliente, id_dispositivo, fase, fecha_prediccion, valor_predicho, intervalo_0_1, intervalo_0_9, fecha_proceso)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id_cliente, id_dispositivo, fase, fecha_prediccion)
        DO UPDATE SET
            valor_predicho = EXCLUDED.valor_predicho,
            intervalo_0_1 = EXCLUDED.intervalo_0_1,
            intervalo_0_9 = EXCLUDED.intervalo_0_9,
            fecha_proceso = EXCLUDED.fecha_proceso
    """

    for row in rows:
        cursor.execute(
            upsert_sql,
            (
                row["id_cliente"],
                row["id_dispositivo"],
                row["fase"],
                row["fecha_prediccion"],
                row["valor_predicho"],
                row["intervalo_0_1"],
                row["intervalo_0_9"],
                row["fecha_proceso"],
            ),
        )


def main():
    print("Cargando modelo Chronos...")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline = Chronos2Pipeline.from_pretrained(MODEL_ID, device_map=device)

    now_utc_minus_5 = datetime.now(TZ_UTC_MINUS_5)
    start_utc_minus_5 = now_utc_minus_5 - timedelta(days=7)

    print("Conectando a RDS...")
    conn = get_connection()
    cursor = conn.cursor()

    try:
        ensure_table(cursor)
        conn.commit()

        df = load_source_data(conn, start_utc_minus_5, now_utc_minus_5)
        if df.empty:
            print("No hay datos en los ultimos 7 dias.")
            return

        df = prepare_dataframe(df)
        print(f"Registros cargados: {len(df)}")
        print(f"Series detectadas: {df['serie_id'].nunique()}")

        predicciones = predict_by_series(df, pipeline, now_utc_minus_5)
        if not predicciones:
            print("No se generaron predicciones.")
            return

        save_predictions(cursor, predicciones)
        conn.commit()
        print(f"Predicciones guardadas: {len(predicciones)}")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    try:
        print("Iniciando proceso...")
        main()
        print("Proceso finalizado correctamente")
        sys.exit(0)  # éxito
    except Exception as e:
        print("ERROR EN EJECUCIÓN:")
        print(traceback.format_exc())
        sys.exit(1)  # fallo explícito
