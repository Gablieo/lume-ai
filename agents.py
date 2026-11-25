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
GEMINI_NATIVE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
FREE_LLM_BASE_URL = "https://text.pollinations.ai/openai"

# Cache em memória dos modelos Gemini ativos descobertos por chave
CACHE_MODELOS_GEMINI = {}


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


def descobrir_modelos_gemini(chave: str) -> list[str]:
    """Consulta a API nativa do Google para listar modelos Gemini ativos que suportam generateContent."""
    if not chave:
        return []

    chave_id = chave[-12:]
    if chave_id in CACHE_MODELOS_GEMINI and CACHE_MODELOS_GEMINI[chave_id]:
        return CACHE_MODELOS_GEMINI[chave_id]

    termos_ignorar = [
        "embedding", "aqa", "imagen", "veo", "tts", "audio", "live",
        "robotics", "computer-use", "deep-research", "image-generation",
        "image-preview", "-image"
    ]

    try:
        resp = requests.get(
            f"{GEMINI_NATIVE_BASE_URL}/models",
            params={"key": chave},
            timeout=10
        )
        if resp.status_code == 200:
            modelos_validos = []
            for m in resp.json().get("models", []):
                metodos = m.get("supportedGenerationMethods", [])
                if "generateContent" not in metodos:
                    continue
                nome = m.get("name", "").replace("models/", "").strip()
                nome_low = nome.lower()
                if "gemini" not in nome_low:
                    continue
                if any(t in nome_low for t in termos_ignorar):
                    continue
                modelos_validos.append(nome)

            if modelos_validos:
                CACHE_MODELOS_GEMINI[chave_id] = modelos_validos
                return modelos_validos
    except Exception as e:
        print(f"⚠️ Não foi possível listar modelos Gemini automaticamente: {e}")

    return []


def _chave_ordenacao_modelo(nome: str) -> tuple:
    """Prioriza versões mais recentes e modelos estáveis sobre previews/experimentais."""
    n = nome.lower()
    m = re.search(r'gemini-(\d+)(?:\.(\d+))?', n)
    major = int(m.group(1)) if m else 0
    minor = int(m.group(2)) if (m and m.group(2)) else 0
    eh_estavel = 0 if ("exp" in n or "preview" in n) else 1
    eh_latest = 1 if "latest" in n else 0
    return (major, minor, eh_estavel, eh_latest, n)


def listar_candidatos_gemini(modo: str, chave: str, tem_imagem: bool = False) -> list[str]:
    """Retorna lista ordenada de modelos Gemini válidos para o perfil de raciocínio escolhido."""
    disponiveis = descobrir_modelos_gemini(chave)

    if not disponiveis:
        if "Pensamento Profundo" in modo or "Profundo" in modo:
            return ["gemini-2.5-pro", "gemini-pro-latest", "gemini-2.5-flash", "gemini-flash-latest"]
        if "Ultra Flash" in modo or "Rápido" in modo:
            return ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash"]
        return ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-pro"]

    flash_lite = sorted([m for m in disponiveis if "flash-lite" in m.lower()], key=_chave_ordenacao_modelo, reverse=True)
    flash_padrao = sorted([m for m in disponiveis if "flash" in m.lower() and "lite" not in m.lower()], key=_chave_ordenacao_modelo, reverse=True)
    pro_modelos = sorted([m for m in disponiveis if "pro" in m.lower()], key=_chave_ordenacao_modelo, reverse=True)
    outros = sorted(disponiveis, key=_chave_ordenacao_modelo, reverse=True)

    if "Pensamento Profundo" in modo or "Profundo" in modo:
        ordem = pro_modelos + flash_padrao + flash_lite + outros
    elif ("Ultra Flash" in modo or "Rápido" in modo) and not tem_imagem:
        ordem = flash_lite + flash_padrao + pro_modelos + outros
    else:
        ordem = flash_padrao + pro_modelos + flash_lite + outros

    # Remove duplicados preservando a prioridade
    unicos = []
    for m in ordem:
        if m not in unicos:
            unicos.append(m)
    return unicos


def criar_cliente_openai(chave_override: str = "") -> OpenAI:
    """Instancia o cliente OpenAI conectado à API oficial do Gemini ou ao fallback gratuito."""
    chave = chave_override.strip() if chave_override else obter_chave_gemini()
    if chave:
        return OpenAI(
            base_url=GEMINI_BASE_URL,
            api_key=chave,
            timeout=90.0
        )
    return OpenAI(
        base_url=FREE_LLM_BASE_URL,
        api_key="lume-free-llm",
        timeout=90.0
    )


