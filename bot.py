#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import requests
import threading
import time
import json
import sqlite3
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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 300))  # 5 minutos por defecto

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
if not UPTIME_API_KEY:
    logger.error("❌ UPTIME_API_KEY no está configurada!")
    exit(1)

# ===== BASE DE DATOS PARA CONFIGURACIÓN =====
def init_db():
    """Inicializa la base de datos SQLite"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    
    # Tabla de configuración de usuarios
    c.execute('''CREATE TABLE IF NOT EXISTS user_config
                 (user_id INTEGER PRIMARY KEY,
                  check_interval INTEGER DEFAULT 300,
                  notifications_enabled INTEGER DEFAULT 1,
                  notify_on_down INTEGER DEFAULT 1,
                  notify_on_up INTEGER DEFAULT 1,
                  notify_on_pause INTEGER DEFAULT 0,
                  last_check TIMESTAMP)''')
    
    # Tabla de estado de monitores (para evitar notificaciones duplicadas)
    c.execute('''CREATE TABLE IF NOT EXISTS monitor_status
                 (monitor_id INTEGER,
                  user_id INTEGER,
                  last_status INTEGER,
                  last_notification TIMESTAMP,
                  PRIMARY KEY (monitor_id, user_id))''')
    
    conn.commit()
    conn.close()

def get_user_config(user_id):
    """Obtiene la configuración de un usuario"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    c.execute("SELECT * FROM user_config WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {
            'user_id': result[0],
            'check_interval': result[1],
            'notifications_enabled': bool(result[2]),
            'notify_on_down': bool(result[3]),
            'notify_on_up': bool(result[4]),
            'notify_on_pause': bool(result[5]),
            'last_check': result[6]
        }
    else:
        # Configuración por defecto
        default_config = {
            'user_id': user_id,
            'check_interval': 300,
            'notifications_enabled': True,
            'notify_on_down': True,
            'notify_on_up': True,
            'notify_on_pause': False,
            'last_check': None
        }
        save_user_config(user_id, default_config)
        return default_config

