#!/usr/bin/env python3
"""
api.py — Backend Flask pour le dashboard système Raspberry Pi 5
Expose : GET /api/stats  (JSON)
Auteur  : Dashboard RPI-5 Monitor
Sécurité : écoute UNIQUEMENT sur 127.0.0.1 par défaut.
           Nginx fait office de reverse proxy pour l'exposition externe.
"""

import psutil
import socket
import subprocess
import time
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ── CORS restreint à l'origine locale uniquement ──────────────
# En prod, remplacez "*" par votre domaine exact, ex :
# CORS(app, origins=["https://mondomaine.fr"])
CORS(app, origins=["http://localhost", "http://127.0.0.1"])

# ─── Variables pour calcul du débit réseau ────────────────────
_prev_net  = psutil.net_io_counters()
_prev_time = time.time()


def human_bytes(n):
    """Convertit un nombre d'octets en chaîne lisible (KB, MB, GB)."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_cpu():
    """Charge CPU globale et par cœur + fréquence actuelle."""
    freq = psutil.cpu_freq()
    return {
        "total":    psutil.cpu_percent(interval=0.5),
        "per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cores":    psutil.cpu_count(logical=True),
        "freq":     freq.current if freq else 0   # MHz
    }


def get_ram():
    """Mémoire vive : pourcentage utilisé + volumes humains."""
    m = psutil.virtual_memory()
    return {
        "pct":   m.percent,
        "used":  human_bytes(m.used),
        "total": human_bytes(m.total),
        "free":  human_bytes(m.available)
    }


def get_temperature():
    """
    Lecture de la température CPU.
    Sur Raspberry Pi, le capteur principal est dans /sys/class/thermal.
    psutil.sensors_temperatures() renvoie un dict ; on cherche 'cpu_thermal'.
    La valeur est en degrés Celsius.
    """
    temps = psutil.sensors_temperatures() if hasattr(psutil, 'sensors_temperatures') else {}
    cpu_temp = 0.0
    for key in ('cpu_thermal', 'cpu-thermal', 'coretemp', 'k10temp'):
        if key in temps and temps[key]:
            cpu_temp = temps[key][0].current
            break

    # Détection du throttling : sur RPi, vcgencmd donne l'état exact.
    # 0x0 = tout OK ; toute autre valeur indique un throttling passé ou actif.
    throttled = False
    try:
        out = subprocess.check_output(
            ['vcgencmd', 'get_throttled'], timeout=1, text=True
        )
        # Exemple de sortie : "throttled=0x50000"
        val = int(out.strip().split('=')[1], 16)
        throttled = val != 0
    except Exception:
        pass   # vcgencmd absent (hors RPi) → on laisse False

    return {"cpu": cpu_temp, "throttled": throttled}


def get_uptime():
    """
    Calcule le temps de fonctionnement depuis psutil.boot_time().
    Retourne une chaîne "Xd Xh Xm".
    """
    boot   = psutil.boot_time()
    uptime = int(time.time() - boot)
    days, r = divmod(uptime, 86400)
    hours, r = divmod(r, 3600)
    mins  = r // 60
    return f"{days}d {hours}h {mins}m"


def get_network():
    """
    Statistiques réseau cumulées + débit instantané (KB/s).
    Le débit est calculé en comparant les compteurs depuis le dernier appel.
    On identifie l'interface active (celle avec l'IP de la passerelle).
    """
    global _prev_net, _prev_time

    cur      = psutil.net_io_counters()
    now      = time.time()
    elapsed  = now - _prev_time or 0.001

    rx_rate  = (cur.bytes_recv - _prev_net.bytes_recv) / elapsed / 1024
    tx_rate  = (cur.bytes_sent - _prev_net.bytes_sent) / elapsed / 1024

    _prev_net  = cur
    _prev_time = now

    # Récupération de l'IP locale principale
    ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except Exception:
        pass

    return {
        "ip":          ip,
        "bytes_recv":  human_bytes(cur.bytes_recv),
        "bytes_sent":  human_bytes(cur.bytes_sent),
        "rx_rate":     f"{rx_rate:.1f}",
        "tx_rate":     f"{tx_rate:.1f}"
    }


def get_disks():
    """
    Retourne l'utilisation de chaque partition montée (sauf les pseudo-fs).
    Exclut tmpfs, devtmpfs, squashfs pour rester lisible.
    """
    excluded_types = {'tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs'}
    results = []
    for part in psutil.disk_partitions():
        if part.fstype in excluded_types:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            results.append({
                "mountpoint": part.mountpoint,
                "pct":   usage.percent,
                "used":  human_bytes(usage.used),
                "total": human_bytes(usage.total),
                "free":  human_bytes(usage.free)
            })
        except PermissionError:
            continue
    return results


def get_top_processes(n=5):
    """
    Récupère les N processus les plus gourmands en CPU.
    Remarque : cpu_percent() nécessite un premier appel pour initialiser
    le compteur ; la valeur est donc l'usage depuis le dernier polling.
    """
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            procs.append({
                "pid":    p.info['pid'],
                "name":   p.info['name'],
                "cpu":    p.info['cpu_percent'] or 0.0,
                "mem":    p.info['memory_percent'] or 0.0,
                "status": p.info['status']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    procs.sort(key=lambda x: x['cpu'], reverse=True)
    return procs[:n]


# ─── Endpoint principal ───────────────────────────────────────
@app.route('/api/stats')
def stats():
    """
    Agrège toutes les métriques système et les retourne en JSON.
    L'en-tête X-Content-Type-Options empêche le MIME-sniffing.
    """
    load = psutil.getloadavg()
    data = {
        "cpu":        get_cpu(),
        "ram":        get_ram(),
        "temp":       get_temperature(),
        "uptime":     get_uptime(),
        "load":       list(load),
        "proc_count": len(psutil.pids()),
        "network":    get_network(),
        "disks":      get_disks(),
        "processes":  get_top_processes()
    }
    resp = jsonify(data)
    # En-têtes de sécurité HTTP minimaux
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control']          = 'no-store'
    return resp


@app.route('/health')
def health():
    """Endpoint de healthcheck pour Nginx/monitoring."""
    return jsonify({"status": "ok"}), 200


# ─── Point d'entrée ───────────────────────────────────────────
if __name__ == '__main__':
    # SÉCURITÉ : on écoute uniquement sur localhost.
    # Nginx exposera le service en HTTP/HTTPS vers l'extérieur.
    app.run(host='127.0.0.1', port=5000, debug=False)
