output "external_ip" {
  description = "Reserved static external IP address of the bot VM"
  value       = google_compute_address.static_ip.address
}

output "ssh_command" {
  description = "Ready-to-run gcloud SSH command"
  value       = "gcloud compute ssh tv-zerodha-bot --zone=${var.zone} --project=${var.project_id}"
}

output "webhook_url" {
  description = "TradingView webhook URL to configure in alert settings"
  value       = "http://${google_compute_address.static_ip.address}:8000/webhook"
}
