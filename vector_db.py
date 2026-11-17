try:
    __import__('pysqlite3')
    import sys
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    pass

import chromadb
import uuid

# Conecta ao banco de dados persistente local
client = chromadb.PersistentClient(path="./chroma_db")

# Coleções persistentes
collection = client.get_or_create_collection(name="lume_memoria_longo_prazo")
collection_resumos = client.get_or_create_collection(name="lume_resumos_chats")

# -------------------------------------------------------------
# MIGRAÇÃO AUTOMÁTICA DE REGISTROS LEGADOS
# -------------------------------------------------------------

def migrar_registros_legados():
    """Garante que dados criados sem metadados recebam a etiqueta de dono."""
    try:
        dados = collection.get(include=["metadatas", "documents"])
        ids = dados.get("ids", [])
        metas = dados.get("metadatas") or []
        docs = dados.get("documents", [])

        ids_up, metas_up, docs_up = [], [], []
        for idx, doc_id in enumerate(ids):
            meta = metas[idx] if idx < len(metas) and metas[idx] is not None else {}
            if "usuario" not in meta:
                nova_meta = dict(meta)
                nova_meta["usuario"] = "padrao"
                ids_up.append(doc_id)
                metas_up.append(nova_meta)
                docs_up.append(docs[idx])

        if ids_up:
            collection.update(ids=ids_up, documents=docs_up, metadatas=metas_up)
            print(f"🔄 ChromaDB: {len(ids_up)} fatos legados vinculados ao usuário 'padrao'.")
    except Exception as e:
        print(f"ℹ️ Verificação de migração de fatos: {e}")

    try:
        dados_r = collection_resumos.get(include=["metadatas", "documents"])
        ids_r = dados_r.get("ids", [])
        metas_r = dados_r.get("metadatas") or []
        docs_r = dados_r.get("documents", [])

        ids_rup, metas_rup, docs_rup = [], [], []
        for idx, doc_id in enumerate(ids_r):
            meta = metas_r[idx] if idx < len(metas_r) and metas_r[idx] is not None else {}
            if "usuario" not in meta:
                nova_meta = dict(meta)
                nova_meta["usuario"] = "padrao"
                ids_rup.append(doc_id)
                metas_rup.append(nova_meta)
                docs_rup.append(docs_r[idx])

        if ids_rup:
            collection_resumos.update(ids=ids_rup, documents=docs_rup, metadatas=metas_rup)
            print(f"🔄 ChromaDB: {len(ids_rup)} resumos legados vinculados ao usuário 'padrao'.")
    except Exception as e:
        print(f"ℹ️ Verificação de migração de resumos: {e}")

migrar_registros_legados()

# -------------------------------------------------------------
# MEMÓRIA DE LONGO PRAZO (FATOS GRANULARES)
# -------------------------------------------------------------

def salvar_memoria(texto: str, usuario: str = "padrao") -> str:
    """Salva um fato no ChromaDB com ID único e metadado de dono."""
    if not texto or not texto.strip():
        return "Texto vazio. Nenhum fato foi registrado."

    try:
        doc_id = str(uuid.uuid4())
        collection.add(
            documents=[texto.strip()],
            ids=[doc_id],
            metadatas=[{"usuario": usuario}]
        )
        return "Informação gravada na memória de longo prazo com sucesso!"
    except Exception as e:
        print(f"❌ Erro ao salvar fato: {e}")
        return f"Erro crítico ao salvar no ChromaDB: {e}"

def consultar_memoria(pergunta: str, usuario: str = "padrao") -> str:
    """Consulta os fatos relevantes sem vazar mensagens de erro ao modelo."""
    if not pergunta or not pergunta.strip():
        return ""

    try:
        total = collection.count()
        if total == 0:
            return ""

        n_res = min(8, total)
        try:
            resultados = collection.query(
                query_texts=[pergunta],
                n_results=n_res,
                where={"usuario": usuario}
            )
        except Exception:
            resultados = collection.query(
                query_texts=[pergunta],
                n_results=n_res
            )

        documentos = resultados.get("documents", [[]])[0]
        documentos_validos = [d for d in documentos if d and d.strip()]
        
        if not documentos_validos:
            return ""

        return "- " + "\n- ".join(documentos_validos)
    except Exception as e:
        print(f"⚠️ Erro ao consultar memória vetorial: {e}")
        return ""

