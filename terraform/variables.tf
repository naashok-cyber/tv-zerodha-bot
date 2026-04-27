variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "tv-zerodha-bot"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "asia-south1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "asia-south1-a"
}

variable "credentials_file" {
  description = "Path to GCP service account credentials JSON"
  type        = string
  default     = "~/.gcp/tv-zerodha-bot-terraform.json"
}