def save_user_config(user_id, config):
    """Guarda la configuración de un usuario"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO user_config 
                 (user_id, check_interval, notifications_enabled, notify_on_down, notify_on_up, notify_on_pause, last_check)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (user_id, config['check_interval'], int(config['notifications_enabled']),
               int(config['notify_on_down']), int(config['notify_on_up']),
               int(config['notify_on_pause']), config['last_check']))
    conn.commit()
    conn.close()

def update_monitor_status(monitor_id, user_id, status):
    """Actualiza el estado de un monitor para un usuario"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR REPLACE INTO monitor_status
                 (monitor_id, user_id, last_status, last_notification)
                 VALUES (?, ?, ?, ?)''',
              (monitor_id, user_id, status, now))
    conn.commit()
    conn.close()

def get_monitor_status(monitor_id, user_id):
    """Obtiene el último estado conocido de un monitor"""
    conn = sqlite3.connect('bot_config.db')
    c = conn.cursor()
    c.execute("SELECT last_status FROM monitor_status WHERE monitor_id = ? AND user_id = ?",
              (monitor_id, user_id))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

# ===== FUNCIÓN DE NOTIFICACIONES EN SEGUNDO PLANO =====
def notification_worker():
    """Hilo que verifica el estado de los monitores y envía notificaciones"""
    while True:
        try:
            # Obtener todos los usuarios con notificaciones activadas
            conn = sqlite3.connect('bot_config.db')
            c = conn.cursor()
            c.execute("SELECT user_id, check_interval FROM user_config WHERE notifications_enabled = 1")
            users = c.fetchall()
            conn.close()
            
            for user_id, interval in users:
                # Verificar si es momento de revisar (cada usuario tiene su intervalo)
                user_config = get_user_config(user_id)
                last_check = user_config['last_check']
                
                if last_check:
                    last_check_time = datetime.fromisoformat(last_check)
                    if datetime.now() - last_check_time < timedelta(seconds=interval):
                        continue
                
                # Obtener monitores de Uptime Robot
                response = requests.post(
                    f'{UPTIME_API_URL}/getMonitors',
                    data={
                        'api_key': UPTIME_API_KEY,
                        'format': 'json'
                    },
                    timeout=30
                )
                
                data = response.json()
                
                if data.get('stat') == 'ok':
                    monitors = data.get('monitors', [])
                    
                    for monitor in monitors:
                        monitor_id = monitor['id']
                        current_status = monitor['status']
                        last_status = get_monitor_status(monitor_id, user_id)
                        
                        # Si el estado cambió, enviar notificación
                        if last_status is not None and last_status != current_status:
                            should_notify = False
                            
                            if current_status == 2 and user_config['notify_on_up']:  # Online
                                should_notify = True
                                message = f"✅ *{monitor['friendly_name']} está ONLINE*\n\nLa web ha vuelto a funcionar."
                            elif current_status == 9 and user_config['notify_on_down']:  # Offline
                                should_notify = True
                                message = f"❌ *{monitor['friendly_name']} está OFFLINE*\n\nLa web no responde. ¡Revisa!"
                            elif current_status == 0 and user_config['notify_on_pause']:  # Pausado
                                should_notify = True
                                message = f"⏸ *{monitor['friendly_name']} está PAUSADO*\n\nEl monitoreo ha sido pausado."
                            
                            if should_notify:
                                try:
                                    # Enviar notificación a Telegram
                                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                    payload = {
                                        'chat_id': user_id,
                                        'text': message,
                                        'parse_mode': 'Markdown'
                                    }
                                    requests.post(url, data=payload, timeout=10)
                                    logger.info(f"✅ Notificación enviada a {user_id} para monitor {monitor_id}")
                                except Exception as e:
                                    logger.error(f"❌ Error enviando notificación: {e}")
                        
                        # Actualizar estado en BD
                        update_monitor_status(monitor_id, user_id, current_status)
                
                # Actualizar último check del usuario
                user_config['last_check'] = datetime.now().isoformat()
                save_user_config(user_id, user_config)
                
        except Exception as e:
            logger.error(f"❌ Error en notification_worker: {e}")
        
        # Esperar 30 segundos antes de la próxima verificación
        time.sleep(30)

# ===== FUNCIÓN PARA MANTENER EL BOT DESPIERTO =====
def keep_alive():
    """Mantiene el servicio despierto haciéndose ping a sí mismo"""
    url = f"https://{RENDER_URL}"
    while True:
        try:
            response = requests.get(url, timeout=30)
            logger.info(f"✅ Auto-ping exitoso a {url} - Status: {response.status_code}")
        except Exception as e:
            logger.error(f"❌ Error en auto-ping: {e}")
        time.sleep(480)  # Cada 8 minutos

# ===== FUNCIONES AUXILIARES =====
def get_status_emoji(status):
    """Convierte el código de estado a emoji"""
    status_map = {
        0: ('⏸️', 'Pausado', '⚪'),
        1: ('🔄', 'Iniciando', '🟡'),
        2: ('✅', 'Online', '🟢'),
        8: ('⚠️', 'Parece offline', '🟠'),
        9: ('❌', 'Offline', '🔴')
    }
    return status_map.get(status, ('❓', 'Desconocido', '⚫'))

def format_interval(seconds):
    """Formatea segundos a texto legible"""
    if seconds < 60:
        return f"{seconds} segundos"
    elif seconds < 3600:
        return f"{seconds // 60} minutos"
    else:
        return f"{seconds // 3600} horas"

def create_main_menu():
    """Crea el teclado del menú principal"""
    keyboard = [
        [InlineKeyboardButton("📊 Ver Estado", callback_data='status')],
        [InlineKeyboardButton("➕ Añadir Web", callback_data='add_web'),
         InlineKeyboardButton("❌ Eliminar Web", callback_data='delete_web')],
        [InlineKeyboardButton("🔔 Configurar Notificaciones", callback_data='notification_settings'),
         InlineKeyboardButton("⚙️ Ajustes Generales", callback_data='settings')],
        [InlineKeyboardButton("❓ Ayuda", callback_data='help')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_button():
    """Crea botón de volver"""
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data='menu')]]
    return InlineKeyboardMarkup(keyboard)

# ===== HANDLERS PRINCIPALES =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Muestra menú principal"""
    user = update.effective_user
    user_id = user.id
    
    # Inicializar configuración del usuario
    get_user_config(user_id)
    
    welcome_msg = (
        f"🤖 *Bienvenido {user.first_name}!*\n\n"
        "Soy tu asistente de monitoreo de Uptime Robot\n\n"
        "🔍 *Características:*\n"
        "• Notificaciones automáticas cuando una web cae o sube\n"
        "• Configuración personalizada del intervalo de chequeo\n"
        "• Elegir qué eventos notificar\n"
        "• Ver estado en tiempo real\n"
        "• Añadir/eliminar monitores\n\n"
        "Selecciona una opción del menú:"
    )
    await update.message.reply_text(welcome_msg, parse_mode='Markdown', reply_markup=create_main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vuelve al menú principal"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📌 *Menú Principal*\n\nSelecciona una opción:",
        parse_mode='Markdown',
        reply_markup=create_main_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones del menú"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'status':
        await show_status(query, context)
    elif query.data == 'add_web':
        await query.edit_message_text(
            "🌐 *Añadir Nueva Web*\n\n"
            "Envía la URL que deseas monitorear.\n"
            "Ejemplo: `https://ejemplo.com`\n\n"
            "También puedes añadir un nombre personalizado:\n"
            "`https://ejemplo.com Mi Web`",
            parse_mode='Markdown',
            reply_markup=create_back_button()
        )
        context.user_data['waiting_for_url'] = True
    elif query.data == 'delete_web':
        await show_delete_menu(query, context)
    elif query.data == 'notification_settings':
        await show_notification_settings(query, context)
    elif query.data == 'settings':
        await show_settings(query, context)
    elif query.data == 'help':
        await show_help(query, context)
    elif query.data.startswith('delete_'):
        monitor_id = query.data.replace('delete_', '')
        await confirm_delete(query, context, monitor_id)
    elif query.data.startswith('set_interval_'):
        interval = int(query.data.replace('set_interval_', ''))
        await set_interval(query, context, interval)
    elif query.data.startswith('toggle_'):
        setting = query.data.replace('toggle_', '')
        await toggle_setting(query, context, setting)
    elif query.data.startswith('confirm_delete_'):
        monitor_id = query.data.replace('confirm_delete_', '')
        await execute_delete(query, context, monitor_id)

# ===== FUNCIONES DE NOTIFICACIONES =====
async def show_notification_settings(query, context):
    """Muestra la configuración de notificaciones"""
    user_id = query.from_user.id
    config = get_user_config(user_id)
    
    # Crear botones para cada configuración
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if config['notifications_enabled'] else '❌'} Notificaciones Activadas",
            callback_data='toggle_notifications_enabled'
        )],
        [InlineKeyboardButton(
            f"{'✅' if config['notify_on_down'] else '❌'} Notificar cuando caiga",
            callback_data='toggle_notify_on_down'
        )],
        [InlineKeyboardButton(
            f"{'✅' if config['notify_on_up'] else '❌'} Notificar cuando suba",
            callback_data='toggle_notify_on_up'
        )],
        [InlineKeyboardButton(
            f"{'✅' if config['notify_on_pause'] else '❌'} Notificar cuando pause",
            callback_data='toggle_notify_on_pause'
        )],
        [InlineKeyboardButton("⏱ Configurar Intervalo", callback_data='show_interval_menu')],
        [InlineKeyboardButton("🔙 Volver", callback_data='menu')]
    ]
    
    message = (
        "🔔 *Configuración de Notificaciones*\n\n"
        f"Estado actual:\n"
        f"• Notificaciones: {'Activadas' if config['notifications_enabled'] else 'Desactivadas'}\n"
        f"• Notificar caídas: {'Sí' if config['notify_on_down'] else 'No'}\n"
        f"• Notificar recuperación: {'Sí' if config['notify_on_up'] else 'No'}\n"
        f"• Notificar pausas: {'Sí' if config['notify_on_pause'] else 'No'}\n"
        f"• Intervalo de chequeo: {format_interval(config['check_interval'])}\n\n"
        "Selecciona qué opción deseas cambiar:"
    )
    
    await query.edit_message_text(
        message,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def toggle_setting(query, context, setting):
    """Activa/desactiva una configuración"""
    user_id = query.from_user.id
    config = get_user_config(user_id)
    
    # Mapear el setting al campo correspondiente
    setting_map = {
        'notifications_enabled': 'notifications_enabled',
        'notify_on_down': 'notify_on_down',
        'notify_on_up': 'notify_on_up',
        'notify_on_pause': 'notify_on_pause'
    }
    
    if setting in setting_map:
        config[setting_map[setting]] = not config[setting_map[setting]]
        save_user_config(user_id, config)
    
    # Volver a mostrar configuración actualizada
    await show_notification_settings(query, context)

async def show_interval_menu(query, context):
    """Muestra el menú para seleccionar intervalo"""
    keyboard = [
        [InlineKeyboardButton("1 minuto", callback_data='set_interval_60')],
        [InlineKeyboardButton("5 minutos", callback_data='set_interval_300')],
        [InlineKeyboardButton("10 minutos", callback_data='set_interval_600')],
        [InlineKeyboardButton("15 minutos", callback_data='set_interval_900')],
        [InlineKeyboardButton("30 minutos", callback_data='set_interval_1800')],
        [InlineKeyboardButton("1 hora", callback_data='set_interval_3600')],
        [InlineKeyboardButton("🔙 Volver", callback_data='notification_settings')]
    ]
    
    await query.edit_message_text(
        "⏱ *Selecciona el intervalo de chequeo*\n\n"
        "Cada cuánto tiempo quieres que el bot verifique el estado de tus webs.\n\n"
        "*(Intervalos más cortos = notificaciones más rápidas)*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_interval(query, context, interval):
    """Configura el intervalo de chequeo"""
    user_id = query.from_user.id
    config = get_user_config(user_id)
    config['check_interval'] = interval
    save_user_config(user_id, config)
    
    await query.answer(f"✅ Intervalo configurado a {format_interval(interval)}")
    await show_notification_settings(query, context)

# ===== FUNCIONES DE MONITORES =====
async def show_status(query, context):
    """Muestra el estado de todas las webs"""
    try:
        await query.edit_message_text("🔄 Consultando estado de tus webs...")
        
        response = requests.post(
            f'{UPTIME_API_URL}/getMonitors',
            data={
                'api_key': UPTIME_API_KEY,
                'format': 'json',
                'logs': 1,
                'response_times': 1,
                'custom_uptime_ratios': 1
            },
            timeout=30
        )
        
        data = response.json()
        
        if data.get('stat') == 'ok':
            monitors = data.get('monitors', [])
            
            if not monitors:
                await query.message.reply_text(
                    "📭 No tienes webs configuradas.\n\n"
                    "Usa '➕ Añadir Web' para comenzar.",
                    reply_markup=create_main_menu()
                )
                return
            
            # Estadísticas
            total = len(monitors)
            online = sum(1 for m in monitors if m['status'] == 2)
            offline = sum(1 for m in monitors if m['status'] == 9)
            paused = sum(1 for m in monitors if m['status'] == 0)
            
            # Calcular uptime promedio
            uptimes = [float(m.get('custom_uptime_ratio', 0)) for m in monitors if m.get('custom_uptime_ratio')]
            avg_uptime = sum(uptimes) / len(uptimes) if uptimes else 0
            
            summary = (
                f"📊 *RESUMEN GENERAL*\n"
                f"┌─────────────────────\n"
                f"│ 📌 Total: {total}\n"
                f"│ ✅ Online: {online}\n"
                f"│ ❌ Offline: {offline}\n"
                f"│ ⏸️ Pausados: {paused}\n"
                f"│ 📈 Uptime Prom: {avg_uptime:.2f}%\n"
                f"└─────────────────────\n\n"
                f"📋 *DETALLE POR WEB:*\n"
            )
            
            await query.message.reply_text(summary, parse_mode='Markdown')
            
            # Mostrar cada monitor
            for monitor in monitors:
                emoji, estado, color = get_status_emoji(monitor['status'])
                nombre = monitor.get('friendly_name', 'Sin nombre')
                url = monitor.get('url', 'URL no disponible')
                monitor_id = monitor.get('id', 'N/A')
                
                uptime = monitor.get('custom_uptime_ratio', 'N/A')
                uptime_str = f"{uptime}%" if uptime != 'N/A' else 'N/A'
                
                last_response = "N/A"
                if monitor.get('response_times'):
                    last_response = f"{monitor['response_times'][0].get('value', 0)}ms"
                
                last_down = "Sin caídas"
                if monitor.get('logs'):
                    for log in reversed(monitor['logs']):
                        if log.get('type') == 2:
                            fecha = datetime.fromtimestamp(log.get('datetime', 0))
                            last_down = fecha.strftime('%d/%m/%Y %H:%M')
                            break
                
                monitor_info = (
                    f"{emoji} *{nombre}*\n"
                    f"├─ ID: `{monitor_id}`\n"
                    f"├─ URL: {url}\n"
                    f"├─ Estado: {estado}\n"
                    f"├─ Uptime: {uptime_str}\n"
                    f"├─ Respuesta: ⚡{last_response}\n"
                    f"└─ Última caída: 📅 {last_down}\n"
                )
                
                await query.message.reply_text(monitor_info, parse_mode='Markdown')
            
            await query.message.reply_text(
                "✅ *Consulta completada*\nSelecciona otra opción:",
                parse_mode='Markdown',
                reply_markup=create_main_menu()
            )
        else:
            await query.message.reply_text(
                f"❌ Error: {data.get('error', {}).get('message', 'Error desconocido')}",
                reply_markup=create_main_menu()
            )
            
    except Exception as e:
        logger.error(f"Error en show_status: {e}")
        await query.message.reply_text(
            "❌ Error al consultar el estado. Intenta de nuevo.",
            reply_markup=create_main_menu()
        )

async def show_delete_menu(query, context):
    """Muestra menú para eliminar webs (VERSIÓN CORREGIDA)"""
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
                emoji, estado, _ = get_status_emoji(monitor['status'])
                nombre = monitor.get('friendly_name', 'Sin nombre')[:25]
                callback_data = f"delete_{monitor['id']}"
                keyboard.append([InlineKeyboardButton(
                    f"{emoji} {nombre} (ID: {monitor['id']})", 
                    callback_data=callback_data
                )])
            
            keyboard.append([InlineKeyboardButton("🔙 Cancelar", callback_data='menu')])
            
            await query.edit_message_text(
                "🗑 *Selecciona la web a eliminar:*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text(
                "📭 No hay webs para eliminar.",
                reply_markup=create_back_button()
            )
            
    except Exception as e:
        logger.error(f"Error en show_delete_menu: {e}")
        await query.edit_message_text(
            "❌ Error al cargar las webs.",
            reply_markup=create_back_button()
        )

async def confirm_delete(query, context, monitor_id):
    """Confirma eliminación de web"""
    keyboard = [
        [InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"confirm_delete_{monitor_id}")],
        [InlineKeyboardButton("❌ No, cancelar", callback_data='delete_web')]
    ]
    await query.edit_message_text(
        f"⚠️ *¿Estás seguro de eliminar el monitor {monitor_id}?*\n\nEsta acción no se puede deshacer.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def execute_delete(query, context, monitor_id):
    """Ejecuta la eliminación del monitor (VERSIÓN CORREGIDA)"""
    try:
        await query.edit_message_text(f"🔄 Eliminando monitor {monitor_id}...")
        
        response = requests.post(
            f'{UPTIME_API_URL}/deleteMonitor',
            data={
                'api_key': UPTIME_API_KEY,
                'format': 'json',
                'id': monitor_id
            },
            timeout=30
        )
        
        data = response.json()
        
        if data.get('stat') == 'ok':
            await query.message.reply_text(
                f"✅ *Monitor {monitor_id} eliminado correctamente!*",
                parse_mode='Markdown',
                reply_markup=create_main_menu()
            )
        else:
            error_msg = data.get('error', {}).get('message', 'Error desconocido')
            await query.message.reply_text(
                f"❌ Error al eliminar: {error_msg}",
                reply_markup=create_main_menu()
            )
            
    except Exception as e:
        logger.error(f"Error en execute_delete: {e}")
        await query.message.reply_text(
            "❌ Error de conexión al eliminar. Intenta de nuevo.",
            reply_markup=create_main_menu()
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto (para añadir URLs)"""
    if context.user_data.get('waiting_for_url'):
        url_input = update.message.text.strip()
        
        # Separar URL y nombre si se proporciona
        parts = url_input.split(' ', 1)
        url = parts[0]
        friendly_name = parts[1] if len(parts) > 1 else None
        
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text(
                "❌ URL inválida. Debe comenzar con http:// o https://\n\nIntenta de nuevo:",
                reply_markup=create_back_button()
            )
            return
        
        try:
            await update.message.reply_text(f"🔄 Añadiendo {url}...")
            
            nombre = friendly_name if friendly_name else url.replace('https://', '').replace('http://', '').split('/')[0]
            
            response = requests.post(
                f'{UPTIME_API_URL}/newMonitor',
                data={
                    'api_key': UPTIME_API_KEY,
                    'format': 'json',
                    'type': '1',
                    'url': url,
                    'friendly_name': nombre,
                    'interval': '300'
                },
                timeout=30
            )
            
            data = response.json()
            
            if data.get('stat') == 'ok':
                monitor = data.get('monitor', {})
                await update.message.reply_text(
                    f"✅ *¡Web añadida exitosamente!*\n\n"
                    f"📌 ID: `{monitor.get('id')}`\n"
                    f"🌐 URL: {url}\n"
                    f"📝 Nombre: {nombre}\n"
                    f"⏱ Intervalo: 5 minutos\n\n"
                    f"Recibirás notificaciones cuando el estado cambie.",
                    parse_mode='Markdown',
                    reply_markup=create_main_menu()
                )
            else:
                await update.message.reply_text(
                    f"❌ Error: {data.get('error', {}).get('message', 'Error desconocido')}",
                    reply_markup=create_main_menu()
                )
                
        except Exception as e:
            logger.error(f"Error al añadir web: {e}")
            await update.message.reply_text(
                "❌ Error al añadir la web. Intenta de nuevo.",
                reply_markup=create_main_menu()
            )
        
        context.user_data['waiting_for_url'] = False

async def show_settings(query, context):
    """Muestra configuración general"""
    keyboard = [
        [InlineKeyboardButton("🔔 Configurar Notificaciones", callback_data='notification_settings')],
        [InlineKeyboardButton("📊 Ver Estadísticas", callback_data='stats')],
        [InlineKeyboardButton("🔙 Volver", callback_data='menu')]
    ]
    
    await query.edit_message_text(
        "⚙️ *Configuración General*\n\n"
        "Aquí puedes ajustar todos los parámetros del bot.\n"
        "Selecciona una opción:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_help(query, context):
    """Muestra ayuda"""
    help_text = (
        "❓ *Ayuda del Bot*\n\n"
        "*📌 Comandos disponibles:*\n"
        "/start - Mostrar menú principal\n\n"
        "*🔔 Notificaciones:*\n"
        "• Puedes configurar qué eventos notificar\n"
        "• Elige el intervalo de chequeo (1 min a 1 hora)\n"
        "• Recibirás mensajes automáticos cuando cambie el estado\n\n"
        "*🌐 Añadir web:*\n"
        "• Usa URL completa (https://ejemplo.com)\n"
        "• Opcional: añade un nombre personalizado\n\n"
        "*🗑 Eliminar web:*\n"
        "• Selecciona de la lista\n"
        "• Confirmación antes de eliminar\n\n"
        "*📊 Estados:*\n"
        "✅ Online - Funcionando\n"
        "❌ Offline - Caída\n"
        "⏸️ Pausado - Monitoreo detenido\n"
        "🔄 Iniciando - Recién creado\n"
        "⚠️ Revisando - Posible problema"
    )
    await query.edit_message_text(help_text, parse_mode='Markdown', reply_markup=create_back_button())

async def post_init(application: Application):
    """Función que se ejecuta después de inicializar la aplicación"""
    logger.info("🤖 Bot iniciado correctamente!")
    logger.info(f"📱 Busca tu bot en Telegram")
    logger.info(f"🌐 Webhook configurado en: https://{RENDER_URL}/{TELEGRAM_TOKEN}")
    logger.info(f"⏱ Sistema de notificaciones activado (intervalo configurable por usuario)")

def main():
    """Función principal"""
    try:
        # Inicializar base de datos
        init_db()
        logger.info("✅ Base de datos inicializada")
        
        # Iniciar el hilo de auto-ping
        threading.Thread(target=keep_alive, daemon=True).start()
        logger.info("🔄 Sistema de auto-ping iniciado (cada 8 minutos)")
        
        # Iniciar el hilo de notificaciones
        threading.Thread(target=notification_worker, daemon=True).start()
        logger.info("🔔 Sistema de notificaciones iniciado")
        
        # Crear la aplicación
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .concurrent_updates(True)
            .build()
        )
        
        # Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Configurar webhook para Render
        webhook_url = f"https://{RENDER_URL}/{TELEGRAM_TOKEN}"
        logger.info(f"🔗 Configurando webhook en: {webhook_url}")
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=webhook_url,
            secret_token=None,
            allowed_updates=['message', 'callback_query']
        )
        
    except Exception as e:
        logger.error(f"❌ Error al iniciar: {e}")

if __name__ == '__main__':
    main()