class ClienteDinamicoOpenAI:
    """Conecta à API oficial do Gemini quando há chave ou usa o LLM gratuito automaticamente."""
    @property
    def chat(self):
        return criar_cliente_openai().chat


client_local = ClienteDinamicoOpenAI()


def chamar_gemini_nativo(
    chave: str,
    modelos_candidatos: list[str],
    system_text: str,
    historico_mensagens: list,
    texto_usuario: str,
    imagens: list = None,
    temperature: float = 0.5
) -> tuple[bool, str]:
    """Chama a API REST nativa do Gemini (generateContent) com suporte completo a imagens inline_data."""
    contents = []

    for autor, texto in historico_mensagens:
        if not texto or not texto.strip():
            continue
        role = "user" if autor == "user" else "model"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": texto[:1200]})
        else:
            contents.append({"role": role, "parts": [{"text": texto[:1200]}]})

    partes_atuais = [{"text": texto_usuario}]
    for img in (imagens or []):
        partes_atuais.append({
            "inline_data": {
                "mime_type": img["mime"],
                "data": img["b64"]
            }
        })

    if contents and contents[-1]["role"] == "user":
        contents[-1]["parts"].extend(partes_atuais)
    else:
        contents.append({"role": "user", "parts": partes_atuais})

    payload = {
        "contents": contents,
        "generationConfig": {"temperature": temperature}
    }
    if system_text:
        payload["system_instruction"] = {"parts": [{"text": system_text}]}

    ultimo_erro = "Nenhum modelo Gemini disponível respondeu."

    for modelo in modelos_candidatos[:4]:
        try:
            resp = requests.post(
                f"{GEMINI_NATIVE_BASE_URL}/models/{modelo}:generateContent",
                params={"key": chave},
                json=payload,
                timeout=60
            )
            if resp.status_code == 200:
                dados = resp.json()
                candidatos = dados.get("candidates", [])
                if candidatos:
                    partes_resp = candidatos[0].get("content", {}).get("parts", [])
                    texto_final = "".join(
                        p.get("text", "") for p in partes_resp if not p.get("thought", False)
                    ).strip()
                    if texto_final:
                        return True, texto_final
                ultimo_erro = f"Resposta vazia do modelo {modelo}."
            else:
                try:
                    msg_err = resp.json().get("error", {}).get("message", resp.text[:240])
                except Exception:
                    msg_err = resp.text[:240]
                ultimo_erro = f"HTTP {resp.status_code} ({modelo}): {msg_err}"
                # Se a chave for inválida ou sem permissão, não adianta tentar outros modelos
                if resp.status_code in (400, 401, 403) and "key" in msg_err.lower():
                    return False, ultimo_erro
        except Exception as e:
            ultimo_erro = f"Erro de conexão ({modelo}): {e}"

    return False, ultimo_erro

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

def eh_arquivo_imagem(anexo: dict) -> bool:
    tipo = (anexo.get("tipo") or "").lower()
    nome = (anexo.get("nome") or "").lower()
    return "image" in tipo or nome.endswith((".png", ".jpg", ".jpeg", ".webp"))

def obter_mime_imagem(anexo: dict) -> str:
    tipo = (anexo.get("tipo") or "").lower()
    nome = (anexo.get("nome") or "").lower()
    if tipo.startswith("image/"):
        return tipo
    if nome.endswith(".png"):
        return "image/png"
    if nome.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"

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
    prompt_lower = prompt.lower().strip()

    # Só mistura o histórico anterior se a pergunta atual for uma continuação dependente de contexto
    termos_continuacao = ["ele", "ela", "dele", "dela", "disso", "acirrada", "quantos %", "porcentagem", "segundo turno", "disputou"]
    precisa_contexto = (
        bool(contexto_recente)
        and len(prompt_lower.split()) <= 10
        and any(tc in prompt_lower for tc in termos_continuacao)
    )

    # Coloca o prompt atual primeiro para nunca cortar o assunto principal
    texto_base = f"{prompt_lower} {contexto_recente.lower()}" if precisa_contexto else prompt_lower

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
        "bom dia", "boa tarde", "boa noite", "quem", "qual", "quais", "são", "sao",
        "como", "onde", "quando", "por que", "porque", "ficou", "foi",
        "ultimas", "últimas", "noticias", "notícias", "noticia", "notícia",
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

