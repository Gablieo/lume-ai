import sqlite3

DB_NAME = "historico_chats.db"

def get_connection():
    """Retorna uma conexão isolada e segura para uso em múltiplas threads com timeout estendido."""
    return sqlite3.connect(DB_NAME, check_same_thread=False, timeout=10.0)

def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS conversas (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS mensagens (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id INTEGER, autor TEXT, texto TEXT, tempo TEXT, FOREIGN KEY(conversa_id) REFERENCES conversas(id))''')
        try:
            c.execute("ALTER TABLE mensagens ADD COLUMN tempo TEXT;")
        except sqlite3.OperationalError:
            pass
        conn.commit()

def criar_nova_conversa(titulo="Nova Conversa"):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO conversas (titulo) VALUES (?)", (titulo,))
        chat_id = c.lastrowid
        conn.commit()
        return chat_id

def carregar_conversas():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, titulo FROM conversas ORDER BY id DESC")
        return c.fetchall()

def carregar_mensagens(conversa_id):
    with get_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("SELECT autor, texto, tempo FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (conversa_id,))
            return c.fetchall()
        except sqlite3.OperationalError:
            c.execute("SELECT autor, texto, NULL FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (conversa_id,))
            return c.fetchall()

def salvar_mensagem(conversa_id, autor, texto, tempo=None):
    with get_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO mensagens (conversa_id, autor, texto, tempo) VALUES (?, ?, ?, ?)", (conversa_id, autor, texto, tempo))
        except sqlite3.OperationalError:
            c.execute("INSERT INTO mensagens (conversa_id, autor, texto) VALUES (?, ?, ?)", (conversa_id, autor, texto))
        conn.commit()

def atualizar_titulo_conversa(conversa_id, novo_titulo):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (novo_titulo, conversa_id))
        conn.commit()

def deletar_conversa(conversa_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM mensagens WHERE conversa_id = ?", (conversa_id,))
        c.execute("DELETE FROM conversas WHERE id = ?", (conversa_id,))
        conn.commit()

init_db()