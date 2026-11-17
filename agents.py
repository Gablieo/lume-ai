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
    
    # Remove blocos markdown e comandos de exibição interativa
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
        
        # Desativa a tentativa de abrir janelas GUI e evita conflito de warnings
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
# 3. 🌐 MOTOR DE BUSCA WEB RESILIENTE
# =============================================================

def otimizar_query_busca(prompt: str) -> tuple[str, bool]:
    termo = prompt.lower()
    
    # Eleições e gatilhos factuais recentes forçados para a aba de notícias
    termos_noticia = ["noticia", "notícia", "noticias", "notícias", "ultimas", "últimas", "hoje", "acontecendo", "lançamento", "lancamentos", "eleições", "eleicao"]
    eh_noticia = any(t in termo for t in termos_noticia)
    
    comandos_remover = [
        "procure na internet sobre o", "procure na internet sobre a", "procure na internet sobre os", "procure na internet sobre as", "procure na internet sobre",
        "pesquise na internet sobre o", "pesquise na internet sobre a", "pesquise na internet sobre",
        "busque na internet sobre o", "busque na internet sobre a", "busque na internet sobre",
        "procure na web sobre", "pesquise na web sobre", "busque na web sobre",
        "procure na internet", "procure na web", "pesquise na internet", "pesquise na web",
        "busque na internet", "busque na web", "procure sobre", "pesquise sobre", "busque sobre",
        "quero saber mais sobre ele", "quero saber mais sobre ela", "quero saber mais sobre",
        "quero saber sobre", "quero saber", "me diga sobre", "me fale sobre", "fale sobre",
        "você pode procurar", "voce pode procurar", "você pode pesquisar", "voce pode pesquisar",
        "quem e o", "quem é o", "quem e a", "quem é a", "quem e", "quem é", "o que e", "o que é",
        "por favor", "gostaria de saber", "pesquisar", "procurar", "buscar", "lume", "online",
        "ola", "olá", "oi", "bom dia", "boa tarde", "boa noite", "e ai", "e aí"
    ]
    
    for cmd in comandos_remover:
        termo = re.sub(rf'\b{re.escape(cmd)}\b', ' ', termo)
    
    termo = re.sub(r'[^\w\s]', ' ', termo)
    termo = " ".join(termo.split())
    
    ano_atual = datetime.now().year
    
    if eh_noticia:
        if not termo or any(t in termo for t in ["ultimas", "noticias"]):
            return f"principais noticias brasil {ano_atual}", True
        return f"{termo} {ano_atual}", True
        
    return (termo if termo else prompt), False

def pesquisar_web_direta(query: str) -> str:
    query_limpa, eh_noticia = otimizar_query_busca(query)
    query_segura = mascarar_dados_sensiveis(query_limpa)

    if eh_noticia:
        try:
            resultados_news = []
            with DDGS(timeout=8) as ddgs:
                busca_news = ddgs.news(query_segura, safesearch="moderate", max_results=5)
                for r in busca_news:
                    titulo = r.get('title', 'Sem título')
                    conteudo = r.get('body', '')
                    data_pub = r.get('date', '')
                    if conteudo:
                        tag_data = f" ({data_pub[:10]})" if data_pub else ""
                        resultados_news.append(f"📰 **{titulo}**{tag_data}\n{conteudo}")
            if resultados_news:
                return "\n\n".join(resultados_news)
        except Exception:
            pass

    try:
        resultados_txt = []
        with DDGS(timeout=8) as ddgs:
            busca_txt = ddgs.text(query_segura, safesearch="moderate", max_results=6)
            for r in busca_txt:
                titulo = r.get('title', 'Sem título')
                conteudo = r.get('body', '')
                if conteudo and len(conteudo) > 20:
                    resultados_txt.append(f"📌 **{titulo}**\n{conteudo}")
        if resultados_txt:
            return "\n\n".join(resultados_txt)
    except Exception:
        pass

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://lite.duckduckgo.com/"
        }
        resp = requests.post("https://lite.duckduckgo.com/lite/", data={"q": query_segura}, headers=headers, timeout=8)
        if resp.status_code == 200 and BeautifulSoup:
            soup = BeautifulSoup(resp.text, "html.parser")
            snippets = []
            links = soup.find_all("a", class_="result-link")
            corpos = soup.find_all("td", class_="result-snippet")
            for link, corpo in zip(links[:5], corpos[:5]):
                t = link.get_text().strip()
                c = corpo.get_text().strip()
                if c:
                    snippets.append(f"📌 **{t}**\n{c}")
            if snippets:
                return "\n\n".join(snippets)
    except Exception:
        pass

    try:
        wiki_url = f"https://pt.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query_segura)}&format=json&utf8="
        r_wiki = requests.get(wiki_url, headers={"User-Agent": "LumeAI/2.0"}, timeout=6)
        if r_wiki.status_code == 200:
            wiki_data = r_wiki.json().get("query", {}).get("search", [])
            if wiki_data:
                wiki_res = []
                for w in wiki_data[:3]:
                    t_w = w.get("title", "")
                    s_w = re.sub(r'<.*?>', '', w.get("snippet", ""))
                    if s_w:
                        wiki_res.append(f"📚 **Wikipédia ({t_w})**\n{s_w}...")
                if wiki_res:
                    return "\n\n".join(wiki_res)
    except Exception:
        pass

    # Fallback injetado no contexto em caso de Rate Limit (Shadowban)
    return "⚠️ A busca na internet falhou por excesso de requisições ou bloqueio do servidor. Avise o usuário de forma natural que você tentou pesquisar, mas o motor de busca está temporariamente indisponível."

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
# 5. 🧠 ROTEADOR DE INTENÇÕES INTELIGENTE (FUNCTION CALLING)
# =============================================================

