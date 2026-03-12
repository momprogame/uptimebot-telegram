#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import requests
import threading
import time
import json
import sqlite3
import subprocess
import socket
import ssl
import whois
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Cargar variables de entorno
load_dotenv()

# ===== CONFIGURACIÓN =====
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
UPTIME_API_KEY = os.getenv('UPTIME_API_KEY')
UPTIME_API_URL = 'https://api.uptimerobot.com/v2'
PORT = int(os.getenv('PORT', 8080))
RENDER_URL = os.getenv('RENDER_EXTERNAL_HOSTNAME', 'uptimebot-telegram.onrender.com')

# TU ID DE ADMIN (ÚNICO USUARIO AUTORIZADO)
ADMIN_ID = 7970466590

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Verificar configuración
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN no está configurado!")
    exit(1)

# ===== BASE DE DATOS =====
def init_db():
    """Inicializa la base de datos SQLite"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    
    # Tabla de monitores personalizados (no Uptime Robot)
    c.execute('''CREATE TABLE IF NOT EXISTS custom_monitors
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  url TEXT UNIQUE,
                  name TEXT,
                  port INTEGER DEFAULT 80,
                  check_interval INTEGER DEFAULT 300,
                  last_check TIMESTAMP,
                  last_status INTEGER,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Tabla de logs de ping
    c.execute('''CREATE TABLE IF NOT EXISTS ping_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  host TEXT,
                  response_time REAL,
                  status TEXT,
                  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Tabla de escaneo de puertos
    c.execute('''CREATE TABLE IF NOT EXISTS port_scans
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  host TEXT,
                  port INTEGER,
                  service TEXT,
                  status TEXT,
                  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Tabla de configuración
    c.execute('''CREATE TABLE IF NOT EXISTS config
                 (key TEXT PRIMARY KEY,
                  value TEXT)''')
    
    conn.commit()
    conn.close()

# ===== FUNCIONES DE UTILIDAD =====
def is_admin(user_id):
    """Verifica si el usuario es el admin"""
    return user_id == ADMIN_ID

def format_time(seconds):
    """Formatea tiempo en segundos a texto legible"""
    if seconds < 60:
        return f"{seconds} seg"
    elif seconds < 3600:
        return f"{seconds//60} min"
    elif seconds < 86400:
        return f"{seconds//3600} h"
    else:
        return f"{seconds//86400} d"

def get_status_emoji(status):
    """Convierte estado a emoji"""
    if status == 200:
        return "✅"
    elif status >= 400:
        return "❌"
    elif status == -1:
        return "⏳"
    else:
        return "⚠️"

# ===== FUNCIONES DE MONITOREO PERSONALIZADO =====
def check_website(url):
    """Verifica si un sitio web está funcionando"""
    try:
        start_time = time.time()
        response = requests.get(url, timeout=10, allow_redirects=True)
        response_time = round((time.time() - start_time) * 1000)  # ms
        return {
            'status': response.status_code,
            'time': response_time,
            'online': response.status_code < 400
        }
    except requests.exceptions.Timeout:
        return {'status': 408, 'time': None, 'online': False}
    except requests.exceptions.ConnectionError:
        return {'status': -1, 'time': None, 'online': False}
    except Exception as e:
        return {'status': -2, 'time': None, 'online': False}

def ping_host(host):
    """Ping a un host"""
    try:
        start = time.time()
        response = subprocess.run(['ping', '-c', '1', '-W', '2', host], 
                                 capture_output=True, text=True, timeout=5)
        elapsed = round((time.time() - start) * 1000)
        
        if response.returncode == 0:
            return {'success': True, 'time': elapsed, 'output': response.stdout}
        else:
            return {'success': False, 'time': None, 'output': response.stderr}
    except Exception as e:
        return {'success': False, 'time': None, 'error': str(e)}

def scan_ports(host, ports=[21,22,23,25,53,80,110,111,135,139,143,443,445,993,995,1723,3306,3389,5900,8080]):
    """Escanea puertos comunes en un host"""
    results = []
    for port in ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            if result == 0:
                try:
                    service = socket.getservbyport(port)
                except:
                    service = "unknown"
                results.append({'port': port, 'status': 'open', 'service': service})
            sock.close()
        except:
            pass
    return results

def check_ssl_cert(host):
    """Verifica certificado SSL"""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                expires = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                days_left = (expires - datetime.now()).days
                return {
                    'valid': True,
                    'issuer': dict(x[0] for x in cert['issuer']),
                    'expires': expires,
                    'days_left': days_left
                }
    except Exception as e:
        return {'valid': False, 'error': str(e)}

def check_domain(domain):
    """Obtiene información WHOIS del dominio"""
    try:
        w = whois.whois(domain)
        return {
            'registrar': w.registrar,
            'creation_date': w.creation_date,
            'expiration_date': w.expiration_date,
            'name_servers': w.name_servers
        }
    except:
        return None

# ===== FUNCIÓN DE NOTIFICACIONES =====
def notification_worker():
    """Hilo que verifica monitores y envía notificaciones"""
    while True:
        try:
            # Obtener monitores de Uptime Robot
            response = requests.post(
                f'{UPTIME_API_URL}/getMonitors',
                data={
                    'api_key': UPTIME_API_KEY,
                    'format': 'json',
                    'logs': 1
                },
                timeout=30
            )
            
            data = response.json()
            
            if data.get('stat') == 'ok':
                monitors = data.get('monitors', [])
                
                if monitors:
                    # Verificar cambios de estado
                    conn = sqlite3.connect('bot_config.db')
                    c = conn.cursor()
                    
                    for monitor in monitors:
                        monitor_id = monitor['id']
                        current_status = monitor['status']
                        
                        c.execute("SELECT last_status FROM monitor_status WHERE monitor_id = ?", (monitor_id,))
                        result = c.fetchone()
                        last_status = result[0] if result else None
                        
                        if last_status is not None and last_status != current_status:
                            # Estado cambiado - enviar notificación
                            emoji = "✅" if current_status == 2 else "❌" if current_status == 9 else "⚠️"
                            status_text = "ONLINE" if current_status == 2 else "OFFLINE" if current_status == 9 else "DESCONOCIDO"
                            
                            message = (
                                f"🚨 *ALERTA DE MONITOREO*\n\n"
                                f"{emoji} *{monitor['friendly_name']}*\n"
                                f"📌 ID: `{monitor_id}`\n"
                                f"🌐 URL: {monitor['url']}\n"
                                f"📊 Estado: {status_text}\n\n"
                                f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                            )
                            
                            # Enviar a Telegram
                            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                            payload = {
                                'chat_id': ADMIN_ID,
                                'text': message,
                                'parse_mode': 'Markdown'
                            }
                            requests.post(url, data=payload, timeout=10)
                        
                        # Actualizar estado
                        c.execute('''INSERT OR REPLACE INTO monitor_status 
                                   (monitor_id, last_status, last_check) 
                                   VALUES (?, ?, ?)''',
                                (monitor_id, current_status, datetime.now().isoformat()))
                    
                    conn.commit()
                    conn.close()
            
        except Exception as e:
            logger.error(f"❌ Error en notification_worker: {e}")
        
        time.sleep(60)  # Revisar cada minuto

# ===== FUNCIÓN DE AUTO-PING =====
def keep_alive():
    """Mantiene el servicio despierto"""
    url = f"https://{RENDER_URL}"
    while True:
        try:
            requests.get(url, timeout=30)
        except:
            pass
        time.sleep(480)

# ===== PANEL PRINCIPAL ESTÉTICO =====
def create_main_panel():
    """Crea el panel principal con botones estéticos"""
    keyboard = [
        [InlineKeyboardButton("🌐 MONITOREAR WEB", callback_data='add_web')],
        [InlineKeyboardButton("📊 ESTADO DE WEBS", callback_data='status')],
        [InlineKeyboardButton("📈 MÉTRICAS", callback_data='metrics')],
        [InlineKeyboardButton("🏓 PING", callback_data='ping')],
        [InlineKeyboardButton("🔍 ESCANEAR PUERTOS", callback_data='ports')],
        [InlineKeyboardButton("✅ VERIFICAR WEB", callback_data='isup')],
        [InlineKeyboardButton("🔐 SSL CERTIFICADO", callback_data='ssl_check')],
        [InlineKeyboardButton("🌍 INFO DOMINIO", callback_data='domain_info')],
        [InlineKeyboardButton("⚙️ EDITAR WEBS", callback_data='edit_web')],
        [InlineKeyboardButton("❌ CANCELAR", callback_data='cancel')],
        [InlineKeyboardButton("🆘 AYUDA", callback_data='help')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_cancel_button():
    """Botón de cancelar"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCELAR", callback_data='cancel')]])

