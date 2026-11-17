import os
# Silencia avisos de symlink do Hugging Face no Windows
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import re
import io
import sys
import time
import gc
import json
import random
import base64
import requests
import traceback
import contextlib
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from openai import OpenAI

try:
    import streamlit as st
except ImportError:
    st = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    from crewai import Agent, Crew, Process, Task, LLM
except ImportError:
    pass

from database import carregar_mensagens
from vector_db import (
    consultar_memoria, 
    salvar_memoria, 
    consultar_resumos_chats, 
    salvar_ou_atualizar_resumo_chat
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
FREE_LLM_BASE_URL = "https://text.pollinations.ai/openai"

def obter_chave_gemini() -> str:
    """Busca a chave na sessão da barra lateral, variável de ambiente ou Secrets do Streamlit."""
    if st is not None:
        try:
            chave_sessao = st.session_state.get("GEMINI_API_KEY", "")
            if chave_sessao and chave_sessao.strip():
                return chave_sessao.strip()
        except Exception:
            pass

    chave_env = os.environ.get("GEMINI_API_KEY", "")
    if chave_env and chave_env.strip():
        return chave_env.strip()

    if st is not None:
        try:
            chave_secret = st.secrets.get("GEMINI_API_KEY", "")
            if chave_secret and chave_secret.strip():
                return chave_secret.strip()
        except Exception:
            pass

    return ""

class ClienteDinamicoOpenAI:
    """Conecta à API oficial do Gemini quando há chave ou usa o LLM gratuito automaticamente."""
    @property
    def chat(self):
        chave = obter_chave_gemini()
        if chave:
            cliente = OpenAI(
                base_url=GEMINI_BASE_URL,
                api_key=chave,
                timeout=90.0
            )
        else:
            cliente = OpenAI(
                base_url=FREE_LLM_BASE_URL,
                api_key="lume-free-llm",
                timeout=90.0
            )
        return cliente.chat

client_local = ClienteDinamicoOpenAI()

# =============================================================
# 1. 🛡️ FIREWALL DE SEGURANÇA E PRIVACIDADE E FERRAMENTAS
# =============================================================

def mascarar_dados_sensiveis(texto: str) -> str:
    texto = re.sub(r'\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b', '[EMAIL PROTEGIDO]', texto)
    texto = re.sub(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b', '[CPF PROTEGIDO]', texto)
    texto = re.sub(r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[CARTAO PROTEGIDO]', texto)
    texto = re.sub(r'\b(?:\+55\s?)?(?:\(?\d{2}\)?\s?)?9?\d{4}[-\s]?\d{4}\b', '[TELEFONE PROTEGIDO]', texto)
    return texto

def verificar_injecao_prompt(prompt: str) -> bool:
    padroes_maliciosos = [
        "ignore as instruções", "ignore todas as instruções", "esqueça as regras", 
        "desconsidere as regras", "system prompt", "modo desenvolvedor", 
        "dan mode", "você agora é", "you are now", "bypassar", 
        "quais são suas instruções", "revele seu prompt"
    ]
    return any(p in prompt.lower() for p in padroes_maliciosos)

def codificar_imagem_base64(imagem_bytes: bytes) -> str:
    """Converte bytes brutos da imagem em Base64 para envio multimodal."""
    return base64.b64encode(imagem_bytes).decode("utf-8")

def extrair_texto_pdf(pdf_bytes: bytes) -> str:
    """Extrai texto bruto de um arquivo PDF carregado em memória."""
    try:
        import fitz
        texto_completo = ""
        documento = fitz.open(stream=pdf_bytes, filetype="pdf")
        for pagina in documento:
            texto_completo += pagina.get_text() + "\n"
        return texto_completo.strip()
    except Exception as e:
        return f"[Erro ao processar PDF: Verifique se o PyMuPDF está instalado. Detalhe: {e}]"

def extrair_conteudo_tabela(arquivo_bytes: bytes, nome_arquivo: str) -> str:
    """Extrai estrutura, metadados e amostras de dados de arquivos CSV ou Excel."""
    try:
        import pandas as pd
        if nome_arquivo.lower().endswith('.csv'):
            df = pd.read_csv(io.BytesIO(arquivo_bytes))
        elif nome_arquivo.lower().endswith(('.xls', '.xlsx')):
            df = pd.read_excel(io.BytesIO(arquivo_bytes))
        else:
            return ""
        
        info = (
            f"Dimensões: {df.shape[0]} linhas x {df.shape[1]} colunas\n"
            f"Colunas: {', '.join(df.columns.astype(str))}\n"
            f"Tipos de Dados:\n{df.dtypes.to_string()}\n"
            f"Amostra dos Dados (5 primeiras linhas):\n{df.head(5).to_string()}\n"
        )
        return info
    except Exception as e:
        return f"[Erro ao ler tabela de dados ({nome_arquivo}): {e}]"

# =============================================================
# 2. 🐍 CODE INTERPRETER & SANDBOX PYTHON LOCAL
# =============================================================

def executar_codigo_sandbox(codigo_python: str) -> dict:
    """Executa scripts Python localmente capturando stdout, erros e gráficos matplotlib."""
    os.makedirs("geracoes_lume", exist_ok=True)
    output_buffer = io.StringIO()
    caminho_grafico = None
    
    cod_limpo = re.sub(r"^```(?:python)?\s*|\s*```$", "", codigo_python.strip(), flags=re.MULTILINE)
    
    namespace_global = {
        "__builtins__": __builtins__,
        "os": os,
        "re": re,
        "math": __import__("math"),
        "datetime": datetime,
        "time": time,
        "json": json
    }
    
    try:
        import numpy as np
        namespace_global["np"] = np
        namespace_global["numpy"] = np
    except ImportError:
        pass

    try:
        import pandas as pd
        namespace_global["pd"] = pd
        namespace_global["pandas"] = pd
    except ImportError:
        pass

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        plt.show = lambda *args, **kwargs: None
        plt.clf()
        plt.close('all')
        
        namespace_global["plt"] = plt
        namespace_global["matplotlib"] = matplotlib
    except ImportError:
        plt = None

    try:
        with contextlib.redirect_stdout(output_buffer), contextlib.redirect_stderr(output_buffer):
            exec(cod_limpo, namespace_global)
        
        if plt and len(plt.get_fignums()) > 0:
            timestamp = int(time.time())
            caminho_grafico = os.path.join("geracoes_lume", f"plot_{timestamp}.png")
            plt.savefig(caminho_grafico, bbox_inches="tight", dpi=150)
            plt.close('all')
            
        saida = output_buffer.getvalue().strip()
        return {
            "sucesso": True,
            "saida": saida if saida else "[Execução concluída sem saídas de texto]",
            "grafico": caminho_grafico
        }
    except Exception:
        erro_trace = traceback.format_exc()
        return {
            "sucesso": False,
            "saida": f"Erro de Execução:\n{erro_trace}",
            "grafico": None
        }

# =============================================================
# 3. 🌐 MOTOR DE BUSCA WEB PROFUNDA (FILTRADA POR ANO)
# =============================================================

def otimizar_query_busca(prompt: str, contexto_recente: str = "") -> tuple[str, bool]:
    texto_base = f"{contexto_recente} {prompt}".strip().lower() if contexto_recente else prompt.lower()
    
    # Atalho cirúrgico para consultas sobre eleições presidenciais de 2026
    if any(t in texto_base for t in ["eleições", "eleicao", "presidente", "segundo turno"]) and "2026" in texto_base:
        return "resultado segundo turno eleicoes presidente brasil 2026 lula flavio bolsonaro tse", True

    termos_noticia = [
        "noticia", "notícia", "noticias", "notícias", "ultimas", "últimas", 
        "hoje", "acontecendo", "lançamento", "lancamentos", "eleições", 
        "eleicao", "presidente", "ganhou", "venceu", "resultado", "turno",
        "votos", "disputa", "disputou", "acirrada", "bolsonaro", "flávio", 
        "flavio", "lula", "2026", "2025"
    ]
    eh_noticia = any(t in texto_base for t in termos_noticia)
    
    stopwords_busca = [
        "procure na internet", "pesquise na internet", "busque na internet",
        "procure na web", "pesquise na web", "busque na web",
        "consegue me dizer", "você sabe", "voce sabe", "me diga", "me fale",
        "gostaria de saber", "por favor", "lume", "olá", "ola", "oi",
        "bom dia", "boa tarde", "boa noite", "quem", "qual", "quais",
        "como", "onde", "quando", "por que", "porque", "ficou", "foi",
        "disputou", "disputaram", "ganhou", "venceu", "enfrentaram", "enfrentou", "perdeu",
        "bem", "muito", "sobre", "para", "com", "que", "uma", "uns",
        "mas", "ele", "ela", "não", "nao", "sim", "então", "entao", "quantos"
    ]
    
    termo = texto_base
    for sw in stopwords_busca:
        termo = re.sub(rf'\b{re.escape(sw)}\b', ' ', termo)
    
    termo = re.sub(r'[^\w\s]', ' ', termo)
    palavras = []
    for p in termo.split():
        if len(p) > 2 or p.isdigit():
            if p not in palavras:
                palavras.append(p)
    
    termo_limpo = " ".join(palavras[:6])
    ano_atual = datetime.now().year
    
    if eh_noticia:
        if not termo_limpo:
            return f"principais noticias brasil {ano_atual}", True
        if str(ano_atual) not in termo_limpo and "2025" not in termo_limpo:
            return f"{termo_limpo} {ano_atual}", True
        return termo_limpo, True
        
    return (termo_limpo if termo_limpo else prompt), False

def pesquisar_web_direta(query: str, contexto_recente: str = "") -> str:
    query_limpa, eh_noticia = otimizar_query_busca(query, contexto_recente)
    query_segura = mascarar_dados_sensiveis(query_limpa)
    blocos_coletados = []
    ano_atual_str = str(datetime.now().year)
    busca_pede_ano_atual = ano_atual_str in query_segura

    # 1. DuckDuckGo News & Text (Prioridade para fatos atuais e percentagens do TSE)
    try:
        with DDGS(timeout=8) as ddgs:
            if eh_noticia:
                for r in ddgs.news(query_segura, safesearch="moderate", max_results=5):
                    t = r.get('title', '')
                    b = r.get('body', '')
                    if b:
                        blocos_coletados.append(f"📰 **{t}**\n{b}")
            for r in ddgs.text(query_segura, safesearch="moderate", max_results=5):
                t = r.get('title', '')
                b = r.get('body', '')
                if b and len(b) > 25:
                    blocos_coletados.append(f"📌 **{t}**\n{b}")
    except Exception:
        pass

    # 2. Google News RSS
    try:
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query_segura)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        r_rss = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r_rss.status_code == 200:
            root = ET.fromstring(r_rss.content)
            for item in root.findall(".//item")[:5]:
                titulo = (item.findtext("title") or "").strip()
                pub_date = item.findtext("pubDate") or ""
                if titulo:
                    data_curta = f" ({pub_date[:16]})" if pub_date else ""
                    blocos_coletados.append(f"📰 **{titulo}**{data_curta}")
    except Exception:
        pass

    # 3. Wikipédia API (Com filtro para ignorar anos passados como 2014/2022 quando a busca é sobre 2026)
    try:
        wiki_search_url = (
            f"https://pt.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={urllib.parse.quote(query_segura)}&format=json&utf8="
        )
        r_ws = requests.get(wiki_search_url, headers={"User-Agent": "LumeAI/2.0"}, timeout=6)
        if r_ws.status_code == 200:
            resultados_wiki = r_ws.json().get("query", {}).get("search", [])
            for w in resultados_wiki[:2]:
                titulo_pag = w.get("title", "")
                if busca_pede_ano_atual and any(ano_ant in titulo_pag for ano_ant in ["2010", "2014", "2018", "2022"]):
                    continue
                if titulo_pag:
                    ext_url = (
                        f"https://pt.wikipedia.org/w/api.php?action=query&prop=extracts"
                        f"&exintro=1&explaintext=1&redirects=1&titles={urllib.parse.quote(titulo_pag)}&format=json"
                    )
                    r_ext = requests.get(ext_url, headers={"User-Agent": "LumeAI/2.0"}, timeout=6)
                    if r_ext.status_code == 200:
                        pages = r_ext.json().get("query", {}).get("pages", {})
                        for _, p_data in pages.items():
                            extrato = p_data.get("extract", "").strip()
                            if extrato:
                                blocos_coletados.append(f"📚 **Wikipédia ({titulo_pag})**\n{extrato[:900]}")
    except Exception:
        pass

    if blocos_coletados:
        return "\n\n".join(blocos_coletados[:8])

    return "⚠️ Nenhuma fonte verificada retornou números exatos para esta consulta no momento."

# =============================================================
# 4. 🎨 MOTOR DE GERAÇÃO E AUTOAVALIAÇÃO VISUAL (DIFFUSION + VLM)
# =============================================================

def limpar_texto_geracao(prompt: str) -> str:
    """Remove comandos em português e negações que induzem pessoas na cena."""
    p = prompt.lower()
    termos_remover = [
        "gere uma imagem de um", "gere uma imagem de uma", "gere uma imagem de", "gere uma imagem",
        "crie uma imagem de um", "crie uma imagem de uma", "crie uma imagem de", "crie uma imagem",
        "faça uma imagem de você mesmo usando essa como base", "faça uma imagem de você mesmo",
        "faça uma imagem de", "faça uma imagem", "faca uma imagem", "faça um desenho de", "faça um desenho",
        "gere uma foto de um", "gere uma foto de uma", "gere uma foto de", "gere uma foto",
        "desenhe um", "desenhe uma", "desenhe", "ilustre",
        "sem ninguem", "sem pessoas", "somente o", "somente a", "apenas o", "apenas a"
    ]
    for termo in termos_remover:
        p = re.sub(rf'\b{re.escape(termo)}\b', ' ', p)
    return " ".join(p.split())

def autoavaliar_e_aprender_imagem(caminho_imagem: str, prompt_usuario: str, prompt_en: str, id_modelo: str) -> str:
    """Analisa a imagem gerada via VLM e grava os aprendizados de estilo no ChromaDB."""
    try:
        if not os.path.exists(caminho_imagem):
            return ""

        with open(caminho_imagem, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")

        prompt_critico = (
            f"Você é o crítico de arte do Lume. Avalie esta imagem gerada para a diretriz: '{prompt_usuario}'. "
            f"Responda em no máximo 2 frases em português dizendo o que ficou bom em termos de iluminação, "
            f"enquadramento e fidelidade temática."
        )

        res = client_local.chat.completions.create(
            model=id_modelo,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_critico},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                ]
            }],
            max_tokens=80,
            timeout=15.0
        )

        critica = res.choices[0].message.content.strip()
        
        if critica and "⚠️" not in critica:
            fato_aprendizado = f"Estilo e composição visual bem-sucedidos para '{prompt_usuario}': prompt usado ('{prompt_en}'). Análise: {critica}"
            salvar_memoria(fato_aprendizado, usuario="lume_vision_critic")

        return critica
    except Exception as e:
        print(f"⚠️ Crítico visual indisponível ou timeout: {e}")
        return ""