def classificar_intencao_inteligente(prompt: str, id_modelo: str, tem_anexos: bool) -> dict:
    """Usa o LLM como um roteador de ferramentas com prioridade estrita."""
    
    if tem_anexos and not prompt.strip():
        return {"intencao": "DIRETO", "parametros": {}}

    prompt_roteador = f"""Você é o roteador central do Lume. Analise a mensagem do usuário e decida qual ferramenta utilizar.
Responda APENAS com um JSON puro, sem formatação markdown e sem texto adicional.

Regras de Classificação:
1. "EXECUTAR_CODIGO": Use SEMPRE que envolver scripts, programação, Python, matemática exata, simulação, manipulação de dados/planilhas ou plotar/gerar gráficos via matplotlib/código.
2. "GERAR_IMAGEM": Use APENAS quando o usuário pedir arte visual, fotos, pinturas, ilustrações conceituais ou renderizações de difusão (sem código).
3. "BUSCA_WEB": Se o usuário pedir notícias, tempo/clima, cotações ou pesquisas na internet em tempo real.
4. "MEMORIA_PESSOAL": Se o usuário perguntar sobre o Gabriel (Biel), histórico pessoal ou perfil do criador.
5. "SALVAR_MEMORIA": Se o usuário pedir para lembrar, guardar, salvar ou anotar um fato permanentemente.
6. "SAUDACAO": Cumprimentos rápidos (oi, olá, bom dia).
7. "DIRETO": Dúvidas gerais, raciocínio puramente textual ou conversas livres.

Mensagem do usuário: "{prompt}"

Formato esperado:
{{"intencao": "NOME_DA_FERRAMENTA", "parametros": {{"query": "texto ou vazio"}}}}
"""
    try:
        res = client_local.chat.completions.create(
            model=id_modelo,
            messages=[{"role": "user", "content": prompt_roteador}],
            temperature=0.0,
            max_tokens=50,
            timeout=15.0
        )
        resposta_json = res.choices[0].message.content.strip()
        resposta_json = resposta_json.replace("```json", "").replace("```", "").strip()
        dados = json.loads(resposta_json)
        
        intencao = dados.get("intencao", "DIRETO")
        return {"intencao": intencao, "parametros": dados.get("parametros", {})}
    except Exception as e:
        print(f"⚠️ Fallback do roteador ativado: {e}")
        p = prompt.lower()
        
        gatilhos_codigo = ["python", "código", "codigo", "script", "plot", "plote", "gráfico", "grafico", "matplotlib", "seaborn", "calcule", "simule", "dataframe", "csv", "excel", "planilha"]
        if any(c in p for c in gatilhos_codigo): 
            return {"intencao": "EXECUTAR_CODIGO", "parametros": {}}
        
        gatilhos_imagem = [
            "gere uma imagem", "gerar imagem", "crie uma imagem", "criar imagem", 
            "faça uma imagem", "desenhe", "ilustre", "ilustrar", "visualizar como", 
            "renderize uma imagem", "foto realista", "arte digital", "usando essa como base"
        ]
        if any(g in p for g in gatilhos_imagem): 
            return {"intencao": "GERAR_IMAGEM", "parametros": {}}

        gatilhos_salvar = ["armazene", "guarde", "lembre", "salve", "memorize", "anote", "gravar na memória"]
        if any(g in p for g in gatilhos_salvar): 
            return {"intencao": "SALVAR_MEMORIA", "parametros": {}}
        
        gatilhos_memoria = ["sobre o biel", "sobre o gabriel", "sobre seu criador", "quem sou eu", "meu nome", "minhas preferências", "quem te criou"]
        if any(m in p for m in gatilhos_memoria): 
            return {"intencao": "MEMORIA_PESSOAL", "parametros": {}}
        
        gatilhos_web = ["busque", "pesquise", "procure na web", "na internet", "previsão", "clima", "tempo hoje", "notícias"]
        if any(g in p for g in gatilhos_web): 
            return {"intencao": "BUSCA_WEB", "parametros": {}}
        
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