# ===== HANDLERS PRINCIPALES =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Muestra panel principal"""
    user_id = update.effective_user.id
    
    # Verificar si es admin
    if not is_admin(user_id):
        await update.message.reply_text("❌ No estás autorizado para usar este bot.")
        return
    
    welcome = (
        "🤖 *WATCH BOT - PANEL DE CONTROL*\n\n"
        "👤 *Admin:* `@CaddisFly`\n"
        "🆔 *Tu ID:* `7970466590`\n\n"
        "📋 *COMANDOS DISPONIBLES:*\n"
        "─────────────────\n"
        "🌐 `/add` - Monitorear nueva web\n"
        "⚙️ `/edit` - Editar configuración\n"
        "📊 `/status` - Estado de webs\n"
        "📈 `/metrics` - Métricas de respuesta\n"
        "🏓 `/ping` - Ping a un host\n"
        "🔍 `/ports` - Escanear puertos\n"
        "✅ `/isup` - Verificar si está online\n"
        "🔐 `/ssl` - Ver certificado SSL\n"
        "🌍 `/domain` - Info WHOIS\n"
        "❌ `/cancel` - Cancelar comando\n"
        "🆘 `/help` - Mostrar ayuda\n\n"
        "─────────────────\n"
        "👇 *SELECCIONA UNA OPCIÓN:*"
    )
    
    await update.message.reply_text(welcome, parse_mode='Markdown', reply_markup=create_main_panel())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones del panel"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ No autorizado")
        return
    
    if query.data == 'add_web':
        await query.edit_message_text(
            "🌐 *AÑADIR WEB A MONITOREAR*\n\n"
            "Envía la URL completa:\n"
            "`https://ejemplo.com`\n\n"
            "O con nombre personalizado:\n"
            "`https://ejemplo.com Mi Sitio`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'add_web'
    
    elif query.data == 'status':
        await show_status(query, context)
    
    elif query.data == 'metrics':
        await query.edit_message_text(
            "📈 *MÉTRICAS DE RESPUESTA*\n\n"
            "Envía la URL para ver métricas:",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'metrics'
    
    elif query.data == 'ping':
        await query.edit_message_text(
            "🏓 *PING A HOST*\n\n"
            "Envía el host o IP para hacer ping:\n"
            "`google.com`\n"
            "`8.8.8.8`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'ping'
    
    elif query.data == 'ports':
        await query.edit_message_text(
            "🔍 *ESCANEAR PUERTOS*\n\n"
            "Envía el host o IP para escanear:\n"
            "`ejemplo.com`\n"
            "`192.168.1.1`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'ports'
    
    elif query.data == 'isup':
        await query.edit_message_text(
            "✅ *VERIFICAR WEB*\n\n"
            "Envía la URL para verificar:\n"
            "`https://ejemplo.com`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'isup'
    
    elif query.data == 'ssl_check':
        await query.edit_message_text(
            "🔐 *VERIFICAR SSL*\n\n"
            "Envía el dominio para ver certificado:\n"
            "`ejemplo.com`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'ssl'
    
    elif query.data == 'domain_info':
        await query.edit_message_text(
            "🌍 *INFO DEL DOMINIO*\n\n"
            "Envía el dominio para información WHOIS:\n"
            "`ejemplo.com`",
            parse_mode='Markdown',
            reply_markup=create_cancel_button()
        )
        context.user_data['awaiting'] = 'domain'
    
    elif query.data == 'edit_web':
        await show_edit_menu(query, context)
    
    elif query.data == 'cancel':
        await query.edit_message_text(
            "❌ Comando cancelado.\n\nVolviendo al panel...",
            reply_markup=create_main_panel()
        )
        context.user_data.clear()
    
    elif query.data == 'help':
        await show_help(query, context)

async def show_status(query, context):
    """Muestra estado de todas las webs"""
    try:
        await query.edit_message_text("🔄 Consultando estado...")
        
        response = requests.post(
            f'{UPTIME_API_URL}/getMonitors',
            data={
                'api_key': UPTIME_API_KEY,
                'format': 'json',
                'logs': 1,
                'response_times': 1
            },
            timeout=30
        )
        
        data = response.json()
        
        if data.get('stat') == 'ok':
            monitors = data.get('monitors', [])
            
            if not monitors:
                await query.message.reply_text(
                    "📭 No hay webs configuradas.",
                    reply_markup=create_main_panel()
                )
                return
            
            total = len(monitors)
            online = sum(1 for m in monitors if m['status'] == 2)
            offline = sum(1 for m in monitors if m['status'] == 9)
            
            header = (
                f"📊 *ESTADO DE WEBS*\n"
                f"─────────────────\n"
                f"📌 Total: {total}\n"
                f"✅ Online: {online}\n"
                f"❌ Offline: {offline}\n"
                f"─────────────────\n\n"
            )
            
            await query.message.reply_text(header, parse_mode='Markdown')
            
            for monitor in monitors:
                emoji = "✅" if monitor['status'] == 2 else "❌" if monitor['status'] == 9 else "⚠️"
                nombre = monitor.get('friendly_name', 'Sin nombre')
                url = monitor.get('url', 'URL no disponible')
                
                # Último tiempo de respuesta
                last_response = "N/A"
                if monitor.get('response_times'):
                    last_response = f"{monitor['response_times'][0].get('value', 0)}ms"
                
                msg = (
                    f"{emoji} *{nombre}*\n"
                    f"├─ URL: `{url}`\n"
                    f"├─ ID: `{monitor['id']}`\n"
                    f"└─ Respuesta: {last_response}\n"
                )
                
                await query.message.reply_text(msg, parse_mode='Markdown')
            
            await query.message.reply_text(
                "✅ Consulta completada",
                reply_markup=create_main_panel()
            )
        else:
            await query.message.reply_text(
                f"❌ Error: {data.get('error', {}).get('message', 'Error')}",
                reply_markup=create_main_panel()
            )
            
    except Exception as e:
        await query.message.reply_text(
            "❌ Error al consultar",
            reply_markup=create_main_panel()
        )

async def show_edit_menu(query, context):
    """Muestra menú para editar webs"""
    try:
        response = requests.post(
            f'{UPTIME_API_URL}/getMonitors',
            data={'api_key': UPTIME_API_KEY, 'format': 'json'},
            timeout=30
        )
        
        data = response.json()
        
        if data.get('stat') == 'ok' and data.get('monitors'):
            keyboard = []
            for monitor in data['monitors']:
                nombre = monitor.get('friendly_name', 'Sin nombre')[:20]
                keyboard.append([InlineKeyboardButton(
                    f"✏️ {nombre}", 
                    callback_data=f"edit_{monitor['id']}"
                )])
            
            keyboard.append([InlineKeyboardButton("🔙 VOLVER", callback_data='menu')])
            
            await query.edit_message_text(
                "✏️ *SELECCIONA WEB A EDITAR*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text(
                "📭 No hay webs para editar",
                reply_markup=create_main_panel()
            )
            
    except Exception as e:
        await query.edit_message_text(
            "❌ Error al cargar",
            reply_markup=create_main_panel()
        )

async def show_help(query, context):
    """Muestra ayuda completa"""
    help_text = (
        "🆘 *AYUDA - WATCH BOT*\n\n"
        "🤖 *COMANDOS DISPONIBLES:*\n"
        "─────────────────\n"
        "🌐 `/add` - Monitorear nueva web\n"
        "⚙️ `/edit` - Editar configuración\n"
        "📊 `/status` - Estado de webs\n"
        "📈 `/metrics` - Métricas de respuesta\n"
        "🏓 `/ping` - Ping a un host\n"
        "🔍 `/ports` - Escanear puertos\n"
        "✅ `/isup` - Verificar si está online\n"
        "🔐 `/ssl` - Ver certificado SSL\n"
        "🌍 `/domain` - Info WHOIS\n"
        "❌ `/cancel` - Cancelar comando\n"
        "🆘 `/help` - Mostrar ayuda\n\n"
        "─────────────────\n"
        "👤 *Admin:* `@CaddisFly`\n"
        "📢 *Canal:* @watch_bot_news\n"
        "💬 *Soporte:* @CaddisFly\n\n"
        "🤝 *Donaciones:* Para mantener el bot"
    )
    
    await query.edit_message_text(
        help_text,
        parse_mode='Markdown',
        reply_markup=create_main_panel()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ No autorizado")
        return
    
    text = update.message.text.strip()
    awaiting = context.user_data.get('awaiting')
    
    if awaiting == 'add_web':
        # Añadir web a Uptime Robot
        parts = text.split(' ', 1)
        url = parts[0]
        name = parts[1] if len(parts) > 1 else url.replace('https://', '').replace('http://', '').split('/')[0]
        
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text(
                "❌ URL inválida. Usa http:// o https://",
                reply_markup=create_main_panel()
            )
            context.user_data.clear()
            return
        
        await update.message.reply_text("🔄 Añadiendo web...")
        
        try:
            response = requests.post(
                f'{UPTIME_API_URL}/newMonitor',
                data={
                    'api_key': UPTIME_API_KEY,
                    'format': 'json',
                    'type': '1',
                    'url': url,
                    'friendly_name': name,
                    'interval': '300'
                },
                timeout=30
            )
            
            data = response.json()
            
            if data.get('stat') == 'ok':
                monitor = data.get('monitor', {})
                await update.message.reply_text(
                    f"✅ *WEB AÑADIDA*\n\n"
                    f"📌 ID: `{monitor.get('id')}`\n"
                    f"🌐 URL: {url}\n"
                    f"📝 Nombre: {name}\n"
                    f"⏱ Intervalo: 5 min",
                    parse_mode='Markdown',
                    reply_markup=create_main_panel()
                )
            else:
                await update.message.reply_text(
                    f"❌ Error: {data.get('error', {}).get('message', 'Error')}",
                    reply_markup=create_main_panel()
                )
        except Exception as e:
            await update.message.reply_text(
                "❌ Error al añadir",
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'ping':
        await update.message.reply_text(f"🏓 Haciendo ping a {text}...")
        
        result = ping_host(text)
        
        if result['success']:
            await update.message.reply_text(
                f"✅ *PING EXITOSO*\n\n"
                f"📍 Host: `{text}`\n"
                f"⏱ Tiempo: {result['time']}ms\n\n"
                f"```\n{result['output'][:500]}\n```",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                f"❌ *PING FALLÓ*\n\n📍 Host: `{text}`",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'ports':
        await update.message.reply_text(f"🔍 Escaneando puertos en {text}...")
        
        results = scan_ports(text)
        
        if results:
            msg = f"🔍 *PUERTOS ABIERTOS EN {text}*\n\n"
            for r in results:
                msg += f"├─ {r['port']}: {r['service']} (ABIERTO)\n"
            
            await update.message.reply_text(
                msg,
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                f"🔍 No se encontraron puertos abiertos en {text}",
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'isup':
        if not text.startswith(('http://', 'https://')):
            text = 'https://' + text
        
        await update.message.reply_text(f"✅ Verificando {text}...")
        
        result = check_website(text)
        
        if result['online']:
            await update.message.reply_text(
                f"✅ *WEB ONLINE*\n\n"
                f"📍 URL: {text}\n"
                f"📊 Status: {result['status']}\n"
                f"⏱ Tiempo: {result['time']}ms",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                f"❌ *WEB OFFLINE*\n\n📍 URL: {text}",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'ssl':
        await update.message.reply_text(f"🔐 Verificando SSL de {text}...")
        
        result = check_ssl_cert(text)
        
        if result['valid']:
            color = "🟢" if result['days_left'] > 30 else "🟡" if result['days_left'] > 7 else "🔴"
            await update.message.reply_text(
                f"🔐 *CERTIFICADO SSL*\n\n"
                f"📍 Dominio: {text}\n"
                f"{color} Válido: Sí\n"
                f"📅 Expira: {result['expires'].strftime('%d/%m/%Y')}\n"
                f"⏱ Días restantes: {result['days_left']}\n"
                f"🏢 Emisor: {result['issuer'].get('organizationName', 'N/A')}",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                f"❌ *SSL NO VÁLIDO*\n\n📍 Dominio: {text}",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'domain':
        await update.message.reply_text(f"🌍 Obteniendo info de {text}...")
        
        info = check_domain(text)
        
        if info:
            creation = info['creation_date'][0] if isinstance(info['creation_date'], list) else info['creation_date']
            expiration = info['expiration_date'][0] if isinstance(info['expiration_date'], list) else info['expiration_date']
            
            await update.message.reply_text(
                f"🌍 *INFO DEL DOMINIO*\n\n"
                f"📍 Dominio: {text}\n"
                f"📅 Creado: {creation.strftime('%d/%m/%Y') if creation else 'N/A'}\n"
                f"⏱ Expira: {expiration.strftime('%d/%m/%Y') if expiration else 'N/A'}\n"
                f"🏢 Registrar: {info['registrar'] or 'N/A'}\n\n"
                f"📋 Nameservers:\n"
                f"{chr(10).join(['├─ ' + ns for ns in (info['name_servers'] or [])[:3]])}",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                f"❌ No se pudo obtener info de {text}",
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()
    
    elif awaiting == 'metrics':
        if not text.startswith(('http://', 'https://')):
            text = 'https://' + text
        
        await update.message.reply_text(f"📈 Obteniendo métricas de {text}...")
        
        # Simular métricas con 5 pings
        times = []
        for i in range(5):
            result = check_website(text)
            if result['time']:
                times.append(result['time'])
            time.sleep(1)
        
        if times:
            avg = sum(times) / len(times)
            max_t = max(times)
            min_t = min(times)
            
            await update.message.reply_text(
                f"📈 *MÉTRICAS DE RESPUESTA*\n\n"
                f"📍 URL: {text}\n"
                f"─────────────────\n"
                f"📊 Promedio: {avg:.0f}ms\n"
                f"📈 Máximo: {max_t}ms\n"
                f"📉 Mínimo: {min_t}ms\n"
                f"📋 Muestras: {len(times)}\n"
                f"─────────────────",
                parse_mode='Markdown',
                reply_markup=create_main_panel()
            )
        else:
            await update.message.reply_text(
                "❌ No se pudieron obtener métricas",
                reply_markup=create_main_panel()
            )
        
        context.user_data.clear()

async def post_init(application: Application):
    """Post initialization"""
    logger.info("🤖 WATCH BOT iniciado!")
    logger.info(f"👤 Admin ID: {ADMIN_ID}")
    logger.info("🔔 Notificaciones activadas")

def main():
    """Función principal"""
    try:
        # Inicializar BD
        init_db()
        
        # Iniciar hilos
        threading.Thread(target=keep_alive, daemon=True).start()
        threading.Thread(target=notification_worker, daemon=True).start()
        
        # Crear aplicación
        application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
        
        # Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("add", lambda u,c: start(u,c)))  # Redirigir a /start
        application.add_handler(CommandHandler("edit", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("status", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("metrics", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("ping", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("ports", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("isup", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("ssl", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("domain", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("cancel", lambda u,c: start(u,c)))
        application.add_handler(CommandHandler("help", lambda u,c: start(u,c)))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Webhook para Render
        webhook_url = f"https://{RENDER_URL}/{TELEGRAM_TOKEN}"
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=webhook_url
        )
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")

if __name__ == '__main__':
    main()
