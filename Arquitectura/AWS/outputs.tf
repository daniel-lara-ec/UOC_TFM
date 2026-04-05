output "rds_endpoint" {
  value = aws_db_instance.postgres.address
}

data "aws_iot_endpoint" "mqtt" {
  endpoint_type = "iot:Data-ATS"
}

output "s3_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "web_instance_public_ip" {
  value = aws_eip.web.public_ip
}

output "web_url" {
  value = "http://${aws_eip.web.public_ip}"
}

output "mqtt_endpoint" {
  value = data.aws_iot_endpoint.mqtt.endpoint_address
}

output "glue_etl_job_name" {
  value = aws_glue_job.zc_energia_clientes_mediciones.name
}

output "glue_etl_migracion_job_name" {
  value = aws_glue_job.zc_energia_clientes_mediciones_migracion.name
}

output "athena_curated_table" {
  value = "${aws_glue_catalog_database.iot.name}.${aws_glue_catalog_table.zc_energia_clientes_mediciones.name}"
}

output "athena_curated_table_s3_location" {
  value = "s3://${aws_s3_bucket.raw.bucket}/${local.curated_prefix}/"
}

output "glue_notebook_role_arn" {
  value = aws_iam_role.glue_notebook.arn
}

output "glue_crawler_mediciones_name" {
  value = aws_glue_crawler.zc_energia_clientes_mediciones_daily.name
}

output "glue_crawler_horaria_name" {
  value = aws_glue_crawler.zc_energia_clientes_horaria_daily.name
}

output "glue_crawler_extremos_name" {
  value = aws_glue_crawler.zc_energia_clientes_extremos_daily.name
}