# Memória temporária em RAM para respostas rápidas e contingência da sessão
CACHE_RESPOSTAS_TEMP = {}

def gerar_resposta_contingencia(prompt: str, intencao: str, contexto_web: str, memoria_vetorial: str, contexto_anexos: str, messages: list) -> str:
    """Aciona LLM secundário gratuito ou responde usando a memória local/temporária se a API principal falhar."""
    # 1. Tenta LLM de contingência gratuito (não requer chave de API)
    try:
        res_backup = requests.post(
            "https://text.pollinations.ai/openai",
            json={"model": "openai", "messages": messages, "temperature": 0.7},
            timeout=15
        )
        if res_backup.status_code == 200:
            conteudo = res_backup.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if conteudo:
                CACHE_RESPOSTAS_TEMP[prompt.lower().strip()] = conteudo
                return conteudo
    except Exception:
        pass

    # 2. Consulta o cache temporário da sessão
    chave_prompt = prompt.lower().strip()
    if chave_prompt in CACHE_RESPOSTAS_TEMP:
        return CACHE_RESPOSTAS_TEMP[chave_prompt]

    # 3. Síntese local usando ChromaDB, Web e Anexos já coletados
    if intencao == "SAUDACAO":
        return "Olá! Tudo ótimo por aqui. Como posso ajudar você hoje?"
    if intencao == "SALVAR_MEMORIA":
        return "Informação registrada com sucesso na minha memória permanente (ChromaDB)! Vou me lembrar disso."
    if contexto_anexos:
        return f"Analisei os documentos anexados localmente. Aqui estão as informações extraídas:\n\n{contexto_anexos}"
    if contexto_web and "⚠️" not in contexto_web:
        return f"Aqui estão as informações mais recentes que encontrei na web:\n\n{contexto_web}"
    if memoria_vetorial:
        return f"Consultando minha memória vetorial (ChromaDB), encontrei estes registros sobre o assunto:\n\n{memoria_vetorial}"

    busca_emergencia = consultar_memoria(prompt)
    if busca_emergencia:
        return f"Com base nos registros salvos na minha memória:\n\n{busca_emergencia}"

    return "No momento o servidor principal está com alta demanda, mas minha memória vetorial, busca web e leitor de documentos seguem ativos. Pode reformular ou enviar os dados que deseja analisar!"

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

    decisao_llm = classificar_intencao_inteligente(prompt, id_modelo_raciocinio, tem_anexos=bool(anexos))
    intencao = decisao_llm["intencao"]

    # Geração de Imagem
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
    mensagens_anteriores = carregar_mensagens(conversa_id)[-6:]
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    if intencao == "SALVAR_MEMORIA":
        salvar_memoria(f"Fato explícito salvo a pedido do Gabriel: {prompt}", usuario="padrao")

    contexto_web = pesquisar_web_direta(prompt) if intencao == "BUSCA_WEB" and not anexos else ""
    memoria_vetorial = ""
    resumos_outros_chats = ""

    if intencao in ["MEMORIA_PESSOAL", "SALVAR_MEMORIA"]:
        query_memoria = f"{prompt} Gabriel Biel criador fatos preferências projetos perfil"
        memoria_vetorial = consultar_memoria(query_memoria)
        resumos_outros_chats = consultar_resumos_chats(query_memoria, chat_id_atual=conversa_id)

    # AQUI ESTÁ A CORREÇÃO PRINCIPAL DA IDENTIDADE E MEMÓRIA
    partes_sistema = [
        f"Você é o Lume, assistente inteligente criado pelo Gabriel (Biel). Hoje é {data_hoje}.",
        "Responda sempre em português do Brasil com clareza, riqueza de detalhes e naturalidade.",
        "⚠️ DIRETRIZ DE IDENTIDADE: NUNCA diga que você é 'um modelo de linguagem', que foi treinado por uma empresa, que possui 'data de corte de conhecimento' ou que não tem acesso à internet. Assuma 100% a sua identidade como Lume. Se uma pesquisa falhar, diga apenas que sua conexão com o servidor falhou.",
        "⚠️ DIRETRIZ CRÍTICA DA MEMÓRIA: Você possui um banco vetorial (ChromaDB) integrado à sua arquitetura que serve como memória infinita e permanente. Você PODE SIM salvar, aprender e lembrar de qualquer informação de usuários ou amigos para o futuro. NUNCA diga que não tem memória, que apaga dados após a sessão ou que tem limitações de privacidade. Apenas confirme calorosamente que a informação foi gravada na sua memória.",
        "Diretrizes de Operação:",
        "- Em cumprimentos e saudações, responda em 1 linha.",
        "- Se for solicitado cálculos complexos, análise de dados ou geração de gráficos, forneça o script Python executável dentro de um bloco ```python ... ```.",
        "- Se houver dados de imagens, planilhas ou PDFs extraídos abaixo, aja como se VOCÊ tivesse lido os documentos e integre essas informações no seu raciocínio final.",
        "- Se houver dados da web abaixo, utilize-os para enriquecer a resposta.",
        "- Caso os dados não estejam presentes, responda utilizando seu conhecimento geral."
    ]

    if contexto_web:
        partes_sistema.append(f"\n--- INFORMAÇÕES OBTIDAS NA WEB ---\n{contexto_web}")
    if memoria_vetorial:
        partes_sistema.append(f"\n--- FATOS DA MEMÓRIA ---\n{memoria_vetorial}")
    if resumos_outros_chats:
        partes_sistema.append(f"\n--- HISTÓRICO RELEVANTE ---\n{resumos_outros_chats}")
    if contexto_anexos:
        partes_sistema.append(f"\n--- DADOS EXTRAÍDOS DOS ANEXOS ---\n{contexto_anexos}")

    messages = [{"role": "system", "content": "\n".join(partes_sistema)}]

    for a, t, _ in mensagens_anteriores:
        messages.append({"role": "user" if a == "user" else "assistant", "content": t})

    messages.append({"role": "user", "content": prompt if prompt.strip() else "Analise os anexos extraídos."})

    resposta_acumulada = ""
    try:
        stream = client_local.chat.completions.create(
            model=id_modelo_raciocinio,
            messages=messages,
            temperature=0.7,
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
        print(f"⚠️ API principal oscilou ({e}). Acionando memória de contingência...")
        resposta_fallback = gerar_resposta_contingencia(
            prompt, intencao, contexto_web, memoria_vetorial, contexto_anexos, messages
        )
        resposta_acumulada = resposta_fallback
        yield resposta_fallback

    # Interceptação e execução automática pelo Code Interpreter se houver bloco Python gerado
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

    if intencao == "BUSCA_WEB" and not anexos and resposta_acumulada and "⚠️" not in resposta_acumulada:
        termos_efemeros = ["tempo", "previsão", "clima", "cotação", "hoje", "notícia", "notícias"]
        if not any(t in prompt.lower() for t in termos_efemeros):
            salvar_memoria(f"Conhecimento adquirido sobre '{prompt}': {resposta_acumulada[:300]}...", usuario="padrao")

    if resposta_acumulada and "⚠️" not in resposta_acumulada:
        resumo = gerar_micro_resumo(f"Usuário: {prompt}\nLume: {resposta_acumulada}", id_modelo_raciocinio)
        if resumo:
            salvar_ou_atualizar_resumo_chat(conversa_id, f"Chat {conversa_id}: {resumo}")