def gerar_imagem_local(prompt_usuario: str, id_modelo: str, imagem_referencia_bytes: bytes = None) -> str:
    """Consulta memórias passadas, traduz o prompt (ou analisa imagem base), sintetiza a imagem e autoavalia."""
    os.makedirs("geracoes_lume", exist_ok=True)
    
    termo_limpo = limpar_texto_geracao(prompt_usuario)
    if not termo_limpo.strip() and not imagem_referencia_bytes:
        termo_limpo = "lume artificial intelligence neural sphere glowing"

    memoria_estilo = consultar_memoria(f"Estilo e composição visual para '{termo_limpo}'")
    contexto_memoria = f"\nUse estes aprendizados prévios de estilo: {memoria_estilo[:200]}" if memoria_estilo else ""

    prompt_en = termo_limpo

    try:
        if imagem_referencia_bytes:
            b64_ref = codificar_imagem_base64(imagem_referencia_bytes)
            conteudo_msg = [
                {
                    "type": "text", 
                    "text": (
                        f"Analyze this reference image and the user directive: '{prompt_usuario}'. "
                        f"Extract its visual style, colors, lighting, and subject to create a concise, high quality "
                        f"English descriptive diffusion prompt that recreates/evolves this concept. "
                        f"Return ONLY the English prompt text."
                    )
                },
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_ref}"}}
            ]
        else:
            conteudo_msg = (
                f"Translate the following subject into a concise English descriptive prompt for an image generator. "
                f"Include the subject and high quality visual tags (lighting, focus, angle). "
                f"Never include humans or portraits unless explicitly requested.{contexto_memoria}\n\n"
                f"Subject: '{termo_limpo}'\n\nPrompt:"
            )

        res = client_local.chat.completions.create(
            model=id_modelo,
            messages=[{"role": "user", "content": conteudo_msg}],
            max_tokens=60,
            timeout=15.0
        )
        conteudo = res.choices[0].message.content.strip().replace('"', '').replace("'", "")
        if conteudo:
            prompt_en = conteudo
    except Exception:
        dicionario_rapido = {
            "piano": "grand piano in a room, aesthetic lighting, photorealistic, 8k", 
            "gato": "cute domestic cat, detailed fur, studio lighting, highly detailed", 
            "carro": "modern sports car, cinematic shot, 8k",
            "lume": "glowing golden neural AI core, futuristic energy sphere, digital particles, dark background, 8k"
        }
        for k, v in dicionario_rapido.items():
            if k in prompt_usuario.lower():
                prompt_en = v
                break

    print(f"\n🎨 [Difusão] Prompt gerado: '{prompt_en}'")

    caminho_salvo = ""
    try:
        timestamp = int(time.time())
        semente_aleatoria = random.randint(1, 999999)
        prompt_codificado = urllib.parse.quote(prompt_en)
        url_imagem = f"https://image.pollinations.ai/prompt/{prompt_codificado}?width=1024&height=1024&nologo=true&seed={semente_aleatoria}"
        
        resp_img = requests.get(url_imagem, timeout=30)
        if resp_img.status_code == 200:
            caminho_salvo = os.path.join("geracoes_lume", f"img_{timestamp}.png")
            with open(caminho_salvo, "wb") as f:
                f.write(resp_img.content)
        else:
            return f"🎨 Imagem sintetizada com base na sua diretriz visual!\n\n[IMAGEM_GERADA:{url_imagem}]"
    except Exception as e:
        return f"⚠️ Falha na geração de imagem: {e}."

    critica_visual = autoavaliar_e_aprender_imagem(caminho_salvo, termo_limpo, prompt_en, id_modelo)
    feedback_txt = f"\n\n🔍 **Autoavaliação Lume:** {critica_visual}" if critica_visual else ""

    return f"🎨 Imagem sintetizada com base na sua diretriz visual!{feedback_txt}\n\n[IMAGEM_GERADA:{caminho_salvo}]"

