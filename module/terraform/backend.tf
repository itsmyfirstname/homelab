terraform {
  backend "gcs" {
    bucket  = "mehays-terraform"
    prefix  = "homelab/state"
  }
}

