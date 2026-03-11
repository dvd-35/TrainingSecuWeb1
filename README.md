# 🛡️ Guide Complet — RPI-5 Monitoring & Sécurité
## Raspberry Pi 5 · Nginx · rsyslog · Wazuh SIEM

---

## PHASE 1 — Dashboard système local

### 1.1 · Prérequis sur le Raspberry Pi

```bash
# Mise à jour du système — toujours commencer par là.
# apt est le gestionnaire de paquets Debian/Raspbian.
# La commande 'update' rafraîchit la liste des paquets disponibles,
# 'upgrade' installe les nouvelles versions. Le flag -y répond "oui" automatiquement.
sudo apt update && sudo apt upgrade -y

# Installation de pip (gestionnaire de paquets Python) et de venv
# (outil d'isolation des environnements Python)
sudo apt install python3-pip python3-venv -y
```

### 1.2 · Installation de l'API Flask

```bash
# On crée un dossier dédié au projet, dans /opt qui est la convention
# Linux pour les logiciels applicatifs tiers (vs /usr pour le système).
sudo mkdir -p /opt/rpi-monitor
sudo chown pi:pi /opt/rpi-monitor    # on donne la propriété à l'utilisateur "pi"
cd /opt/rpi-monitor

# On copie les deux fichiers fournis ici :
# dashboard.html → fichier servi par Nginx
# api.py         → backend Flask

# Création d'un environnement virtuel Python isolé.
# Raison sécuritaire : les dépendances du projet n'interfèrent pas
# avec les packages système, et en cas de compromission du projet,
# le dommage reste contenu dans ce venv.
python3 -m venv venv

# Activation du venv : à partir de là, "python" et "pip" pointent
# vers les binaires du venv, pas ceux du système.
source venv/bin/activate

# Installation des dépendances Python :
# - psutil : accès aux métriques système (/proc, /sys)
# - flask  : micro-framework web léger pour exposer l'API JSON
# - flask-cors : gestion des headers CORS (Cross-Origin Resource Sharing)
pip install psutil flask flask-cors
```

### 1.3 · Service systemd pour l'API (démarrage automatique)

```bash
# systemd est le gestionnaire de services Linux.
# On crée un fichier "unit" qui décrit comment démarrer notre API.
sudo nano /etc/systemd/system/rpi-monitor.service
```

Contenu du fichier :

```ini
[Unit]
Description=RPI Monitor API — Dashboard système
# On attend que le réseau soit disponible avant de démarrer
After=network.target

[Service]
Type=simple
# Utilisateur non-privilégié : JAMAIS lancer un service web en root.
# Si l'API est compromise, l'attaquant n'aura que les droits de "pi".
User=pi
WorkingDirectory=/opt/rpi-monitor
# ExecStart pointe vers le python du venv, pas celui du système
ExecStart=/opt/rpi-monitor/venv/bin/python api.py
# Si le service plante, systemd le relance automatiquement après 5s
Restart=on-failure
RestartSec=5

[Install]
# L'API démarre au niveau multi-utilisateur (boot normal)
WantedBy=multi-user.target
```

```bash
# Rechargement de la configuration systemd pour prendre en compte
# le nouveau fichier .service qu'on vient de créer
sudo systemctl daemon-reload

# Activation au démarrage + lancement immédiat
sudo systemctl enable rpi-monitor
sudo systemctl start rpi-monitor

# Vérification : "active (running)" doit apparaître en vert
sudo systemctl status rpi-monitor

# Test direct de l'API depuis le Pi (curl = client HTTP en ligne de commande)
# jq formate le JSON pour le rendre lisible
curl http://localhost:5000/api/stats | python3 -m json.tool
```

---

## PHASE 2 — Nginx : reverse proxy & service du dashboard

### Raisonnement

On ne sert jamais directement Flask à l'extérieur. Nginx joue le rôle de
**reverse proxy** : il reçoit les connexions HTTP(S) entrantes, les transmet
à Flask sur le loopback interne, et renvoie la réponse au client.

