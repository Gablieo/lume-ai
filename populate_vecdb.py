import os
import sqlite3
import random
import math
import uuid
import chromadb
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------
# 1. CONFIGURAÇÕES
# ---------------------------------------------------------
# Caminho para o banco de dados vetorial
VECDB_DIR = ".vecdb"
# Caminho para a pasta que contém seus documentos de texto
DOCS_FOLDER = "dados_lume"
# Nome da coleção no banco de dados vetorial
COLLECTION_NAME = "conhecimento_lume"

# Usaremos um modelo de embedding local, pequeno e eficiente
# https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
# Ele será baixado automaticamente na primeira vez.
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# ---------------------------------------------------------
# 2. POPULAÇÃO DO BANCO DE DADOS
# ---------------------------------------------------------

def carregar_e_indexar_documentos():
    print(f"--- Iniciando a população do banco de dados vetorial ---")
    print(f"--- Usando o modelo de embedding: {EMBEDDING_MODEL_NAME} ---")

    # Carrega o modelo de embedding
    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    print("--- Modelo de embedding carregado. ---")

    # Inicializa o cliente do ChromaDB (localmente, persistente)
    chroma_client = chromadb.PersistentClient(path=VECDB_DIR)

    # Cria ou obtém a coleção
    collection = chroma_client.create_collection(name=COLLECTION_NAME, embedding_function=None, get_or_create=True)
    print(f"--- Coleção '{COLLECTION_NAME}' inicializada. ---")

    # Lê todos os arquivos .txt da pasta dados_lume
    print(f"--- Lendo documentos da pasta '{DOCS_FOLDER}' ---")
    
    if not os.path.exists(DOCS_FOLDER):
        os.makedirs(DOCS_FOLDER)
        print(f"--- Pasta '{DOCS_FOLDER}' criada. Coloque seus arquivos .txt nela. ---")
        return

    arquivos = [f for f in os.listdir(DOCS_FOLDER) if f.endswith(".txt")]
    
    if not arquivos:
        print(f"--- Nenhum arquivo .txt encontrado na pasta '{DOCS_FOLDER}'. ---")
        return

    documentos = []
    ids = []

    for arquivo in arquivos:
        print(f"--- Lendo arquivo: {arquivo} ---")
        caminho_completo = os.path.join(DOCS_FOLDER, arquivo)
        with open(caminho_completo, "r", encoding="utf-8") as f:
            conteudo = f.read()
            
            # Aqui você pode adicionar lógica para dividir textos longos em 'chunks'
            # (pedações menores) para melhor performance. Para este exemplo simples,
            # vamos carregar o arquivo inteiro como um documento.
            
            documentos.append(conteudo)
            # Cria um ID único para o documento
            ids.append(str(uuid.uuid4()))

    # Gera os embeddings e adiciona à coleção
    if documentos:
        print(f"--- Gerando embeddings e indexando {len(documentos)} documentos... ---")
        
        # O ChromaDB com embedding_function=None exige que geremos e passemos os embeddings
        embeddings = embedding_model.encode(documentos).tolist()
        
        collection.add(
            documents=documentos,
            embeddings=embeddings,
            ids=ids
        )
        
        print(f"--- Sucesso! Banco de dados vetorial populado. ---")
    else:
        print(f"--- Nenhum documento carregado. ---")

if __name__ == "__main__":
    carregar_e_indexar_documentos()