variable "deployment_name" {
  type        = string
  description = "Префикс имён ресурсов"
}

variable "zone" {
  type    = string
  default = "ru-central1-a"
}

variable "subnet_id" {
  type        = string
  description = "ID подсети Yandex Cloud"
}

variable "ssh_user" {
  type    = string
  default = "obadmin"
}

variable "ssh_public_key" {
  type        = string
  description = "Содержимое публичного SSH-ключа"
}

variable "image_family" {
  type    = string
  default = "ubuntu-2204-lts"
}

# --- Observer profile (OceanBase production defaults) ---
variable "observer_count" {
  type    = number
  default = 3
}

variable "observer_platform" {
  type    = string
  default = "standard-v3"
}

variable "observer_cores" {
  type    = number
  default = 8
}

variable "observer_memory_gb" {
  type    = number
  default = 32
}

variable "observer_boot_disk_type" {
  type    = string
  default = "network-ssd-io-m3"
}

variable "observer_boot_disk_size_gb" {
  type    = number
  default = 50
}

variable "observer_data_disk_type" {
  type    = string
  default = "network-ssd-nonreplicated"
}

variable "observer_data_disk_size_gb" {
  type    = number
  default = 558
}

variable "observer_log_disk_type" {
  type    = string
  default = "network-ssd-io-m3"
}

variable "observer_log_disk_size_gb" {
  type    = number
  default = 279
}

# --- OBProxy profile ---
variable "obproxy_count" {
  type    = number
  default = 2
}

variable "obproxy_cores" {
  type    = number
  default = 2
}

variable "obproxy_memory_gb" {
  type    = number
  default = 4
}
