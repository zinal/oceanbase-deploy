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

data "yandex_compute_image" "os" {
  family    = var.image_family
  folder_id = var.image_folder_id
}

resource "yandex_compute_disk" "observer_data" {
  count = var.observer_count

  name = "${var.deployment_name}-observer-${count.index + 1}-data"
  type = var.observer_data_disk_type
  zone = var.zone
  size = var.observer_data_disk_size_gb
}

resource "yandex_compute_disk" "observer_log" {
  count = var.observer_count

  name = "${var.deployment_name}-observer-${count.index + 1}-log"
  type = var.observer_log_disk_type
  zone = var.zone
  size = var.observer_log_disk_size_gb
}

resource "yandex_compute_instance" "observer" {
  count = var.observer_count

  name        = "${var.deployment_name}-observer-${count.index + 1}"
  platform_id = var.observer_platform
  zone        = var.zone

  resources {
    cores  = var.observer_cores
    memory = var.observer_memory_gb
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.os.id
      type     = var.observer_boot_disk_type
      size     = var.observer_boot_disk_size_gb
    }
  }

  secondary_disk {
    disk_id     = yandex_compute_disk.observer_data[count.index].id
    device_name = "data"
  }

  secondary_disk {
    disk_id     = yandex_compute_disk.observer_log[count.index].id
    device_name = "log"
  }

  network_interface {
    subnet_id = var.subnet_id
    nat       = true
  }

  metadata = {
    user-data = local.cloud_init
  }

  labels = {
    deployment = var.deployment_name
    role       = "observer"
    managed-by = "oceanbase-deploy"
  }
}

resource "yandex_compute_instance" "obproxy" {
  count = var.obproxy_count

  name        = "${var.deployment_name}-obproxy-${count.index + 1}"
  platform_id = var.observer_platform
  zone        = var.zone

  resources {
    cores  = var.obproxy_cores
    memory = var.obproxy_memory_gb
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.os.id
      type     = "network-ssd"
      size     = 20
    }
  }

  network_interface {
    subnet_id = var.subnet_id
    nat       = true
  }

  metadata = {
    user-data = local.cloud_init
  }

  labels = {
    deployment = var.deployment_name
    role       = "obproxy"
    managed-by = "oceanbase-deploy"
  }
}

output "observer_public_ips" {
  value = [for vm in yandex_compute_instance.observer : vm.network_interface[0].nat_ip_address]
}

output "obproxy_public_ips" {
  value = [for vm in yandex_compute_instance.obproxy : vm.network_interface[0].nat_ip_address]
}