# =============================================================
# 5. 🧠 ROTEADOR DE INTENÇÕES COM PRIORIDADE ESTRITA
# =============================================================

def classificar_intencao_inteligente(prompt: str, id_modelo: str, tem_anexos: bool, historico_txt: str = "") -> dict:
    """Garante que comandos de memória, código e imagem nunca sejam sobrescritos pelo histórico de busca."""
    
    if tem_anexos and not prompt.strip():
        return {"intencao": "DIRETO", "parametros": {}}

    p = prompt.lower().strip()
    hist_lower = historico_txt.lower()

    # PRIORIDADE 1: SALVAR NA MEMÓRIA VETORIAL (CHROMADB)
    gatilhos_salvar = [
        "registre", "registrar", "banco vetorial", "chromadb", "armazene", "armazenar",
        "guarde", "guardar", "lembre", "lembrar", "salve", "salvar", "memorize", 
        "memorizar", "anote", "anotar", "grave", "gravar na memória", "sua memória"
    ]
    if any(g in p for g in gatilhos_salvar):
        return {"intencao": "SALVAR_MEMORIA", "parametros": {}}

    # PRIORIDADE 2: CONSULTAR MEMÓRIA PESSOAL
    gatilhos_memoria = [
        "sobre o biel", "sobre o gabriel", "sobre seu criador", "quem sou eu", 
        "meu nome", "minhas preferências", "quem te criou", "o que você sabe sobre mim",
        "o que está salvo", "quais memórias"
    ]
    if any(m in p for m in gatilhos_memoria):
        return {"intencao": "MEMORIA_PESSOAL", "parametros": {}}

    # PRIORIDADE 3: GERAR IMAGEM
    gatilhos_imagem = [
        "gere uma imagem", "gerar imagem", "crie uma imagem", "criar imagem", 
        "faça uma imagem", "desenhe", "ilustre", "ilustrar", "visualizar como", 
        "renderize uma imagem", "foto realista", "arte digital", "usando essa como base"
    ]
    if any(g in p for g in gatilhos_imagem):
        return {"intencao": "GERAR_IMAGEM", "parametros": {}}

    # PRIORIDADE 4: EXECUTAR CÓDIGO / GRÁFICOS
    gatilhos_codigo = [
        "python", "código", "codigo", "script", "plot", "plote", "gráfico", 
        "grafico", "matplotlib", "seaborn", "calcule", "simule", "dataframe"
    ]
    if any(c in p for c in gatilhos_codigo):
        return {"intencao": "EXECUTAR_CODIGO", "parametros": {}}

    # PRIORIDADE 5: BUSCA WEB DIRETA E PERGUNTAS DE CONTINUAÇÃO FACTUAL
    gatilhos_forcar_web = [
        "eleições", "eleicao", "eleito", "presidente", "quem ganhou", "quem venceu",
        "segundo turno", "primeiro turno", "acirrada", "disputa", "disputou", "adversário",
        "quantos %", "porcentagem", "votos", "bolsonaro", "flávio", "flavio", "lula", 
        "boulos", "tarcísio", "tarcisio", "2025", "2026", "notícias", "noticia", 
        "ultimas", "últimas", "clima", "previsão", "tempo hoje", "cotação", "dólar", 
        "bitcoin", "pesquise", "busque", "procure na web", "na internet"
    ]
    if any(g in p for g in gatilhos_forcar_web):
        return {"intencao": "BUSCA_WEB", "parametros": {"query": prompt}}

    temas_atuais_historico = ["eleições", "eleição", "presidente", "2026", "segundo turno", "notícias"]
    if any(t in hist_lower for t in temas_atuais_historico) and len(p.split()) <= 18:
        return {"intencao": "BUSCA_WEB", "parametros": {"query": prompt}}

    # PRIORIDADE 6: SAUDAÇÃO RÁPIDA
    cumprimentos = ["oi", "ola", "olá", "boa noite", "bom dia", "boa tarde", "e ai", "e aí"]
    if p in cumprimentos or (len(p.split()) <= 2 and any(c in p for c in cumprimentos)):
        return {"intencao": "SAUDACAO", "parametros": {}}

    return {"intencao": "DIRETO", "parametros": {}}

