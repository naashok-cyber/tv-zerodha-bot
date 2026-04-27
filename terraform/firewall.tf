resource "google_compute_firewall" "allow_webhook" {
  name    = "tv-zerodha-bot-webhook"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["webhook"]

  description = "Allow inbound webhook traffic from TradingView (IP allowlist enforced in app)"
}

resource "google_compute_firewall" "allow_ssh" {
  name    = "tv-zerodha-bot-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["http-server"]

  description = "Allow SSH access"
}

resource "google_compute_firewall" "allow_https" {
  name    = "tv-zerodha-bot-https"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["https-server"]

  description = "Allow HTTPS (future TLS termination)"
}