Avantages sécuritaires :
- Flask ne voit jamais Internet directement
- Nginx gère le TLS/HTTPS
- Nginx peut rate-limiter, bloquer des IPs, et logger les accès
- Flask écoute sur 127.0.0.1:5000 (inaccessible depuis l'extérieur)

```bash
# Installation de Nginx
sudo apt install nginx -y

# Créer la config du site
sudo nano /etc/nginx/sites-available/rpi-monitor
```

Contenu de la config Nginx :

```nginx
# Limitation du débit : protège contre les attaques par flood/scan.
# On autorise 10 requêtes/seconde par IP. Les dépassements sont mis
# en attente (burst=20) puis les suivants sont rejetés avec 429.
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

server {
    listen 80;
    # Remplacez par l'IP locale de votre Pi ou votre domaine
    server_name 192.168.1.42 localhost;

    # Racine du site : le dashboard HTML
    root /opt/rpi-monitor;
    index dashboard.html;

    # En-têtes de sécurité HTTP :
    # - X-Frame-Options : empêche le clickjacking (iframe malveillante)
    # - X-Content-Type-Options : empêche le MIME sniffing
    # - X-XSS-Protection : active le filtre XSS des navigateurs anciens
    # - Referrer-Policy : limite les infos envoyées au site suivant
    # - Content-Security-Policy : whitelist des sources autorisées
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self' http://localhost:5000;" always;

    # Route vers le dashboard HTML
    location / {
        try_files $uri $uri/ =404;
    }

    # Route vers l'API Flask (reverse proxy)
    location /api/ {
        # Application de la limite de débit définie plus haut
        limit_req zone=api_limit burst=20 nodelay;

        # proxy_pass redirige la requête vers Flask en local
        proxy_pass http://127.0.0.1:5000;

        # Ces headers informent Flask de l'IP réelle du client
        # (sinon Flask voit toujours 127.0.0.1)
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Timeout : si Flask ne répond pas en 10s, on retourne 504
        proxy_read_timeout 10s;
    }

    # On cache les fichiers de config (sécurité)
    location ~ /\. {
        deny all;
    }
}
```

```bash
# Activation du site (lien symbolique de sites-available vers sites-enabled)
sudo ln -s /etc/nginx/sites-available/rpi-monitor /etc/nginx/sites-enabled/

# Désactivation du site par défaut Nginx (qui pourrait exposer des infos)
sudo rm /etc/nginx/sites-enabled/default

# Test de syntaxe de la config — ne jamais reloader sans ça !
sudo nginx -t

# Rechargement de Nginx sans coupure de service
sudo systemctl reload nginx
```

**Modification de dashboard.html** : changer l'URL de l'API dans le JS :

```javascript
// Avant (accès direct Flask) :
const API_URL = 'http://localhost:5000/api/stats';

// Après (via Nginx reverse proxy) :
const API_URL = '/api/stats';
// Nginx achemine /api/ vers Flask automatiquement
```

---

## PHASE 3 — Accès à distance sécurisé

### Option A — Cloudflare Tunnel (recommandé, sans ouvrir de port)

Cloudflare Tunnel crée un tunnel chiffré sortant depuis votre Pi vers
les serveurs Cloudflare. Aucun port n'est ouvert sur votre box.
C'est la solution la plus sûre car votre Pi n'est jamais exposé directement.

```bash
# Téléchargement de cloudflared (agent du tunnel)
# On télécharge le binaire ARM64 (architecture du BCM2712 du Pi 5)
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
chmod +x cloudflared-linux-arm64
sudo mv cloudflared-linux-arm64 /usr/local/bin/cloudflared

# Authentification : ouvre une URL dans votre navigateur
# pour lier cloudflared à votre compte Cloudflare
cloudflared tunnel login

# Création du tunnel nommé "rpi-monitor"
cloudflared tunnel create rpi-monitor

# Le tunnel génère un UUID, notez-le. Exemple : a1b2c3d4-...
# Création du fichier de config du tunnel
nano ~/.cloudflared/config.yml
```

Contenu du fichier config.yml :

```yaml
tunnel: a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx  # UUID de votre tunnel
credentials-file: /home/pi/.cloudflared/a1b2c3d4-xxxx.json

ingress:
  - hostname: monitor.votredomaine.fr
    service: http://localhost:80   # Nginx écoute sur le port 80
  - service: http_status:404      # Tout le reste → 404 (règle obligatoire)
```

```bash
# Création du DNS CNAME chez Cloudflare (pointe vers le tunnel)
cloudflared tunnel route dns rpi-monitor monitor.votredomaine.fr

# Test manuel avant d'automatiser
cloudflared tunnel run rpi-monitor

# Installation en service systemd
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

### Option B — WireGuard VPN (contrôle total, IP statique requise)

WireGuard est un VPN moderne, léger, et très performant.
Principe : vous vous connectez d'abord au VPN, puis accédez au Pi
comme si vous étiez sur le réseau local. Aucune exposition publique.

```bash
# Installation de WireGuard sur le Pi
sudo apt install wireguard -y

# Génération de la paire de clés du serveur (Pi)
# La clé privée ne doit JAMAIS quitter le Pi
wg genkey | sudo tee /etc/wireguard/private.key | wg pubkey | sudo tee /etc/wireguard/public.key
sudo chmod 600 /etc/wireguard/private.key

# Afficher les clés pour les noter
sudo cat /etc/wireguard/private.key
sudo cat /etc/wireguard/public.key

# Même opération sur votre PC client (ou smartphone)
wg genkey | tee client_private.key | wg pubkey > client_public.key

# Création de la config WireGuard sur le Pi
sudo nano /etc/wireguard/wg0.conf
```

```ini
[Interface]
Address = 10.0.0.1/24        # IP du Pi dans le réseau VPN
ListenPort = 51820            # Port UDP WireGuard (à ouvrir sur votre box)
PrivateKey = <CLÉ_PRIVÉE_PI> # Contenu de /etc/wireguard/private.key

# Sauvegarde auto des pairs lors des changements dynamiques
SaveConfig = true

[Peer]
# Votre PC client
PublicKey = <CLÉ_PUBLIQUE_CLIENT>
AllowedIPs = 10.0.0.2/32     # IP attribuée au client dans le VPN
```

```bash
# Activation et démarrage de l'interface WireGuard
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0

# Vérification
sudo wg show
```

### Pare-feu UFW (indispensable dans les deux cas)

```bash
# UFW (Uncomplicated Firewall) est un wrapper user-friendly autour d'iptables.
# Principe de moindre privilège : on bloque TOUT par défaut, puis on ouvre
# uniquement ce qui est nécessaire.
sudo apt install ufw -y

# Politique par défaut : refuser tout entrant, autoriser tout sortant
sudo ufw default deny incoming
sudo ufw default allow outgoing

# SSH uniquement depuis le réseau local (adaptez à votre sous-réseau)
# Remplacez 192.168.1.0/24 par votre plage d'adresses locales
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp

# HTTP local uniquement (Cloudflare Tunnel sortant ne nécessite pas d'ouverture)
sudo ufw allow from 192.168.1.0/24 to any port 80 proto tcp

# WireGuard (seulement si vous utilisez l'Option B)
sudo ufw allow 51820/udp

# Activation du pare-feu
sudo ufw enable

# Vérification des règles actives
sudo ufw status verbose
```

### Fail2ban — Protection anti-brute-force

```bash
# Fail2ban surveille les logs système et bannit temporairement les IPs
# qui font trop de tentatives d'authentification échouées.
# Il crée des règles iptables dynamiques.
sudo apt install fail2ban -y

# On ne modifie JAMAIS jail.conf directement (écrasé lors des mises à jour).
# On crée un fichier local qui surcharge la config par défaut.
sudo cp /etc/fail2ban/jail.conf /etc/fail2ban/jail.local
sudo nano /etc/fail2ban/jail.local
```

Sections à modifier dans jail.local :

```ini
[DEFAULT]
# Durée du ban en secondes (ici 1 heure)
bantime  = 3600
# Fenêtre d'observation (10 minutes)
findtime = 600
# Nombre de tentatives avant ban
maxretry = 5
# Backend de surveillance des logs (auto = systemd journal sur Pi)
backend = auto

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s

[nginx-http-auth]
enabled  = true
port     = http,https
logpath  = /var/log/nginx/error.log

[nginx-limit-req]
enabled  = true
port     = http,https
logpath  = /var/log/nginx/error.log
```

```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban

# Vérification des jails actives
sudo fail2ban-client status

# Voir les IPs bannies pour SSH
sudo fail2ban-client status sshd
```

---

## PHASE 4 — rsyslog : centralisation des logs sur le Pi

### Raisonnement

rsyslog est le démon de logging standard sous Linux.
Objectif : collecter les logs de TOUS les services du Pi (Nginx, SSH,
Fail2ban, système) dans des fichiers structurés, puis les envoyer
vers le SIEM Wazuh sur votre PC.

```bash
# rsyslog est généralement pré-installé. On l'enrichit avec les modules
# nécessaires pour la réception réseau.
sudo apt install rsyslog -y

# Configuration de rsyslog sur le Pi (émetteur)
sudo nano /etc/rsyslog.d/99-wazuh-forward.conf
```

```conf
# ── Module de transport TCP vers Wazuh ──────────────────────
# On utilise TCP plutôt qu'UDP : TCP garantit la livraison des messages
# (TCP est fiable, UDP ne l'est pas).
# @@  = TCP (@ seul = UDP)
# 192.168.1.100 = IP de votre PC (machine virtuelle Wazuh)
# 514 = port standard syslog

*.* @@192.168.1.100:514

# Format enrichi : on ajoute l'hostname du Pi dans chaque message
# pour que Wazuh sache d'où vient le log
$template WazuhFormat,"%HOSTNAME% %syslogtag%%msg%\n"
*.* @@192.168.1.100:514;WazuhFormat

# Conservation locale des logs même si Wazuh est injoignable
# Principe de résilience : on ne perd pas de logs si le réseau flanche
$ActionQueueType LinkedList
$ActionQueueFileName wazuh_fwd_queue
$ActionResumeRetryCount -1
$ActionQueueSaveOnShutdown on
```

```bash
# Validation de la configuration rsyslog
sudo rsyslogd -N1

# Redémarrage pour appliquer
sudo systemctl restart rsyslog

# Vérification en temps réel
sudo journalctl -fu rsyslog
```

### Logs Nginx vers rsyslog

```bash
# Dans /etc/nginx/nginx.conf, s'assurer que les logs sont en format combiné
# (format par défaut) pour une bonne lisibilité par Wazuh :
# access_log /var/log/nginx/access.log combined;
# error_log  /var/log/nginx/error.log warn;

# Ajouter une règle rsyslog pour ingérer les logs Nginx
sudo nano /etc/rsyslog.d/10-nginx.conf
```

```conf
# Module d'entrée de fichier : surveille le fichier Nginx access.log
# comme tail -f et envoie chaque nouvelle ligne dans rsyslog
module(load="imfile" PollingInterval="10")

input(type="imfile"
      File="/var/log/nginx/access.log"
      Tag="nginx-access"
      Severity="info"
      Facility="local6")

input(type="imfile"
      File="/var/log/nginx/error.log"
      Tag="nginx-error"
      Severity="warn"
      Facility="local6")
```

```bash
sudo systemctl restart rsyslog
```

---

## PHASE 5 — Wazuh SIEM sur machine virtuelle (PC)

### Raisonnement

Wazuh est un SIEM (Security Information and Event Management) open source.
Il centralise, corrèle, et alerte sur les événements de sécurité.

Architecture recommandée :
- **Hyperviseur** : VirtualBox (gratuit) ou VMware Player
- **OS de la VM** : Ubuntu Server 22.04 LTS
- **RAM VM** : 4 GB minimum (8 GB recommandé)
- **CPU VM** : 2 vCPU minimum
- **Disque** : 50 GB

### 5.1 · Installation de VirtualBox

Téléchargez VirtualBox depuis https://www.virtualbox.org/
Créez une VM avec Ubuntu Server 22.04 LTS.

**Configuration réseau de la VM** :
- Adaptateur 1 : NAT (accès Internet depuis la VM)
- Adaptateur 2 : Réseau hôte uniquement (host-only)
  Cela permet à la VM d'être joignable depuis le Pi
  via l'IP host-only (ex: 192.168.56.101)

### 5.2 · Installation de Wazuh (all-in-one)

```bash
# Dans la VM Ubuntu, en tant que root ou avec sudo

# Wazuh fournit un script d'installation officiel qui installe :
# - Wazuh Manager  : le cerveau, analyse les logs
# - Wazuh Indexer  : stockage (basé sur OpenSearch/Elasticsearch)
# - Wazuh Dashboard: interface web (basée sur Kibana)

# Téléchargement et vérification de l'intégrité du script
curl -sO https://packages.wazuh.com/4.7/wazuh-install.sh
curl -sO https://packages.wazuh.com/4.7/config.yml

# Édition du fichier config.yml :
# Indiquer l'IP de votre VM dans les champs nodes.indexer, nodes.server, nodes.dashboard
nano config.yml

# Génération des certificats TLS (communication chiffrée entre les composants)
bash wazuh-install.sh --generate-config-files

# Installation complète (--all = indexer + manager + dashboard)
# Cette étape prend 10 à 20 minutes
bash wazuh-install.sh --all-in-one

# À la fin, noter le mot de passe admin généré affiché à l'écran
```

### 5.3 · Réception des logs rsyslog du Pi

```bash
# Sur la VM Wazuh, configurer le Manager pour écouter les logs syslog
sudo nano /var/ossec/etc/ossec.conf
```

Ajouter dans la section `<ossec_config>` :

```xml
<!-- Réception des logs syslog depuis le Raspberry Pi -->
<!-- Le manager écoute sur le port 514 TCP -->
<remote>
  <connection>syslog</connection>
  <port>514</port>
  <protocol>tcp</protocol>
  <!-- Restreindre aux IPs autorisées — IP de votre Pi -->
  <allowed-ips>192.168.1.42</allowed-ips>
</remote>
```

```bash
# Redémarrage du manager Wazuh
sudo systemctl restart wazuh-manager

# Vérification que le port 514 est bien en écoute
sudo ss -tlnp | grep 514

# Test depuis le Pi : envoi manuel d'un log de test
logger -n 192.168.1.100 -P 514 "TEST LOG depuis RaspberryPi5"
```

### 5.4 · Accès au dashboard Wazuh

Ouvrez votre navigateur sur votre PC hôte :
```
https://192.168.56.101  (IP host-only de votre VM)
Login : admin
Mot de passe : (celui noté lors de l'installation)
```

---

## RÉCAPITULATIF SÉCURITÉ

| Couche          | Outil              | Rôle                                      |
|-----------------|--------------------|-------------------------------------------|
| Périmètre       | UFW                | Pare-feu, politique deny-by-default       |
| Anti-brute-force| Fail2ban           | Ban automatique des IPs malveillantes     |
| Exposition web  | Nginx              | Reverse proxy, headers sécu, rate-limit   |
| Accès distant   | Cloudflare Tunnel  | Pas d'ouverture de port entrant           |
| Logging         | rsyslog            | Collecte et transfert des logs            |
| Analyse         | Wazuh              | Corrélation, alertes, tableaux de bord    |
| Processus       | systemd + venv     | Isolation des services                    |

## COMMANDES DE DIAGNOSTIC UTILES

```bash
# Voir les logs en temps réel sur le Pi
sudo journalctl -f

# Status de tous les services du projet
sudo systemctl status rpi-monitor nginx fail2ban rsyslog

# Vérifier les connexions actives (qui est connecté au Pi ?)
ss -tlnp

# Top 10 des IPs qui accèdent à Nginx
sudo awk '{print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -10

# Tester la résilience du rate-limiting Nginx
# (depuis votre PC, adapter l'IP)
for i in {1..20}; do curl -s -o /dev/null -w "%{http_code}\n" http://192.168.1.42/api/stats; done
```