# =============================================================
# 6. ⚙️ SELEÇÃO DINÂMICA DE MODELOS (API GEMINI + FALLBACK)
# =============================================================

def obter_modelo_para_modo(modo: str, tem_imagem: bool = False) -> str:
    """Seleciona o modelo da família Gemini 2.5 quando há chave ou usa o modelo gratuito padrão."""
    if not obter_chave_gemini():
        return "openai"
    if tem_imagem:
        return "gemini-2.5-flash"
    if "Ultra Flash" in modo:
        return "gemini-2.5-flash-lite"
    elif "Pensamento Profundo" in modo:
        return "gemini-2.5-pro"
    return "gemini-2.5-flash"

def gerar_micro_resumo(contexto_historico: str, id_modelo: str) -> str:
    """Gera um resumo semântico curto para arquivamento no ChromaDB."""
    try:
        res = client_local.chat.completions.create(
            model=id_modelo,
            messages=[{"role": "user", "content": f"Sintetize em 1 frase curta o tópico desta conversa:\n\n{contexto_historico}\n\nResumo:"}],
            max_tokens=50,
            timeout=10.0
        )
        return res.choices[0].message.content.strip()
    except Exception:
        return ""

# =============================================================
# 7. ⚡ EXECUTOR HÍBRIDO EM STREAMING (MULTIMODAL 2-PASSOS)
# =============================================================