def autoavaliar_e_aprender_imagem(caminho_imagem: str, prompt_usuario: str, prompt_en: str, id_modelo: str, cliente=None, chave_gemini: str = "") -> str:
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

        critica = ""
        if chave_gemini:
            ok, texto_resp = chamar_gemini_nativo(
                chave=chave_gemini,
                modelos_candidatos=listar_candidatos_gemini("Padrão", chave_gemini, tem_imagem=True),
                system_text="",
                historico_mensagens=[],
                texto_usuario=prompt_critico,
                imagens=[{"mime": "image/png", "b64": b64_img}],
                temperature=0.4
            )
            if ok:
                critica = texto_resp
        else:
            cliente_uso = cliente or client_local
            res = cliente_uso.chat.completions.create(
                model=id_modelo,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_critico},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}}
                    ]
                }],
                timeout=25.0
            )
            critica = (res.choices[0].message.content or "").strip()

        if critica and "⚠️" not in critica:
            fato_aprendizado = f"Estilo e composição visual bem-sucedidos para '{prompt_usuario}': prompt usado ('{prompt_en}'). Análise: {critica}"
            salvar_memoria(fato_aprendizado, usuario="lume_vision_critic")

        return critica
    except Exception as e:
        print(f"⚠️ Crítico visual indisponível ou timeout: {e}")
        return ""

def gerar_imagem_local(prompt_usuario: str, id_modelo: str, imagem_referencia_bytes: bytes = None, mime_referencia: str = "image/png", cliente=None, chave_gemini: str = "") -> str:
    """Consulta memórias passadas, traduz o prompt (ou analisa imagem base), sintetiza a imagem e autoavalia."""
    cliente_uso = cliente or client_local
    os.makedirs("geracoes_lume", exist_ok=True)

    termo_limpo = limpar_texto_geracao(prompt_usuario)
    if not termo_limpo.strip() and not imagem_referencia_bytes:
        termo_limpo = "lume artificial intelligence neural sphere glowing"

    memoria_estilo = consultar_memoria(f"Estilo e composição visual para '{termo_limpo}'")
    contexto_memoria = f"\nUse estes aprendizados prévios de estilo: {memoria_estilo[:200]}" if memoria_estilo else ""

    prompt_en = termo_limpo

    try:
        if imagem_referencia_bytes and chave_gemini:
            b64_ref = codificar_imagem_base64(imagem_referencia_bytes)
            ok, conteudo = chamar_gemini_nativo(
                chave=chave_gemini,
                modelos_candidatos=listar_candidatos_gemini("Padrão", chave_gemini, tem_imagem=True),
                system_text="",
                historico_mensagens=[],
                texto_usuario=(
                    f"Analyze this reference image and the user directive: '{prompt_usuario}'. "
                    f"Extract its visual style, colors, lighting, and subject to create a concise, high quality "
                    f"English descriptive diffusion prompt that recreates/evolves this concept. "
                    f"Return ONLY the English prompt text."
                ),
                imagens=[{"mime": mime_referencia, "b64": b64_ref}],
                temperature=0.5
            )
            if ok and conteudo:
                prompt_en = conteudo.strip().replace('"', '').replace("'", "")
        else:
            conteudo_msg = (
                f"Translate the following subject into a concise English descriptive prompt for an image generator. "
                f"Include the subject and high quality visual tags (lighting, focus, angle). "
                f"Never include humans or portraits unless explicitly requested.{contexto_memoria}\n\n"
                f"Subject: '{termo_limpo}'\n\nPrompt:"
            )
            res = cliente_uso.chat.completions.create(
                model=id_modelo,
                messages=[{"role": "user", "content": conteudo_msg}],
                timeout=25.0
            )
            conteudo = (res.choices[0].message.content or "").strip().replace('"', '').replace("'", "")
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

    critica_visual = autoavaliar_e_aprender_imagem(
        caminho_salvo, termo_limpo, prompt_en, id_modelo, cliente=cliente_uso, chave_gemini=chave_gemini
    )
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

def obter_modelo_para_modo(modo: str, tem_imagem: bool = False, tem_chave: bool = None, chave: str = "") -> str:
    """Seleciona o modelo Gemini ativo descoberto dinamicamente ou usa o modelo gratuito padrão."""
    chave_uso = chave or obter_chave_gemini()
    usa_gemini = tem_chave if tem_chave is not None else bool(chave_uso)
    if not usa_gemini:
        return "openai"
    candidatos = listar_candidatos_gemini(modo, chave_uso, tem_imagem=tem_imagem)
    return candidatos[0] if candidatos else "gemini-2.5-flash"

