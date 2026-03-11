#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import requests
import threading
import time
from datetime import datetime
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
        0: ('⏳', 'Pausado', '⚪'),
        1: ('🔄', 'Iniciando', '🟡'),
        2: ('✅', 'Online', '🟢'),
        8: ('⚠️', 'Parece offline', '🟠'),
        9: ('❌', 'Offline', '🔴')
    }
    return status_map.get(status, ('❓', 'Desconocido', '⚫'))

def create_main_menu():
    """Crea el teclado del menú principal"""
    keyboard = [
        [InlineKeyboardButton("📊 Ver Estado", callback_data='status')],
        [InlineKeyboardButton("➕ Añadir Web", callback_data='add_web'),
         InlineKeyboardButton("❌ Eliminar Web", callback_data='delete_web')],
        [InlineKeyboardButton("⚙️ Configurar Alertas", callback_data='settings'),
         InlineKeyboardButton("❓ Ayuda", callback_data='help')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_button():
    """Crea botón de volver"""
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data='menu')]]
    return InlineKeyboardMarkup(keyboard)

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Muestra menú principal"""
    user = update.effective_user
    welcome_msg = (
        f"🤖 *Bienvenido {user.first_name}!*\n\n"
        "Soy tu asistente de monitoreo de Uptime Robot\n\n"
        "🔍 *¿Qué puedo hacer por ti?*\n"
        "• Ver estado de tus webs en tiempo real\n"
        "• Añadir nuevas webs para monitorear\n"
        "• Eliminar monitores existentes\n"
        "• Configurar alertas personalizadas\n\n"
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
            "Ejemplo: `https://ejemplo.com`",
            parse_mode='Markdown',
            reply_markup=create_back_button()
        )
        context.user_data['waiting_for_url'] = True
    elif query.data == 'delete_web':
        await show_delete_menu(query, context)
    elif query.data == 'settings':
        await show_settings(query, context)
    elif query.data == 'help':
        await show_help(query, context)
    elif query.data.startswith('delete_'):
        monitor_id = query.data.replace('delete_', '')
        await confirm_delete(query, context, monitor_id)

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
                'response_times': 1
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
            
            # Mensaje de resumen
            summary = (
                f"📊 *RESUMEN GENERAL*\n"
                f"┌─────────────────────\n"
                f"│ 📌 Total: {total}\n"
                f"│ 🟢 Online: {online}\n"
                f"│ 🔴 Offline: {offline}\n"
                f"│ ⚪ Pausados: {paused}\n"
                f"└─────────────────────\n\n"
                f"📋 *DETALLE POR WEB:*\n"
            )
            
            await query.message.reply_text(summary, parse_mode='Markdown')
            
            # Mostrar cada monitor individualmente
            for monitor in monitors:
                emoji, estado, color = get_status_emoji(monitor['status'])
                nombre = monitor.get('friendly_name', 'Sin nombre')
                url = monitor.get('url', 'URL no disponible')
                monitor_id = monitor.get('id', 'N/A')
                
                # Obtener último tiempo de respuesta
                last_response = "N/A"
                if monitor.get('response_times'):
                    last_response = f"{monitor['response_times'][0].get('value', 0)}ms"
                
                # Obtener última caída
                last_down = "Sin caídas"
                if monitor.get('logs'):
                    for log in monitor['logs']:
                        if log.get('type') == 2:  # offline
                            fecha = datetime.fromtimestamp(log.get('datetime', 0))
                            last_down = fecha.strftime('%d/%m/%Y %H:%M')
                            break
                
                monitor_info = (
                    f"{emoji} *{nombre}*\n"
                    f"├─ ID: `{monitor_id}`\n"
                    f"├─ URL: {url}\n"
                    f"├─ Estado: {estado} {color}\n"
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
    """Muestra menú para eliminar webs"""
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
                emoji, _, _ = get_status_emoji(monitor['status'])
                nombre = monitor.get('friendly_name', 'Sin nombre')[:20]
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes de texto (para añadir URLs)"""
    if context.user_data.get('waiting_for_url'):
        url = update.message.text.strip()
        
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text(
                "❌ URL inválida. Debe comenzar con http:// o https://\n\nIntenta de nuevo:",
                reply_markup=create_back_button()
            )
            return
        
        try:
            await update.message.reply_text(f"🔄 Añadiendo {url}...")
            
            nombre = url.replace('https://', '').replace('http://', '').split('/')[0]
            
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
                    f"⏱ Intervalo: 5 minutos\n\n"
                    f"Usa /start para ver el menú principal.",
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
    """Muestra configuración de alertas"""
    keyboard = [
        [InlineKeyboardButton("⏰ Intervalo de alertas", callback_data='set_interval')],
        [InlineKeyboardButton("🔔 Notificaciones", callback_data='notifications')],
        [InlineKeyboardButton("🔙 Volver", callback_data='menu')]
    ]
    await query.edit_message_text(
        "⚙️ *Configuración*\n\n"
        "Personaliza cómo quieres recibir las alertas:\n\n"
        "• Intervalo actual: 5 minutos\n"
        "• Notificaciones: Activadas\n"
        "• Alertas SSL: Activadas\n\n"
        "*Próximamente:* más opciones de personalización.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_help(query, context):
    """Muestra ayuda"""
    help_text = (
        "❓ *Ayuda del Bot*\n\n"
        "*📌 Comandos disponibles:*\n"
        "/start - Mostrar menú principal\n\n"
        "*🔍 Funciones:*\n"
        "• **Ver Estado**: Muestra todas tus webs con su estado actual\n"
        "• **Añadir Web**: Agrega una nueva URL para monitorear\n"
        "• **Eliminar Web**: Quita una web de la lista\n"
        "• **Configurar**: Ajusta las alertas (próximamente)\n\n"
        "*📊 Estados de las webs:*\n"
        "🟢 Online - Funcionando correctamente\n"
        "🔴 Offline - Caída confirmada\n"
        "⚪ Pausado - Monitoreo detenido\n"
        "🟠 Posible caída - Revisando\n\n"
        "*💡 Consejos:*\n"
        "• Usa URLs completas (https://ejemplo.com)\n"
        "• El intervalo de chequeo es de 5 minutos\n"
        "• Recibirás alertas automáticas cuando una web caiga"
    )
    await query.edit_message_text(help_text, parse_mode='Markdown', reply_markup=create_back_button())

async def post_init(application: Application):
    """Función que se ejecuta después de inicializar la aplicación"""
    logger.info("🤖 Bot iniciado correctamente!")
    logger.info(f"📱 Busca tu bot en Telegram")
    logger.info(f"🌐 Webhook configurado en: https://{RENDER_URL}/{TELEGRAM_TOKEN}")

def main():
    """Función principal"""
    try:
        # Iniciar el hilo de auto-ping en segundo plano
        threading.Thread(target=keep_alive, daemon=True).start()
        logger.info("🔄 Sistema de auto-ping iniciado (cada 8 minutos)")
        
        # Crear la aplicación con configuración optimizada para producción
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .concurrent_updates(True)
            .build()
        )
        
        # Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button_handler, pattern='^(?!confirm_delete_).*'))
        application.add_handler(CallbackQueryHandler(confirm_delete, pattern='^confirm_delete_'))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Configurar y ejecutar webhook para Render
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
