"""
Bot de dívidas e serviços para Telegram.
Código configurado para rodar 24h na nuvem (Render)
"""

import os
import re
import logging
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# TOKEN DEFINIDO DIRETAMENTE NO CÓDIGO
TOKEN = "8404473454:AAGgrYpoUTf7NTllmgIYQe-P9dDa3Mz4_7Y"
DB_PATH = "dividas.db"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)

# --- SERVIDOR WEB AUXILIAR PARA O RENDER NÃO DERRUBAR O BOT ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot esta vivo!")

    def log_message(self, format, *args):
        return  # Desativa logs repetitivos de ping no terminal

def rodar_servidor_web():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- BANCO DE DADOS ---
STOP_WORDS = {"me", "deve", "devendo", "dever", "devo", "de", "do", "da", "reais", "real", "r\$"}
PAYMENT_WORDS = {"pago", "paga", "pagou", "pagaram", "quitou", "recebi", "acertou", "liquidou"}

def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.casefold())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip()

@contextmanager
def conectar():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dividas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                nome TEXT,
                valor_cents INTEGER,
                motivo TEXT
            )
        """)

def dinheiro(cents: int) -> str:
    return f"R\$ {cents // 100},{cents % 100:02d}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Olá! Sou seu assistente de dívidas e serviços. 🧾\n\n"
        "Exemplos:\n"
        "• `Baiano deve 30`\n"
        "• `Jessica deve 50 da troca de tela`\n"
        "• `Baiano pagou` — quita as dívidas dele\n"
        "• `2 pago` — quita o item 2 da última lista\n"
        "• `Todos pagaram` — limpa a lista inteira\n\n"
        "Comandos: /lista e /ajuda"
    )

async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def listar_dividas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with conectar() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, valor_cents, motivo FROM dividas WHERE usuario_id=?", (user_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.effective_message.reply_text("Ninguém te deve nada no momento! Nada consta. 🎉")
        return

    resposta = "📋 **Pessoas que te devem:**\n\n"
    total = 0
    context.user_data['lista_cache'] = []

    for i, row in enumerate(rows, start=1):
        context.user_data['lista_cache'].append({"id": row[0], "nome": row[1], "valor": row[2]})
        motivo = f" ({row[3]})" if row[3] and row[3] != "Serviço" else ""
        resposta += f"{i}. **{row[1].capitalize()}**: {dinheiro(row[2])}{motivo}\n"
        total += row[2]

    resposta += f"\n💰 **Total a receber:** {dinheiro(total)}"
    await update.effective_message.reply_text(resposta, parse_mode="Markdown")

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_original = update.effective_message.text
    texto = texto_original.lower().strip()
    user_id = update.effective_user.id

    # 1. VERIFICAR CONSULTA DA LISTA
    if "quem" in texto or ("todos" in texto and "deve" in texto) or texto in ["lista", "/lista"]:
        await listar_dividas(update, context)
        return

    # 2. VERIFICAR SE TODOS PAGARAM MESTRE
    if ("todos" in texto and any(p in texto for p in PAYMENT_WORDS)) or "eles pagaram" in texto:
        with conectar() as conn:
            conn.execute("DELETE FROM dividas WHERE usuario_id=?", (user_id,))
        context.user_data['lista_cache'] = []
        await update.effective_message.reply_text("✅ Excelente! Todas as dívidas foram zeradas. Nada consta! 🎉")
        return

    # 3. BAIXA POR NÚMERO DA LISTA (EX: "1 pago" ou "pago 2")
    padrao_num = r"(\d+)\s+(?:" + "|".join(PAYMENT_WORDS) + ")|(?:" + "|".join(PAYMENT_WORDS) + r")\s+(\d+)"
    match_num = re.search(padrao_num, texto)
    if match_num:
        num_str = match_num.group(1) or match_num.group(2)
        idx = int(num_str)
        cache = context.user_data.get('lista_cache', [])
        
        if not cache or idx < 1 or idx > len(cache):
            await update.effective_message.reply_text("⚠️ Esse número não bate com a última lista. Digite 'lista' para ver de novo.")
            return

        item = cache[idx - 1]
        with conectar() as conn:
            conn.execute("DELETE FROM dividas WHERE id=?", (item["id"],))
        
        await update.effective_message.reply_text(f"✅ Baixa realizada! **{item['nome'].capitalize()}** quitou {dinheiro(item['valor'])}.")
        return

    # 4. VERIFICAR BAIXA POR NOME APROXIMADO (EX: "baiano pago" ou "bhaia pagou")
    if any(p in texto for p in PAYMENT_WORDS):
        palavras = [p for p in texto.split() if p not in PAYMENT_WORDS and p not in STOP_WORDS]
        if palavras:
            nome_digitado = palavras[0]
            with conectar() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT nome FROM dividas WHERE usuario_id=?", (user_id,))
                todos_nomes = [r[0] for r in cursor.fetchall()]
            
            nome_certo = None
            for n in todos_nomes:
                if SequenceMatcher(None, normalizar(nome_digitado), normalizar(n)).ratio() >= 0.72:
                    nome_certo = n
                    break
            
            if nome_certo:
                with conectar() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, valor_cents FROM dividas WHERE usuario_id=? AND nome=? ORDER BY id DESC LIMIT 1",
                        (user_id, nome_certo)
                    )
                    ultima_divida = cursor.fetchone()
                
                if ultima_divida:
                    with conectar() as conn:
                        conn.execute("DELETE FROM dividas WHERE id=?", (ultima_divida[0],))
                    await update.effective_message.reply_text(f"✅ Baixa realizada! Registro encontrado para **{nome_certo.capitalize()}** (R\$ {dinheiro(ultima_divida[1])}) quitado.")
                    return

    # 5. NOVO REGISTRO DE DÍVIDA (EX: "baiano conta google 30")
    valores_encontrados = re.findall(r'\b\d+(?:[.,]\d{1,2})?\b', texto)
    if valores_encontrados:
        valor_txt = valores_encontrados[-1]
        
        # Converte valor para centavos com segurança
        s = valor_txt.replace(",", ".")
        try:
            cents = int(float(s) * 100)
        except ValueError:
            cents = None
        
        if cents and cents > 0:
            partes = texto_original.split()
            nome = None
            motivo_lista = []
            
            for p in partes:
                p_norm = normalizar(p)
                if p_norm == normalizar(valor_txt) or p_norm in STOP_WORDS or p_norm in PAYMENT_WORDS:
                    continue
                if not nome and p.isalpha():
                    nome = p_norm
                else:
                    motivo_lista.append(p)
            
            if nome:
                motivo_texto = " ".join(motivo_lista) if motivo_lista else "Serviço"
                
                with conectar() as conn:
                    conn.execute(
                        "INSERT INTO dividas (usuario_id, nome, valor_cents, motivo) VALUES (?, ?, ?, ?)",
                        (user_id, nome, cents, motivo_texto)
                    )
                await update.effective_message.reply_text(f"✅ Anotado! **{nome.capitalize()}** te deve {dinheiro(cents)} por: *{motivo_texto}*.")
                return

    await update.effective_message.reply_text("Não entendi bem. Formatos válidos:\n• `Baiano tela 120`\n• `1 pago` ou `Baiano pago`\n• `Lista`")

def main():
    init_db()
    # Inicia o servidor auxiliar do Render em segundo plano
    threading.Thread(target=rodar_servidor_web, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("lista", listar_dividas))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))

    print("Bot rodando perfeitamente...")
    app.run_polling()

if __name__ == "__main__":
    main()