def gerar_micro_resumo(contexto_historico: str, id_modelo: str, cliente=None, chave_gemini: str = "") -> str:
    """Gera um resumo semântico curto para arquivamento no ChromaDB."""
    try:
        if chave_gemini:
            ok, texto = chamar_gemini_nativo(
                chave=chave_gemini,
                modelos_candidatos=listar_candidatos_gemini("Rápido", chave_gemini),
                system_text="",
                historico_mensagens=[],
                texto_usuario=f"Sintetize em 1 frase curta o tópico desta conversa:\n\n{contexto_historico}\n\nResumo:",
                temperature=0.3
            )
            return texto if ok else ""

        cliente_uso = cliente or client_local
        res = cliente_uso.chat.completions.create(
            model=id_modelo,
            messages=[{"role": "user", "content": f"Sintetize em 1 frase curta o tópico desta conversa:\n\n{contexto_historico}\n\nResumo:"}],
            timeout=15.0
        )
        return (res.choices[0].message.content or "").strip()
    except Exception:
        return ""

# =============================================================
# 7. ⚡ EXECUTOR HÍBRIDO EM STREAMING (MULTIMODAL NATIVO)
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
        return f"✅ **Informação registrada com sucesso no meu banco vetorial (ChromaDB)!**\n\n> *{fato_gravado}*\n\nVocê já pode consultar ou editar esse registro abrindo a aba **🗄️ Memória (ChromaDB)**."

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

    return "O servidor gratuito atingiu o limite momentâneo de requisições. Aguarde alguns segundos ou verifique sua chave do Google AI Studio em '⚙️ Configurações IA' na barra lateral!"