CACHE_RESPOSTAS_TEMP = {}

def extrair_fato_para_salvar(prompt: str, mensagens_anteriores: list) -> str:
    """Identifica se o usuário quer salvar a informação da resposta anterior ou um fato escrito no próprio prompt."""
    p_lower = prompt.lower()
    termos_referencia = [
        "essa informação", "esta informação", "essa informacao", "esta informacao",
        "isso", "isto", "essa resposta", "esta resposta", "esse resultado", 
        "esse fato", "o que você disse", "acima", "anterior"
    ]
    respostas_assistente = [t for a, t, _ in mensagens_anteriores if a == "assistant" and t.strip()]
    
    if respostas_assistente and (any(tr in p_lower for tr in termos_referencia) or len(prompt.split()) <= 10):
        ultima_resp = respostas_assistente[-1].strip()
        ultima_resp_limpa = re.sub(r"^Com base nas fontes verificadas.*?:\s*", "", ultima_resp, flags=re.IGNORECASE).strip()
        return f"Fato registrado da conversa: {ultima_resp_limpa[:500]}"
    
    return f"Fato explícito salvo a pedido do usuário: {prompt}"

def gerar_resposta_contingencia(prompt: str, intencao: str, contexto_web: str, memoria_vetorial: str, contexto_anexos: str, messages: list, fato_gravado: str = "") -> str:
    """Tenta múltiplos modelos gratuitos antes de recorrer à síntese local."""
    if intencao == "SALVAR_MEMORIA" and fato_gravado:
        return f"✅ **Informação registrada com sucesso no meu banco vetorial (ChromaDB)!**\n\n> *{fato_gravado}*\n\nVocê já pode consultar ou editar esse registro abrindo a aba **⚙️ Gerenciar Banco Vetorial** na barra lateral esquerda."

    for modelo_alt in ["openai", "mistral", "openai-fast"]:
        try:
            time.sleep(0.5)
            res_backup = requests.post(
                "https://text.pollinations.ai/openai",
                json={"model": modelo_alt, "messages": messages, "temperature": 0.2},
                timeout=25
            )
            if res_backup.status_code == 200:
                conteudo = res_backup.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                if conteudo and len(conteudo.strip()) > 10:
                    CACHE_RESPOSTAS_TEMP[prompt.lower().strip()] = conteudo
                    return conteudo
        except Exception:
            continue

    chave_prompt = prompt.lower().strip()
    if chave_prompt in CACHE_RESPOSTAS_TEMP:
        return CACHE_RESPOSTAS_TEMP[chave_prompt]

    if intencao == "SAUDACAO":
        return "Olá! Tudo ótimo por aqui. Como posso ajudar você hoje?"
    if contexto_anexos:
        return f"Analisei os documentos anexados. Aqui estão as informações extraídas:\n\n{contexto_anexos}"
    if contexto_web and "⚠️" not in contexto_web:
        return f"Com base nas fontes verificadas em tempo real:\n\n{contexto_web}"
    if memoria_vetorial:
        return f"Consultando minha memória pessoal (ChromaDB), encontrei estes registros:\n\n{memoria_vetorial}"

    return "O servidor gratuito atingiu o limite momentâneo de requisições. Aguarde alguns segundos ou insira sua chave gratuita do Google AI Studio em '🔑 Configurar API' na barra lateral!"

