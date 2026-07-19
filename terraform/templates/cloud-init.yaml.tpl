#cloud-config
users:
  - name: ${ssh_user}
    groups: [sudo, adm]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - ${ssh_public_key}
package_update: true
packages:
  - python3
  - curl
  - wget
  - jq
  - lvm2
  - xfsprogs
  - e2fsprogs
