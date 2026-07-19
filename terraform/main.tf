terraform {
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = ">= 0.100"
    }
  }
}

provider "yandex" {
  zone = var.zone
}

locals {
  cloud_init = templatefile("${path.module}/templates/cloud-init.yaml.tpl", {
    ssh_user       = var.ssh_user
    ssh_public_key = chomp(var.ssh_public_key)
  })
}

resource "yandex_compute_instance" "observer" {
  count = var.observer_count

  name        = "${var.deployment_name}-observer-${count.index + 1}"
  platform_id = var.platform_id
  zone        = var.zone

  resources {
    cores         = var.cores
    memory        = var.memory_gb
    core_fraction = var.core_fraction
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.os.id
      type     = var.boot_disk_type
      size     = var.boot_disk_size_gb
    }
  }

  secondary_disk {
    disk_id = yandex_compute_disk.data[count.index].id
  }

  network_interface {
    subnet_id = var.subnet_id
    nat       = true
  }

  metadata = {
    user-data = local.cloud_init
  }

  labels = {
    deployment  = var.deployment_name
    role        = "observer"
    managed-by  = "oceanbase-deploy"
  }
}

resource "yandex_compute_disk" "data" {
  count = var.observer_count

  name = "${var.deployment_name}-observer-${count.index + 1}-data"
  type = var.data_disk_type
  zone = var.zone
  size = var.data_disk_size_gb
}

data "yandex_compute_image" "os" {
  family = var.image_family
}

output "observer_public_ips" {
  value = [for vm in yandex_compute_instance.observer : vm.network_interface[0].nat_ip_address]
}

output "observer_private_ips" {
  value = [for vm in yandex_compute_instance.observer : vm.network_interface[0].ip_address]
}
