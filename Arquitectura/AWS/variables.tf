variable "region" {
  default = "us-east-1"
}

variable "project" {
  default = "uoc-tfm"
}

variable "env" {
  default = "dev"
}

variable "db_password" {
  sensitive = true
  default   = ""
}

variable "vpc_id" {
  default = "vpc-"
}
variable "subnet_ids" {
  type    = list(string)
  default = ["subnet-", "subnet-"]
}

variable "public_subnet_id" {
  description = "Subnet publica donde desplegar la EC2 web"
  type        = string
  default     = "subnet-"
}

variable "web_instance_type" {
  description = "Tipo de instancia EC2 para la web"
  type        = string
  default     = "t3.small"
}

variable "web_key_name" {
  description = "Nombre de Key Pair para acceso SSH a la EC2"
  type        = string
  default     = "Mi clave"
}

variable "web_ssh_cidr_blocks" {
  description = "CIDR blocks permitidos para acceso SSH a la EC2 web"
  type        = list(string)
  default     = ["<Mi IP V4>/32"]
}

variable "id_cliente" {
  description = "Identificador de cliente usado en el topic MQTT"
  type        = string
  default     = "cliente_001"
}

variable "id_dispositivo" {
  description = "Identificador del dispositivo IoT (thing name)"
  type        = string
  default     = "dispositivo_001"
}

variable "mqtt_client_id" {
  description = "Client ID MQTT opcional; si es null se usa id_dispositivo"
  type        = string
  default     = null
}

variable "iot_certificate_arn" {
  description = "ARN del certificado IoT para adjuntar policy/principal al thing"
  type        = string
  default     = null
}
