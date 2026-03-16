#!/bin/bash
set -e

# ATLAS Production Setup Script
# Automates deployment of ATLAS Runtime Governance

echo "================================================"
echo "  ATLAS Production Setup"
echo "================================================"
echo ""

# Check requirements
command -v python3 >/dev/null 2>&1 || { echo "❌ Python 3 required"; exit 1; }
command -v pip3 >/dev/null 2>&1 || { echo "❌ pip3 required"; exit 1; }

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYTHON_VERSION detected"

# Deployment mode
echo ""
echo "Select deployment mode:"
echo "  1) Development (local, no Docker)"
echo "  2) Docker Compose"
echo "  3) Systemd (production server)"
echo ""
read -p "Choice [1-3]: " DEPLOY_MODE

case $DEPLOY_MODE in
  1)
    echo ""
    echo "=== Development Deployment ==="
    echo ""
    
    # Install dependencies
    echo "Installing Python dependencies..."
    pip3 install -r requirements-prod.txt
    
    # Initialize database
    echo "Initializing database..."
    python3 init_db.py
    
    echo ""
    echo "✅ Setup complete!"
    echo ""
    echo "Start ATLAS proxy:"
    echo "  python3 atlas_proxy_prod.py"
    echo ""
    echo "Start agent (in new terminal):"
    echo "  export AWS_REGION=us-east-1"
    echo "  export AWS_ACCESS_KEY_ID=..."
    echo "  export AWS_SECRET_ACCESS_KEY=..."
    echo "  python3 nova_agent_prod.py"
    echo ""
    ;;
    
  2)
    echo ""
    echo "=== Docker Compose Deployment ==="
    echo ""
    
    # Check Docker
    command -v docker >/dev/null 2>&1 || { echo "❌ Docker required"; exit 1; }
    command -v docker-compose >/dev/null 2>&1 || { echo "❌ Docker Compose required"; exit 1; }
    
    # Generate HMAC secret
    HMAC_SECRET=$(openssl rand -hex 32)
    echo "ATLAS_HMAC_SECRET=$HMAC_SECRET" > .env
    echo "✓ Generated HMAC secret"
    
    # Initialize database
    echo "Initializing database..."
    python3 init_db.py
    
    # Build and start
    echo "Building Docker images..."
    docker-compose build
    
    echo "Starting services..."
    docker-compose up -d
    
    echo ""
    echo "✅ ATLAS is running!"
    echo ""
    echo "Services:"
    echo "  • ATLAS Proxy:  http://localhost:9000"
    echo "  • Admin UI:     http://localhost:9000/admin"
    echo "  • Tool Backend: http://localhost:9001"
    echo ""
    echo "Check status: docker-compose ps"
    echo "View logs:    docker-compose logs -f"
    echo "Stop:         docker-compose down"
    echo ""
    ;;
    
  3)
    echo ""
    echo "=== Systemd Production Deployment ==="
    echo ""
    
    # Check if running as root
    if [ "$EUID" -ne 0 ]; then
      echo "❌ Please run as root for systemd installation"
      exit 1
    fi
    
    # Create atlas user
    if ! id -u atlas >/dev/null 2>&1; then
      echo "Creating atlas user..."
      useradd -r -s /bin/false -d /opt/atlas atlas
    fi
    
    # Create directories
    echo "Creating directories..."
    mkdir -p /opt/atlas/{venv,logs}
    mkdir -p /etc/atlas
    
    # Copy files
    echo "Installing application..."
    cp -r atlas_core services *.py /opt/atlas/
    
    # Create virtual environment
    echo "Setting up Python virtual environment..."
    python3 -m venv /opt/atlas/venv
    /opt/atlas/venv/bin/pip install -r requirements-prod.txt
    
    # Initialize database
    echo "Initializing database..."
    cd /opt/atlas
    /opt/atlas/venv/bin/python init_db.py
    
    # Set permissions
    chown -R atlas:atlas /opt/atlas
    
    # Generate HMAC secret
    HMAC_SECRET=$(openssl rand -hex 32)
    cat > /etc/atlas/proxy.env <<EOF
ATLAS_HMAC_SECRET=$HMAC_SECRET
EOF
    chmod 600 /etc/atlas/proxy.env
    
    # Install systemd services
    echo "Installing systemd services..."
    cp atlas-proxy.service /etc/systemd/system/
    cp atlas-tools.service /etc/systemd/system/
    
    # Update HMAC secret in service file
    sed -i "s/REPLACE_WITH_SECURE_SECRET/$HMAC_SECRET/" /etc/systemd/system/atlas-proxy.service
    
    # Reload systemd
    systemctl daemon-reload
    
    # Enable and start services
    echo "Enabling services..."
    systemctl enable atlas-tools
    systemctl enable atlas-proxy
    
    echo "Starting services..."
    systemctl start atlas-tools
    sleep 2
    systemctl start atlas-proxy
    
    echo ""
    echo "✅ ATLAS installed as systemd service!"
    echo ""
    echo "Commands:"
    echo "  Status:  sudo systemctl status atlas-proxy"
    echo "  Logs:    sudo journalctl -u atlas-proxy -f"
    echo "  Restart: sudo systemctl restart atlas-proxy"
    echo "  Stop:    sudo systemctl stop atlas-proxy"
    echo ""
    echo "Services:"
    echo "  • ATLAS Proxy:  http://$(hostname -I | awk '{print $1}'):9000"
    echo "  • Admin UI:     http://$(hostname -I | awk '{print $1}'):9000/admin"
    echo ""
    ;;
    
  *)
    echo "Invalid choice"
    exit 1
    ;;
esac

echo ""
echo "================================================"
echo "  Setup Complete!"
echo "================================================"