def processar_resposta_ia_stream(prompt: str, conversa_id: str, modo: str, anexos: list = None, api_key_override: str = ""):
    """Executa o pipeline multimodal nativo com suporte a Sandbox Python, PDFs, Dados e Imagens."""
    if verificar_injecao_prompt(prompt):
        yield "⚠️ **Alerta de Segurança:** Diretriz de proteção ativada contra comandos maliciosos."
        return

    anexos = anexos or []
    tem_pdf = any("pdf" in (a.get("tipo") or "").lower() or (a.get("nome") or "").lower().endswith(".pdf") for a in anexos)
    tem_imagem = any(eh_arquivo_imagem(a) for a in anexos)
    tem_tabela = any((a.get("nome") or "").lower().endswith(('.csv', '.xls', '.xlsx')) for a in anexos)

    chave_ativa = api_key_override.strip() or obter_chave_gemini()
    cliente_ativo = criar_cliente_openai(chave_ativa)
    candidatos_raciocinio = listar_candidatos_gemini(modo, chave_ativa, tem_imagem=False) if chave_ativa else ["openai"]
    candidatos_visao = listar_candidatos_gemini(modo, chave_ativa, tem_imagem=True) if chave_ativa else ["openai"]
    id_modelo_raciocinio = candidatos_raciocinio[0]
    id_modelo_visao = candidatos_visao[0]

    mensagens_anteriores = carregar_mensagens(conversa_id)[-6:]
    ultimas_msgs_usuario = " ".join([t for a, t, _ in mensagens_anteriores[-4:] if a == "user"])

    decisao_llm = classificar_intencao_inteligente(
        prompt, id_modelo_raciocinio, tem_anexos=bool(anexos), historico_txt=ultimas_msgs_usuario
    )
    intencao = decisao_llm["intencao"]

    if intencao == "GERAR_IMAGEM" and not tem_pdf:
        anexo_img = next((a for a in anexos if eh_arquivo_imagem(a)), None)
        imagem_referencia_bytes = anexo_img["bytes"] if anexo_img else None
        mime_ref = obter_mime_imagem(anexo_img) if anexo_img else "image/png"
        yield "🎨 Analisando diretrizes e sintetizando imagem...\n\n"
        id_modelo_geracao = id_modelo_visao if imagem_referencia_bytes else id_modelo_raciocinio
        resultado_img = gerar_imagem_local(
            prompt,
            id_modelo_geracao,
            imagem_referencia_bytes=imagem_referencia_bytes,
            mime_referencia=mime_ref,
            cliente=cliente_ativo,
            chave_gemini=chave_ativa
        )
        yield resultado_img
        return

    contexto_anexos = ""
    imagens_multimodais = []

    # ---------------------------------------------------------
    # PASSO 1: EXTRAÇÃO DE DOCUMENTOS E PREPARAÇÃO MULTIMODAL
    # ---------------------------------------------------------
    if anexos:
        yield "🔍 Lendo documentos e extraindo contexto...\n\n"

        for arq in anexos:
            nome_arq = arq.get("nome", "")
            tipo_arq = (arq.get("tipo") or "").lower()

            if "pdf" in tipo_arq or nome_arq.lower().endswith(".pdf"):
                texto_pdf = extrair_texto_pdf(arq["bytes"])
                contexto_anexos += f"\n--- CONTEÚDO DO PDF ({nome_arq}) ---\n{texto_pdf[:4000]}...\n"

            elif nome_arq.lower().endswith(('.csv', '.xls', '.xlsx')):
                texto_tabela = extrair_conteudo_tabela(arq["bytes"], nome_arq)
                contexto_anexos += f"\n--- DADOS DA TABELA/PLANILHA ({nome_arq}) ---\n{texto_tabela}\n"

            elif eh_arquivo_imagem(arq):
                imagens_multimodais.append({
                    "nome": nome_arq,
                    "mime": obter_mime_imagem(arq),
                    "b64": codificar_imagem_base64(arq["bytes"])
                })

    if imagens_multimodais and not chave_ativa:
        yield "⚠️ **Chave API necessária para visão:** Nenhuma `GEMINI_API_KEY` ativa foi detectada. Cole sua chave em **⚙️ Configurações IA**, clique em **Salvar** (ou salve nos *Secrets* do Streamlit) e envie a imagem novamente."
        return

    # ---------------------------------------------------------
    # PASSO 2: RACIOCÍNIO CENTRAL E EXECUÇÃO (MULTIMODAL NATIVO)
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

    texto_sistema = "\n".join(partes_sistema)
    messages = [{"role": "system", "content": texto_sistema}]

    historico_pares = []
    for a, t, _ in mensagens_anteriores[-4:]:
        messages.append({"role": "user" if a == "user" else "assistant", "content": t[:1200]})
        historico_pares.append((a, t))

    if intencao == "SALVAR_MEMORIA":
        texto_usuario_final = f"{prompt}\n\n[Instrução Interna: Confirme ao usuário que a informação '{fato_gravado[:200]}' já foi gravada com sucesso no seu banco vetorial ChromaDB.]"
    elif contexto_web:
        texto_usuario_final = (
            f"Pergunta do usuário: {prompt}\n\n"
            f"[Instrução Interna: Responda de forma conversacional usando os dados das FONTES VERIFICADAS DA WEB acima. "
            f"Jamais adicione sobrenomes entre parênteses que não estejam nas fontes.]"
        )
    else:
        texto_usuario_final = prompt if prompt.strip() else "Analise e descreva os anexos enviados em detalhes."

    messages.append({"role": "user", "content": texto_usuario_final})

    temperatura_exec = 0.2 if intencao == "BUSCA_WEB" else 0.7
    resposta_acumulada = ""

    # Imagens vão direto pela API nativa do Gemini (mais estável e sem erros 404 da camada OpenAI-compat)
    if imagens_multimodais:
        ok, resultado_nativo = chamar_gemini_nativo(
            chave=chave_ativa,
            modelos_candidatos=candidatos_visao,
            system_text=texto_sistema,
            historico_mensagens=historico_pares,
            texto_usuario=texto_usuario_final,
            imagens=imagens_multimodais,
            temperature=temperatura_exec
        )
        if ok:
            resposta_acumulada = resultado_nativo
            yield resultado_nativo
        else:
            erro_limpo = resultado_nativo.replace(chave_ativa, "***") if chave_ativa else resultado_nativo
            yield f"⚠️ **Falha ao analisar a imagem na API do Gemini:** `{erro_limpo[:320]}`\n\nVerifique se a chave foi salva corretamente em **⚙️ Configurações IA** e se a cota da API está ativa."
            return
    else:
        try:
            stream = cliente_ativo.chat.completions.create(
                model=id_modelo_raciocinio,
                messages=messages,
                temperature=temperatura_exec,
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
            print(f"⚠️ API OpenAI-compat oscilou ({e}). Tentando rota nativa Gemini / contingência...")
            if chave_ativa:
                ok, resultado_nativo = chamar_gemini_nativo(
                    chave=chave_ativa,
                    modelos_candidatos=candidatos_raciocinio,
                    system_text=texto_sistema,
                    historico_mensagens=historico_pares,
                    texto_usuario=texto_usuario_final,
                    imagens=None,
                    temperature=temperatura_exec
                )
                if ok:
                    resposta_acumulada = resultado_nativo
                    CACHE_RESPOSTAS_TEMP[prompt.lower().strip()] = resposta_acumulada
                    yield resultado_nativo

            if not resposta_acumulada:
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
        resumo = gerar_micro_resumo(
            f"Usuário: {prompt}\nLume: {resposta_acumulada}",
            id_modelo_raciocinio,
            cliente=cliente_ativo,
            chave_gemini=chave_ativa
        )
        if resumo:
            salvar_ou_atualizar_resumo_chat(conversa_id, f"Chat {conversa_id}: {resumo}")
