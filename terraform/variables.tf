# Переменные Terraform для Yandex Cloud (опциональная альтернатива scripts/01-provision-vms.sh)

variable "deployment_name" {
  type        = string
  description = "Префикс имён ресурсов"
}

variable "zone" {
  type    = string
  default = "ru-central1-a"
}

variable "platform_id" {
  type    = string
  default = "standard-v3"
}

variable "cores" {
  type    = number
  default = 8
}

variable "memory_gb" {
  type    = number
  default = 32
}

variable "core_fraction" {
  type    = number
  default = 100
}

variable "boot_disk_type" {
  type    = string
  default = "network-ssd"
}

variable "boot_disk_size_gb" {
  type    = number
  default = 50
}

variable "data_disk_type" {
  type    = string
  default = "network-ssd"
}

variable "data_disk_size_gb" {
  type    = number
  default = 500
}

variable "observer_count" {
  type    = number
  default = 3
}

variable "image_family" {
  type    = string
  default = "ubuntu-2204-lts"
}

variable "ssh_user" {
  type    = string
  default = "obadmin"
}

variable "ssh_public_key" {
  type        = string
  description = "Содержимое публичного SSH-ключа"
}

variable "subnet_id" {
  type        = string
  description = "ID подсети Yandex Cloud"
}
