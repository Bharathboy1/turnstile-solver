#!/bin/bash
set -e

echo "======================================="
echo " Turnstile Solver - PM2 VPS Deployment"
echo "======================================="

# 1. Update and Install System Dependencies (only if missing)
PACKAGES_TO_INSTALL=()

if ! command -v python3 >/dev/null 2>&1 && ! dpkg -s python3 >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(python3)
fi

if ! python3 -m pip --version >/dev/null 2>&1 && ! dpkg -s python3-pip >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(python3-pip)
fi

if ! python3 -m venv --help >/dev/null 2>&1 && ! dpkg -s python3-venv >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(python3-venv)
fi

if ! command -v xvfb-run >/dev/null 2>&1 && ! dpkg -s xvfb >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(xvfb)
fi

if ! command -v node >/dev/null 2>&1 && ! dpkg -s nodejs >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(nodejs)
fi

if ! command -v npm >/dev/null 2>&1 && ! dpkg -s npm >/dev/null 2>&1; then
    PACKAGES_TO_INSTALL+=(npm)
fi

if [ ${#PACKAGES_TO_INSTALL[@]} -gt 0 ]; then
    echo "[*] Installing missing system dependencies (${PACKAGES_TO_INSTALL[*]})..."
    sudo apt update
    sudo apt install -y "${PACKAGES_TO_INSTALL[@]}"
else
    echo "[*] All system dependencies (python3, pip, venv, xvfb, nodejs, npm) are already installed. Skipping apt install."
fi

# 2. Install PM2 globally (only if not already installed)
if ! command -v pm2 >/dev/null 2>&1; then
    echo "[*] Installing PM2..."
    sudo npm install -g pm2
else
    echo "[*] PM2 is already installed ($(pm2 -v)). Skipping..."
fi

# 3. Setup Python Virtual Environment
if [ ! -d "venv" ] || [ ! -f "venv/bin/activate" ]; then
    echo "[*] Setting up Python virtual environment..."
    python3 -m venv venv
else
    echo "[*] Python virtual environment already exists. Skipping creation."
fi
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
    
    # Fix Docker paths to local paths for PM2
    sed -i 's|DB_PATH=/data/solver.db|DB_PATH=./solver.db|g' .env
    
    # Remove Docker-specific proxies since we are running natively
    sed -i 's|SOLVER_PROXY=http://warp:8080|SOLVER_PROXY=|g' .env
    sed -i 's|CHALLENGE_PROXY_URL=http://byparr:8191|CHALLENGE_PROXY_URL=|g' .env
    
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
const fs = require('fs');

const envConfig = fs.readFileSync('.env', 'utf-8')
  .split('\n')
  .filter(line => line && !line.startsWith('#'))
  .reduce((acc, line) => {
    const [key, ...val] = line.split('=');
    if (key) acc[key.trim()] = val.join('=').trim();
    return acc;
  }, {});

module.exports = {
  apps: [{
    name: "turnstile-solver",
    script: "xvfb-run",
    args: "-a venv/bin/python -m app",
    interpreter: "none",
    env: {
      NODE_ENV: "production",
      DB_PATH: "solver.db",
      ...envConfig
    }
  }]
}
EOF

# 8. Start with PM2
echo "[*] Starting Turnstile Solver via PM2..."
if pm2 describe turnstile-solver >/dev/null 2>&1; then
    echo "[*] turnstile-solver is already registered in PM2, restarting with updated env..."
    pm2 restart ecosystem.config.js --update-env
else
    pm2 start ecosystem.config.js
fi
pm2 save
sudo pm2 startup

echo "======================================="
echo "✅ Deployment Complete!"
echo "View live logs with: pm2 logs turnstile-solver"
echo "======================================="
