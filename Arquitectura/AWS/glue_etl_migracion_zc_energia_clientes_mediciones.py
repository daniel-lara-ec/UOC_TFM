import sys
import logging
from datetime import timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.utils import AnalysisException

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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

raw_path = f"s3://{args['RAW_BUCKET']}/{args['RAW_PREFIX'].strip('/')}/"
curated_path = f"s3://{args['RAW_BUCKET']}/{args['CURATED_PREFIX'].strip('/')}/"

raw_df = None
try:
    raw_df = (
        spark.read.option("multiLine", "false")
        .option("recursiveFileLookup", "true")
        .option("pathGlobFilter", "*.json")
        .json(raw_path)
    )
except AnalysisException as exc:
    if "Unable to infer schema for JSON" in str(exc):
        logger.info("No JSON files found in RAW path; exiting cleanly.")
    else:
        logger.exception("Failed to read RAW JSON data from %s", raw_path)
    job.commit()
    raise SystemExit(0)

if raw_df is None or raw_df.rdd.isEmpty():
    logger.info("No data available in RAW path; nothing to migrate.")
    job.commit()
    raise SystemExit(0)

fecha_medicion_expr = F.coalesce(
    F.to_timestamp(F.col("metrica.FechaMedicion")),
    F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd HH:mm:ss"),
    F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss"),
    F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss.SSS"),
    F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ssX"),
    F.to_timestamp(F.col("metrica.FechaMedicion"), "yyyy-MM-dd'T'HH:mm:ss.SSSX"),
)

metricas_df = (
    raw_df.filter(F.col("metricas").isNotNull())
    .withColumn("ts_raw", F.col("ts").cast("long"))
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
        "hora",
        F.hour(F.coalesce(F.col("fecha_medicion"), F.col("event_ts"))).cast("int"),
    )
    .withColumn(
        "fecha_ingesta", F.from_utc_timestamp(F.current_timestamp(), "America/Bogota")
    )
    .filter(F.col("periodo").isNotNull() & F.col("hora").isNotNull())
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

if metricas_df.rdd.isEmpty():
    logger.info("No records matched expected schema in RAW; nothing to migrate.")
    job.commit()
    raise SystemExit(0)

logger.info("Migrating full dataset to %s", curated_path)
logger.info("Rows to process: %s", metricas_df.count())

# Full replacement for mediciones table path before writing all partitions.
jvm = spark._jvm
hadoop_path = jvm.org.apache.hadoop.fs.Path(curated_path)
fs = hadoop_path.getFileSystem(spark._jsc.hadoopConfiguration())
if fs.exists(hadoop_path):
    fs.delete(hadoop_path, True)

(
    metricas_df.dropDuplicates(["mqtt_topic", "ts_raw", "fase"])
    .write.mode("overwrite")
    .option("partitionOverwriteMode", "dynamic")
    .format("parquet")
    .partitionBy("periodo", "hora")
    .save(curated_path)
)

logger.info("Migration completed. Rebuilt mediciones table in %s", curated_path)
job.commit()
