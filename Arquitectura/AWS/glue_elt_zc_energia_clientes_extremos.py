import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.types import DateType
from pyspark.sql.utils import AnalysisException


args = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "RAW_BUCKET",
        "SOURCE_DATABASE",
        "SOURCE_TABLE",
        "TARGET_CURATED_PREFIX",
        "FECHA_CORTE",
    ],
)

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
spark.conf.set("spark.sql.sources.partitionOverwriteMode", "dynamic")
job = Job(glue_context)
job.init(args["JOB_NAME"], args)

fecha_proceso = args["FECHA_CORTE"]

if fecha_proceso == "YYYY-MM-DD":
    yesterday_utc = (
        (datetime.now(timezone.utc) - timedelta(days=1)).date().strftime("%Y-%m-%d")
    )
else:
    yesterday_utc = fecha_proceso

target_path = f"s3://{args['RAW_BUCKET']}/{args['TARGET_CURATED_PREFIX'].strip('/')}/"
source_database = args["SOURCE_DATABASE"]
source_table = args["SOURCE_TABLE"]

try:
    df = glue_context.create_dynamic_frame.from_catalog(
        database=source_database,
        table_name=source_table,
        push_down_predicate=f"periodo = '{yesterday_utc}'",
    ).toDF()

except AnalysisException as exc:
    job.commit()
    exit(0)

if not df.rdd.isEmpty():

    extremos_df = (
        df.groupBy("cliente", "dispositivo", "fase", "periodo")
        .agg(
            F.min("voltaje").alias("voltaje_minimo"),
            F.max("voltaje").alias("voltaje_maximo"),
            F.min("corriente").alias("corriente_minima"),
            F.max("corriente").alias("corriente_maxima"),
            F.min("potencia").alias("potencia_minima"),
            F.max("potencia").alias("potencia_maxima"),
            F.min("factor_potencia").alias("factor_potencia_minimo"),
            F.max("factor_potencia").alias("factor_potencia_maximo"),
            F.min("frecuencia").alias("frecuencia_minima"),
            F.max("frecuencia").alias("frecuencia_maxima"),
            F.min("energia_activa").alias("energia_activa_minima"),
            F.max("energia_activa").alias("energia_activa_maxima"),
            F.count(F.lit(1)).alias("muestras"),
        )
        .withColumn(
            "fecha_ingesta",
            F.from_utc_timestamp(F.current_timestamp(), "America/Bogota"),
        )
    )

    (
        extremos_df.write.mode("overwrite")
        .option("partitionOverwriteMode", "dynamic")
        .format("parquet")
        .partitionBy("periodo")
        .save(target_path)
    )


job.commit()