def processar_resposta_ia_stream(prompt: str, conversa_id: str, modo: str, anexos: list = None):
    """Executa o pipeline multimodal em 2 passos com suporte a Sandbox Python, PDFs, Dados e Imagens."""
    if verificar_injecao_prompt(prompt):
        yield "⚠️ **Alerta de Segurança:** Diretriz de proteção ativada contra comandos maliciosos."
        return

    anexos = anexos or []
    tem_pdf = any("pdf" in a.get("tipo", "") for a in anexos)
    tem_imagem = any("image" in a.get("tipo", "") for a in anexos)
    tem_tabela = any(a.get("nome", "").lower().endswith(('.csv', '.xls', '.xlsx')) for a in anexos)
    
    id_modelo_raciocinio = obter_modelo_para_modo(modo, tem_imagem=False)
    id_modelo_visao = obter_modelo_para_modo(modo, tem_imagem=True)

    mensagens_anteriores = carregar_mensagens(conversa_id)[-6:]
    ultimas_msgs_usuario = " ".join([t for a, t, _ in mensagens_anteriores[-4:] if a == "user"])

    decisao_llm = classificar_intencao_inteligente(
        prompt, id_modelo_raciocinio, tem_anexos=bool(anexos), historico_txt=ultimas_msgs_usuario
    )
    intencao = decisao_llm["intencao"]

    if intencao == "GERAR_IMAGEM" and not tem_pdf:
        imagem_referencia_bytes = next((a["bytes"] for a in anexos if "image" in a.get("tipo", "")), None)
        yield "🎨 Analisando diretrizes e sintetizando imagem...\n\n"
        id_modelo_geracao = id_modelo_visao if imagem_referencia_bytes else id_modelo_raciocinio
        resultado_img = gerar_imagem_local(prompt, id_modelo_geracao, imagem_referencia_bytes=imagem_referencia_bytes)
        yield resultado_img
        return

    contexto_anexos = ""
    
    # ---------------------------------------------------------
    # PASSO 1: EXTRAÇÃO DE DADOS (VISÃO, TABELAS E PDF)
    # ---------------------------------------------------------
    if anexos:
        yield "🔍 Lendo documentos e extraindo contexto...\n\n"
        
        for arq in anexos:
            nome_arq = arq.get("nome", "")
            tipo_arq = arq.get("tipo", "")
            
            if "pdf" in tipo_arq:
                texto_pdf = extrair_texto_pdf(arq["bytes"])
                contexto_anexos += f"\n--- CONTEÚDO DO PDF ({nome_arq}) ---\n{texto_pdf[:4000]}...\n"
            
            elif nome_arq.lower().endswith(('.csv', '.xls', '.xlsx')):
                texto_tabela = extrair_conteudo_tabela(arq["bytes"], nome_arq)
                contexto_anexos += f"\n--- DADOS DA TABELA/PLANILHA ({nome_arq}) ---\n{texto_tabela}\n"

            elif "image" in tipo_arq:
                img_b64 = codificar_imagem_base64(arq["bytes"])
                try:
                    res_visao = client_local.chat.completions.create(
                        model=id_modelo_visao,
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": "Descreva esta imagem em detalhes absolutos. Extraia todos os textos, dados, cores e formas. Não tire conclusões, apenas relate objetivamente o que vê."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                        ]}],
                        max_tokens=300,
                        timeout=20.0
                    )
                    desc_img = res_visao.choices[0].message.content.strip()
                    contexto_anexos += f"\n--- DESCRIÇÃO DA IMAGEM ({nome_arq}) ---\n{desc_img}\n"
                except Exception as e:
                    contexto_anexos += f"\n[Falha ao ler a imagem {nome_arq}: {e}]\n"

    # ---------------------------------------------------------
    # PASSO 2: RACIOCÍNIO CENTRAL E EXECUÇÃO (MODELO PRINCIPAL)
    # ---------------------------------------------------------
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    fato_gravado = ""
    if intencao == "SALVAR_MEMORIA":
        fato_gravado = extrair_fato_para_salvar(prompt, mensagens_anteriores)
        salvar_memoria(fato_gravado, usuario="padrao")

    contexto_web = ""
    if intencao == "BUSCA_WEB" and not anexos:
        contexto_web = pesquisar_web_direta(prompt, contexto_recente=ultimas_msgs_usuario)

    memoria_vetorial = ""
    resumos_outros_chats = ""

    if intencao in ["MEMORIA_PESSOAL", "SALVAR_MEMORIA"]:
        query_memoria = f"{prompt} Gabriel Biel criador fatos preferências projetos perfil"
        memoria_vetorial = consultar_memoria(query_memoria)
        resumos_outros_chats = consultar_resumos_chats(query_memoria, chat_id_atual=conversa_id)

    partes_sistema = [
        f"Você é o Lume, assistente inteligente criado pelo Gabriel (Biel). Hoje é {data_hoje}.",
        "Responda sempre em português do Brasil com clareza, precisão factual e naturalidade.",
        "⚠️ DIRETRIZ DE IDENTIDADE: Assuma 100% a sua identidade como Lume.",
        "⚠️ REGRA RÍGIDA ANTI-ALUCINAÇÃO: NUNCA tente adivinhar ou completar sobrenomes entre parênteses que não estejam escritos nas fontes abaixo (ex: nunca escreva 'Flávio Dino' se a notícia falar de 'Flávio Bolsonaro' ou apenas 'Flávio'). Use exclusivamente os nomes completos e porcentagens que aparecem em 'FONTES VERIFICADAS DA WEB'.",
        "⚠️ DIRETRIZ DA MEMÓRIA: Você possui um banco vetorial (ChromaDB) integrado à sua arquitetura. Quando uma informação for salva, confirme calorosamente ao usuário mostrando um resumo do que foi gravado no banco vetorial."
    ]

    if fato_gravado:
        partes_sistema.append(f"\n--- REGISTRO ACABOU DE SER SALVO NO CHROMADB COM SUCESSO ---\nConteúdo gravado: {fato_gravado}")
    if contexto_web:
        partes_sistema.append(f"\n--- FONTES VERIFICADAS DA WEB (USE APENAS ESTES FATOS) ---\n{contexto_web}")
    if memoria_vetorial:
        partes_sistema.append(f"\n--- FATOS DA MEMÓRIA ---\n{memoria_vetorial}")
    if resumos_outros_chats:
        partes_sistema.append(f"\n--- HISTÓRICO RELEVANTE ---\n{resumos_outros_chats}")
    if contexto_anexos:
        partes_sistema.append(f"\n--- DADOS EXTRAÍDOS DOS ANEXOS ---\n{contexto_anexos}")

    messages = [{"role": "system", "content": "\n".join(partes_sistema)}]

    for a, t, _ in mensagens_anteriores[-4:]:
        messages.append({"role": "user" if a == "user" else "assistant", "content": t[:1200]})

    if intencao == "SALVAR_MEMORIA":
        messages.append({
            "role": "user",
            "content": f"{prompt}\n\n[Instrução Interna: Confirme ao usuário que a informação '{fato_gravado[:200]}' já foi gravada com sucesso no seu banco vetorial ChromaDB.]"
        })
    elif contexto_web:
        prompt_enriquecido = (
            f"Pergunta do usuário: {prompt}\n\n"
            f"[Instrução Interna: Responda de forma conversacional usando os dados das FONTES VERIFICADAS DA WEB acima. "
            f"Jamais adicione sobrenomes entre parênteses que não estejam nas fontes.]"
        )
        messages.append({"role": "user", "content": prompt_enriquecido})
    else:
        messages.append({"role": "user", "content": prompt if prompt.strip() else "Analise os anexos extraídos."})

    resposta_acumulada = ""
    try:
        stream = client_local.chat.completions.create(
            model=id_modelo_raciocinio,
            messages=messages,
            temperature=0.2 if intencao == "BUSCA_WEB" else 0.7,
            stream=True
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                resposta_acumulada += token
                yield token
        if resposta_acumulada:
            CACHE_RESPOSTAS_TEMP[prompt.lower().strip()] = resposta_acumulada
            
    except Exception as e:
        print(f"⚠️ API principal oscilou ({e}). Acionando contingência multi-modelo...")
        resposta_fallback = gerar_resposta_contingencia(
            prompt, intencao, contexto_web, memoria_vetorial, contexto_anexos, messages, fato_gravado=fato_gravado
        )
        resposta_acumulada = resposta_fallback
        yield resposta_fallback

    if "```python" in resposta_acumulada and (intencao == "EXECUTAR_CODIGO" or tem_tabela):
        try:
            codigos_encontrados = re.findall(r"```(?:python)?\s*(.*?)\s*```", resposta_acumulada, re.DOTALL)
            if codigos_encontrados:
                yield "\n\n⚙️ **Executando código no Code Interpreter Sandbox...**\n"
                codigo_alvo = codigos_encontrados[-1]
                resultado_sandbox = executar_codigo_sandbox(codigo_alvo)
                
                saida_formatada = f"\n**Resultado da Execução:**\n```text\n{resultado_sandbox['saida']}\n```"
                resposta_acumulada += saida_formatada
                yield saida_formatada
                
                if resultado_sandbox.get("grafico"):
                    tag_img = f"\n\n[IMAGEM_GERADA:{resultado_sandbox['grafico']}]"
                    resposta_acumulada += tag_img
                    yield tag_img
        except Exception as e:
            print(f"⚠️ Falha no Code Interpreter pós-processamento: {e}")

    if resposta_acumulada and "⚠️" not in resposta_acumulada:
        resumo = gerar_micro_resumo(f"Usuário: {prompt}\nLume: {resposta_acumulada}", id_modelo_raciocinio)
        if resumo:
            salvar_ou_atualizar_resumo_chat(conversa_id, f"Chat {conversa_id}: {resumo}")