# -------------------------------------------------------------
# MEMÓRIA HIERÁRQUICA (RESUMOS DE CHATS)
# -------------------------------------------------------------

def salvar_ou_atualizar_resumo_chat(chat_id: int, resumo_texto: str, usuario: str = "padrao") -> str:
    """Atualiza o resumo de um chat garantindo a propriedade do usuário."""
    if not resumo_texto or not resumo_texto.strip():
        return "Resumo vazio."

    try:
        doc_id = f"chat_summary_{chat_id}"
        collection_resumos.upsert(
            documents=[resumo_texto.strip()],
            ids=[doc_id],
            metadatas=[{"chat_id": str(chat_id), "usuario": usuario}]
        )
        return f"Resumo da conversa {chat_id} atualizado com sucesso."
    except Exception as e:
        print(f"❌ Erro ao atualizar resumo: {e}")
        return f"Erro ao atualizar resumo do chat: {e}"

def consultar_resumos_chats(pergunta: str, chat_id_atual: int = None, usuario: str = "padrao") -> str:
    """Busca tópicos de conversas passadas de forma isolada e segura."""
    if not pergunta or not pergunta.strip():
        return ""

    try:
        total = collection_resumos.count()
        if total == 0:
            return ""

        n_res = min(3, total)
        where_clause = {"usuario": usuario}
        if chat_id_atual is not None:
            where_clause = {
                "$and": [
                    {"usuario": usuario},
                    {"chat_id": {"$ne": str(chat_id_atual)}}
                ]
            }

        try:
            resultados = collection_resumos.query(
                query_texts=[pergunta],
                n_results=n_res,
                where=where_clause
            )
        except Exception:
            resultados = collection_resumos.query(
                query_texts=[pergunta],
                n_results=n_res
            )

        documentos = resultados.get("documents", [[]])[0]
        documentos_validos = [d for d in documentos if d and d.strip()]
        
        if not documentos_validos:
            return ""

        return "- " + "\n- ".join(documentos_validos)
    except Exception as e:
        print(f"⚠️ Erro ao consultar resumos de chats: {e}")
        return ""

def deletar_resumo_chat(chat_id: int) -> str:
    """Remove o resumo do ChromaDB quando uma conversa é excluída."""
    try:
        doc_id = f"chat_summary_{chat_id}"
        collection_resumos.delete(ids=[doc_id])
        return f"Resumo do chat {chat_id} removido."
    except Exception as e:
        return f"Aviso ao remover resumo do chat {chat_id}: {e}"

# -------------------------------------------------------------
# FUNÇÕES DE GERENCIAMENTO (CRUD)
# -------------------------------------------------------------

def listar_todas_memorias(usuario: str = "padrao"):
    """Lista as memórias com suporte a dados pré-existentes."""
    try:
        dados = collection.get(include=["documents", "metadatas"])
        ids = dados.get("ids") or []
        documentos = dados.get("documents") or []
        metadatas = dados.get("metadatas") or []

        ids_filtrados, docs_filtrados = [], []
        for idx, doc_id in enumerate(ids):
            meta = metadatas[idx] if idx < len(metadatas) and metadatas[idx] is not None else {}
            dono = meta.get("usuario", "padrao")
            if dono == usuario:
                ids_filtrados.append(doc_id)
                docs_filtrados.append(documentos[idx])

        return ids_filtrados, docs_filtrados
    except Exception as e:
        print(f"Erro ao listar memórias: {e}")
        return [], []

def deletar_memoria_por_id(id_memoria: str) -> str:
    """Remove um registro específico da coleção pelo ID."""
    try:
        collection.delete(ids=[id_memoria])
        return f"Memória {id_memoria} removida com sucesso."
    except Exception as e:
        return f"Erro ao deletar memória: {e}"

def atualizar_memoria_por_id(id_memoria: str, novo_texto: str, usuario: str = "padrao") -> str:
    """Sobrescreve o texto e mantém a propriedade do usuário."""
    if not novo_texto or not novo_texto.strip():
        return "Texto inválido."

    try:
        collection.update(
            ids=[id_memoria],
            documents=[novo_texto.strip()],
            metadatas=[{"usuario": usuario}]
        )
        return "Memória atualizada com sucesso."
    except Exception as e:
        return f"Erro ao atualizar memória: {e}"