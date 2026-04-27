resource "google_compute_address" "static_ip" {
  name   = "tv-zerodha-bot-ip"
  region = var.region
}

resource "google_compute_instance" "tv-zerodha-bot" {
  name         = "tv-zerodha-bot"
  machine_type = "e2-micro"
  zone         = var.zone

  tags = ["http-server", "https-server", "webhook"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"

    access_config {
      nat_ip = google_compute_address.static_ip.address
    }
  }

  metadata = {
    enable-oslogin = "true"
  }

  metadata_startup_script = <<-EOF
    #!/bin/bash
    set -euo pipefail

    # Install Docker
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

    # Enable Docker service
    systemctl enable docker
    systemctl start docker

    # Clone repository (replace URL with your actual repo)
    cd /opt
    git clone https://github.com/naashok-cyber/tv-zerodha-bot.git
    cd tv-zerodha-bot

    # Create data directory and .env from example
    mkdir -p data
    cp .env.example .env
    # Edit /opt/tv-zerodha-bot/.env with real credentials before running

    # Start bot
    docker compose up -d
  EOF
}
