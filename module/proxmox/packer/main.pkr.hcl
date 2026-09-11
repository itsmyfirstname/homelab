packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.2"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

# --- Connection and Authentication Variables ---
variable "proxmox_api_url" {
  type    = string
  default = "https://192.168.1.101:8006/api2/json"
}

variable "proxmox_api_token_id" {
  type    = string
  default = "root@pam!packer" # Example Token ID
}

variable "proxmox_api_token_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "proxmox_node" {
  type    = string
  default = "aceprox"
}

# --- Source Configuration ---
source "proxmox-iso" "debian" {
  proxmox_url              = var.proxmox_api_url
  username                 = var.proxmox_api_token_id
  token                    = var.proxmox_api_token_secret
  insecure_skip_tls_verify = true

  node                 = var.proxmox_node
  vm_id                = "9000"
  vm_name              = "debian-12-template"
  template_description = "Debian 12 Template created via Packer"

  # VM Hardware Configuration
  cores    = 2
  sockets  = 1
  memory   = 2048
  cpu_type = "x86-64-v2-AES" # Match existing CPU type from terraform config

  # Storage Settings
  scsihw = "virtio-scsi-pci"
  disks {
    disk_size         = "20G"
    storage_pool      = "local-lvm"
    type              = "scsi"
  }

  # Network Settings
  network_adapters {
    model    = "virtio"
    bridge   = "vmbr0"
    firewall = false
  }

  # ISO Location (matches terraform config)
  iso_file    = "local:iso/debian-12.4.0-amd64-netinst.iso"
  unmount_iso = true

  # Enable QEMU Agent
  qemu_agent = true

  # SSH configuration for Packer provisioning
  ssh_username = "packer"
  ssh_password = "packer"
  ssh_timeout  = "30m"

  # Automated installer configuration (Debian Preseed)
  # Packer hosts a local HTTP server on a random port to serve the preseed file
  http_directory = "http"
  boot_wait      = "10s"
  boot_command = [
    "<esc><wait>",
    "install ",
    "preseed/url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg ",
    "debian-installer/locale=en_US.UTF-8 ",
    "keyboard-configuration/xkb-keymap=us ",
    "netcfg/choose_interface=auto ",
    "fb=falsedebconf/priority=critical ",
    "grub-installer/bootdev=/dev/sda ",
    "<enter>"
  ]
}

# --- Build Logic ---
build {
  sources = ["source.proxmox-iso.debian"]

  # Provision using the existing script in the directory
  provisioner "shell" {
    script = "${path.root}/debain.sh"
  }
}
