#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import requests
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

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Verificar configuración
if not TELEGRAM_TOKEN or not UPTIME_API_KEY:
    logger.error("Faltan variables de entorno")
    exit(1)

# ===== FUNCIONES =====
def get_status_emoji(status):
    """Convierte el código de estado a emoji"""
    status_map = {
        0: ('⏳', 'Pausado'),
        1: ('🔄', 'Iniciando'),
        2: ('✅', 'Online'),
        8: ('⚠️', 'Parece offline'),
        9: ('❌', 'Offline')
    }
    return status_map.get(status, ('❓', 'Desconocido'))

def main_menu():
    """Crea el menú principal"""
    keyboard = [
        [InlineKeyboardButton("📊 Ver Estado", callback_data='status')],
        [InlineKeyboardButton("➕ Añadir Web", callback_data='add')],
        [InlineKeyboardButton("❌ Eliminar Web", callback_data='delete')],
        [InlineKeyboardButton("❓ Ayuda", callback_data='help')]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_button():
    """Botón de volver"""
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data='menu')]]
    return InlineKeyboardMarkup(keyboard)

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    user = update.effective_user
    await update.message.reply_text(
        f"🤖 Hola {user.first_name}!\n\n"
        "Bot de monitoreo Uptime Robot\n"
        "Selecciona una opción:",
        reply_markup=main_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'menu':
        await query.edit_message_text("Menú principal:", reply_markup=main_menu())
    
    elif query.data == 'status':
        await query.edit_message_text("🔄 Consultando...")
        try:
            response = requests.post(
                f'{UPTIME_API_URL}/getMonitors',
                data={'api_key': UPTIME_API_KEY, 'format': 'json'},
                timeout=10
            )
            data = response.json()
            
            if data.get('stat') == 'ok':
                monitors = data.get('monitors', [])
                if not monitors:
                    await query.message.reply_text("📭 No hay webs", reply_markup=main_menu())
                    return
                
                msg = "📊 *ESTADO*\n\n"
                for m in monitors:
                    emoji, estado = get_status_emoji(m['status'])
                    msg += f"{emoji} *{m.get('friendly_name')}*\n"
                    msg += f"ID: `{m.get('id')}`\n"
                    msg += f"Estado: {estado}\n\n"
                
                await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu())
            else:
                await query.message.reply_text("❌ Error API", reply_markup=main_menu())
        except Exception as e:
            logger.error(f"Error: {e}")
            await query.message.reply_text("❌ Error de conexión", reply_markup=main_menu())
    
    elif query.data == 'add':
        await query.edit_message_text(
            "🌐 Envía la URL (ej: https://ejemplo.com):",
            reply_markup=back_button()
        )
        context.user_data['esperando_url'] = True
    
    elif query.data == 'delete':
        try:
            response = requests.post(
                f'{UPTIME_API_URL}/getMonitors',
                data={'api_key': UPTIME_API_KEY, 'format': 'json'},
                timeout=10
            )
            data = response.json()
            
            if data.get('stat') == 'ok' and data.get('monitors'):
                keyboard = []
                for m in data['monitors']:
                    keyboard.append([InlineKeyboardButton(
                        f"{m.get('friendly_name')} (ID: {m['id']})",
                        callback_data=f"del_{m['id']}"
                    )])
                keyboard.append([InlineKeyboardButton("🔙 Cancelar", callback_data='menu')])
                await query.edit_message_text("🗑 Selecciona:", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.edit_message_text("📭 No hay webs", reply_markup=main_menu())
        except Exception as e:
            logger.error(f"Error: {e}")
            await query.edit_message_text("❌ Error", reply_markup=main_menu())
    
    elif query.data.startswith('del_'):
        monitor_id = query.data.replace('del_', '')
        keyboard = [
            [InlineKeyboardButton("✅ Sí", callback_data=f"confirm_{monitor_id}")],
            [InlineKeyboardButton("❌ No", callback_data='delete')]
        ]
        await query.edit_message_text(
            f"⚠️ ¿Eliminar monitor {monitor_id}?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif query.data.startswith('confirm_'):
        monitor_id = query.data.replace('confirm_', '')
        await query.edit_message_text("🔄 Eliminando...")
        try:
            response = requests.post(
                f'{UPTIME_API_URL}/deleteMonitor',
                data={'api_key': UPTIME_API_KEY, 'format': 'json', 'id': monitor_id},
                timeout=10
            )
            if response.json().get('stat') == 'ok':
                await query.message.reply_text("✅ Eliminado", reply_markup=main_menu())
            else:
                await query.message.reply_text("❌ Error", reply_markup=main_menu())
        except Exception as e:
            logger.error(f"Error: {e}")
            await query.message.reply_text("❌ Error", reply_markup=main_menu())
    
    elif query.data == 'help':
        await query.edit_message_text(
            "❓ *AYUDA*\n\n"
            "📊 Ver Estado: Muestra todas tus webs\n"
            "➕ Añadir Web: Agrega nueva URL\n"
            "❌ Eliminar Web: Quita una web\n\n"
            "*Estados:*\n"
            "✅ Online\n❌ Offline\n⏳ Pausado\n⚠️ Posible caída",
            parse_mode='Markdown',
            reply_markup=back_button()
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja mensajes (para añadir URLs)"""
    if context.user_data.get('esperando_url'):
        url = update.message.text.strip()
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text("❌ URL inválida. Usa http:// o https://", reply_markup=back_button())
            return
        
        await update.message.reply_text(f"🔄 Añadiendo...")
        try:
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
                timeout=10
            )
            data = response.json()
            if data.get('stat') == 'ok':
                await update.message.reply_text(
                    f"✅ Añadida!\nID: `{data['monitor']['id']}`",
                    parse_mode='Markdown',
                    reply_markup=main_menu()
                )
            else:
                await update.message.reply_text(f"❌ Error: {data.get('error', {})}", reply_markup=main_menu())
        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text("❌ Error de conexión", reply_markup=main_menu())
        
        context.user_data['esperando_url'] = False

def main():
    """Función principal"""
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("🤖 Bot iniciando en Render...")
        
        # Usar webhook para producción
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"
        )
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
