"""
ETL para carga diaria de datos desde el desembarco a la zona curada.
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.utils import AnalysisException
from pyspark.sql import functions as F


args = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "RAW_BUCKET",
        "RAW_PREFIX",
        "CURATED_PREFIX",
    ],
)

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
spark.conf.set("spark.sql.sources.partitionOverwriteMode", "dynamic")
job = Job(glue_context)
job.init(args["JOB_NAME"], args)

RAW_PATH = f"s3://{args['RAW_BUCKET']}/{args['RAW_PREFIX'].strip('/')}/"
CURATED_PATH = f"s3://{args['RAW_BUCKET']}/{args['CURATED_PREFIX'].strip('/')}/"

# Calculamos el dia objetivo (dia anterior) en UTC-5 para definir los datos a leer.
ingest_tz = ZoneInfo("America/Bogota")
current_ts = datetime.now(ingest_tz)
target_day = (current_ts - timedelta(days=1)).date()

RAW_DAY_PATH = f"{RAW_PATH}*/*/*/*/" f"{target_day.strftime('%Y/%m/%d')}/*/"

try:
    raw_df = (
        spark.read.option("multiLine", "false")
        .option("pathGlobFilter", "*.json")
        .json(RAW_DAY_PATH)
    )
except AnalysisException as exc:
    job.commit()
    exit(0)

if raw_df is not None and not raw_df.rdd.isEmpty():
    # Enforce expected shape from IoT payload and derive periodo from FechaMedicion.
    fecha_medicion_expr = F.coalesce(
        F.to_timestamp(F.col("metrica.FechaMedicion")),
        F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd HH:mm:ss"),
        F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss"),
        F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss.SSS"),
        F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ssX"),
        F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss.SSSX"),
    )

    metricas_df = (
        raw_df.withColumn("ts_raw", F.col("ts").cast("long"))
        .withColumn("metrica", F.explode_outer(F.col("metricas")))
        .withColumn("mqtt_topic", F.col("mqtt_topic").cast("string"))
        .withColumn(
            "event_ts", F.to_timestamp(F.from_unixtime(F.col("ts_raw") / F.lit(1000)))
        )
        .withColumn("cliente", F.element_at(F.split(F.col("mqtt_topic"), "/"), 2))
        .withColumn("dispositivo", F.element_at(F.split(F.col("mqtt_topic"), "/"), 3))
        .withColumn("fecha_medicion", fecha_medicion_expr)
        .withColumn("periodo", F.to_date(F.col("fecha_medicion")))
        .withColumn(
            "fecha_ingesta",
            F.from_utc_timestamp(F.current_timestamp(), "America/Bogota"),
        )
        .filter(F.col("periodo").isNotNull())
        .withColumn(
            "hora",
            F.hour(F.coalesce(F.col("fecha_medicion"), F.col("event_ts"))).cast("int"),
        )
        .filter(F.col("hora").isNotNull())
        .select(
            F.col("cliente"),
            F.col("dispositivo"),
            F.col("metrica.Fase").cast("int").alias("fase"),
            F.col("metrica.Voltaje").cast("double").alias("voltaje"),
            F.col("metrica.Corriente").cast("double").alias("corriente"),
            F.col("metrica.Potencia").cast("double").alias("potencia"),
            F.col("metrica.FactorPotencia").cast("double").alias("factor_potencia"),
            F.col("metrica.Frecuencia").cast("double").alias("frecuencia"),
            F.col("metrica.EnergiaActiva").cast("double").alias("energia_activa"),
            F.col("fecha_medicion"),
            F.col("periodo"),
            F.col("event_ts"),
            F.col("fecha_ingesta"),
            F.col("mqtt_topic"),
            F.col("ts_raw"),
            F.col("hora"),
        )
    )

    if not metricas_df.rdd.isEmpty():

        (
            metricas_df.dropDuplicates(["mqtt_topic", "ts_raw", "fase"])
            .write.mode("overwrite")
            .option("partitionOverwriteMode", "dynamic")
            .format("parquet")
            .partitionBy("periodo", "hora")
            .save(CURATED_PATH)
        )

job.commit()
