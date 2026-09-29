#!/bin/bash
set -e

echo "======================================="
echo " Turnstile Solver - PM2 VPS Deployment"
echo "======================================="

# 1. Update and Install System Dependencies
echo "[*] Installing system dependencies (Python, npm, xvfb)..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv xvfb nodejs

# 2. Install PM2 globally
echo "[*] Installing PM2..."
sudo npm install -g pm2

# 3. Setup Python Virtual Environment
echo "[*] Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# 4. Install Python Dependencies
echo "[*] Installing Python requirements..."
pip install -r requirements.txt

# 5. Fetch Camoufox browser binaries
echo "[*] Downloading Camoufox headless browser..."
python -m camoufox fetch

# 6. Setup Environment Variables
if [ ! -f .env ]; then
    echo "[*] Creating .env file from template..."
    cp .env.example .env
    
    # Generate a random secure API Key
    RANDOM_KEY=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)
    echo "API_KEY=$RANDOM_KEY" >> .env
    echo "[+] Generated secure API_KEY: $RANDOM_KEY"
    echo "    (Please save this key, you will need it for your Python script!)"
else
    echo "[*] .env file already exists, skipping creation."
fi

# 7. Create PM2 Ecosystem File
echo "[*] Creating PM2 ecosystem.config.js..."
cat << 'EOF' > ecosystem.config.js
module.exports = {
  apps: [{
    name: "turnstile-solver",
    script: "xvfb-run",
    args: "-a venv/bin/python -m app",
    interpreter: "none",
    env: {
      NODE_ENV: "production",
    }
  }]
}
EOF

# 8. Start with PM2
echo "[*] Starting Turnstile Solver via PM2..."
pm2 start ecosystem.config.js
pm2 save
sudo pm2 startup

echo "======================================="
echo "✅ Deployment Complete!"
echo "View live logs with: pm2 logs turnstile-solver"
echo "======================================="
