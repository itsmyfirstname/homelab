resource "google_secret_manager_secret" "default_encryption_key" {
  secret_id = "default_encryption_key"

  labels = {
    label = "homelab"
  }

  replication {
    user_managed {
      replicas {
        location = "us-central1"
      }
      replicas {
        location = "us-east1"
      }
    }
  }

  deletion_protection = false
}

resource "google_secret_manager_secret_version" "secret-version-basic" {
  secret = google_secret_manager_secret.default_encryption_key.id
  secret_data = sensitive(var.default_encryption_key)
}
