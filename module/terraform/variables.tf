variable "default_encryption_key" {
  sensitive = true
  description = "The default key used to encrypt objects"
  default = "OVERWRITTEN"
}
