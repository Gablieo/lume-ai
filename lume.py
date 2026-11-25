import os
import json
import time
import re
import streamlit as st
from fpdf import FPDF

from database import (
    carregar_conversas, carregar_mensagens, criar_nova_conversa, 
    salvar_mensagem, atualizar_titulo_conversa, deletar_conversa
)
from agents import processar_resposta_ia_stream
from vector_db import (
    salvar_memoria, consultar_memoria, listar_todas_memorias,
    deletar_memoria_por_id, atualizar_memoria_por_id, deletar_resumo_chat,
    collection_resumos
)

st.set_page_config(page_title="Lume - Assistente de IA", page_icon="✦", layout="wide")

# =============================================================
# ESTADO INICIAL DA SESSÃO
# =============================================================

if "chat_atual_id" not in st.session_state:
    st.session_state.chat_atual_id = None

if "GEMINI_API_KEY" not in st.session_state:
    st.session_state["GEMINI_API_KEY"] = ""

if "aba_ativa" not in st.session_state:
    st.session_state.aba_ativa = "conversa"  # "conversa" ou "memoria"

if "modo_ui" not in st.session_state:
    st.session_state.modo_ui = "Padrão"  # "Rápido", "Padrão", "Profundo"

MAPA_MODOS = {
    "Rápido": {
        "interno": "⚡ Rápido",
        "badge": "Rápido",
        "placeholder": "Pergunte ao Lume em modo rápido...",
        "desc": "Respostas ágeis e diretas para tarefas cotidianas."
    },
    "Padrão": {
        "interno": "⚖️ Equilibrado",
        "badge": "Equilibrado",
        "placeholder": "Pergunte ao Lume em modo equilibrado...",
        "desc": "Combina clareza, profundidade e exemplos contextualizados."
    },
    "Profundo": {
        "interno": "🧠 Pensamento Profundo",
        "badge": "Profundo",
        "placeholder": "Pergunte ao Lume em modo profundo...",
        "desc": "Raciocínio analítico avançado para problemas complexos."
    }
}

# =============================================================
# FUNÇÕES DE TRATAMENTO DE TEXTO, IMAGEM E PDF
# =============================================================

def limpar_vazamentos_llm(texto: str) -> str:
    if not texto:
        return ""
    padroes_vazamento = [
        r"^#*\s*i will then.*$",
        r"^#*\s*don't forget.*$",
        r"^#*\s*thought:?.*$",
        r"^#*\s*action:?.*$",
        r"^#*\s*final answer:?.*$",
        r"^#*\s*final_answer:?.*$",
        r"^agora que você está pronto para sua resposta final.*$",
        r"^para responder à sua pergunta original.*$"
    ]
    linhas = [
        l for l in texto.split("\n")
        if not any(re.match(p, l.strip(), re.IGNORECASE) for p in padroes_vazamento)
    ]
    resultado = "\n".join(linhas).strip()
    return re.sub(r'^(FINAL ANSWER|FINAL_ANSWER|Thought:|Action:)\s*', '', resultado, flags=re.IGNORECASE).strip()

def renderizar_conteudo_assistente(texto: str):
    texto_limpo = limpar_vazamentos_llm(texto)
    if "[IMAGEM_GERADA:" in texto_limpo:
        partes = texto_limpo.split("[IMAGEM_GERADA:")
        texto_antes = partes[0].strip()
        resto = partes[1].split("]", 1)
        caminho_img = resto[0].strip()
        texto_depois = resto[1].strip() if len(resto) > 1 else ""
        if texto_antes:
            st.markdown(texto_antes)
        if os.path.exists(caminho_img) or caminho_img.startswith("http"):
            st.image(caminho_img, caption="Gráfico / Imagem gerada pelo Lume", width=460)
        if texto_depois:
            st.markdown(texto_depois)
    else:
        st.markdown(texto_limpo)

def sanitizar_texto_latin1(texto: str) -> str:
    texto = limpar_vazamentos_llm(texto)
    texto = texto.replace("\t", "    ").replace("\xa0", " ").replace("•", "-")
    texto = re.sub(r'\*\*(.*?)\*\*', r'\1', texto)
    texto = re.sub(r'\*(.*?)\*', r'\1', texto)
    return re.sub(r'[^\x00-\xFF]', '', texto)

def gerar_pdf_bytes(texto: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", size=11)
    for linha in sanitizar_texto_latin1(texto).split("\n"):
        linha_str = linha.strip()
        if not linha_str:
            pdf.ln(3)
            continue
        linha_safe = linha_str.encode('latin-1', 'replace').decode('latin-1')
        try:
            if linha_str.startswith("#"):
                pdf.set_font("Helvetica", "B", 13)
                pdf.multi_cell(0, 7, text=linha_safe.lstrip("#").strip())
                pdf.ln(2)
            else:
                pdf.set_font("Helvetica", "", 11)
                pdf.multi_cell(0, 6, text=linha_safe)
        except Exception:
            continue
    return bytes(pdf.output())

# =============================================================
# CSS FIEL AO PROTÓTIPO DO GOOGLE AI STUDIO (OBSIDIAN DARK)
# =============================================================

st.markdown("""
<style>
    /* Fundo principal Obsidian */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background-color: #09090B !important;
        color: #F4F4F5 !important;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    [data-testid="stHeader"] {
        height: 0px !important;
        background: transparent !important;
    }

    .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 2rem !important;
        max-width: 1100px !important;
    }

    [data-testid="stBottom"], [data-testid="stBottom"] > div, div[data-testid="stBottomBlockContainer"] {
        background-color: #09090B !important;
        background: #09090B !important;
        border: none !important;
    }

    /* Sidebar Obsidian */
    [data-testid="stSidebar"] {
        background-color: #0C0C0E !important;
        border-right: 1px solid #1F1F23 !important;
    }

    /* Botões padrão na Sidebar e Topo */
    .stButton > button {
        background-color: #121215 !important;
        color: #D4D4D8 !important;
        border: 1px solid #27272A !important;
        border-radius: 10px !important;
        font-size: 13px !important;
        font-weight: 500 !important;
        padding: 6px 12px !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button:hover {
        background-color: #1F1F23 !important;
        color: #FFFFFF !important;
        border-color: #3F3F46 !important;
    }

    /* Botão primário (usado para o modo selecionado ou destaque branco) */
    .stButton > button[kind="primary"] {
        background-color: #27272A !important;
        color: #FFFFFF !important;
        border: 1px solid #52525B !important;
    }

    /* Popover estilo barra inferior e topo */
    [data-testid="stPopover"] > div > button {
        background-color: #121215 !important;
        color: #D4D4D8 !important;
        border: 1px solid #27272A !important;
        border-radius: 10px !important;
        font-size: 13px !important;
    }

    /* Caixa de Chat Flutuante estilo AI Studio */
    [data-testid="stChatInput"] {
        max-width: 780px !important;
        margin: 0 auto !important;
        padding-bottom: 6px !important;
    }
    [data-testid="stChatInput"] > div {
        background-color: #111114 !important;
        border: 1px solid #27272A !important;
        border-radius: 16px !important;
        padding: 8px 14px !important;
        box-shadow: 0 10px 30px rgba(0,0,0,0.6) !important;
    }
    [data-testid="stChatInput"] > div:focus-within {
        border-color: #3F3F46 !important;
        background-color: #141418 !important;
    }
    [data-testid="stChatInput"] textarea {
        color: #F4F4F5 !important;
        font-size: 14px !important;
    }
    [data-testid="stChatInput"] textarea::placeholder {
        color: #71717A !important;
    }
    [data-testid="stChatInput"] button {
        background-color: #27272A !important;
        color: #F4F4F5 !important;
        border-radius: 8px !important;
        width: 32px !important;
        height: 32px !important;
    }

    /* Animação de processamento */
    .lume-dot {
        width: 6px; height: 6px; background-color: #71717A;
        border-radius: 50%; display: inline-block;
        animation: lume-bounce 1.4s infinite ease-in-out both;
    }
    .lume-dot-1 { animation-delay: -0.32s; }
    .lume-dot-2 { animation-delay: -0.16s; }
    .lume-dot-3 { animation-delay: 0s; }
    @keyframes lume-bounce {
        0%, 80%, 100% { transform: scale(0.3); opacity: 0.3; }
        40% { transform: scale(1.1); opacity: 1; background-color: #F4F4F5; }
    }

    #MainMenu, footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

info_modo = MAPA_MODOS[st.session_state.modo_ui]
modo_raciocinio = info_modo["interno"]

# =============================================================
# BARRA LATERAL (IDÊNTICA AO PROTÓTIPO AI STUDIO)
# =============================================================

with st.sidebar:
    # Cabeçalho com ícone "L" + Lume Assistente
    st.markdown("""
        <div style="display: flex; align-items: center; gap: 12px; padding: 4px 4px 16px 4px;">
            <div style="width: 34px; height: 34px; border-radius: 10px; background-color: #18181B; border: 1px solid #27272A; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 14px; color: #F4F4F5;">
                L
            </div>
            <div>
                <div style="font-weight: 600; font-size: 14px; color: #F4F4F5; line-height: 1.2;">Lume</div>
                <div style="font-size: 11px; color: #71717A;">Assistente</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    if st.button("＋  Nova conversa", use_container_width=True, key="side_new_chat"):
        st.session_state.chat_atual_id = None
        st.session_state.aba_ativa = "conversa"
        st.rerun()

    # Seletor de Modo em 3 Botões (Rápido | Padrão | Profundo)
    st.markdown("<div style='font-size: 10px; font-weight: 600; letter-spacing: 0.08em; color: #71717A; margin: 18px 0 6px 2px;'>MODO DE RACIOCÍNIO</div>", unsafe_allow_html=True)
    c_m1, c_m2, c_m3 = st.columns(3)
    with c_m1:
        if st.button("⚡ Rápido", use_container_width=True, type="primary" if st.session_state.modo_ui == "Rápido" else "secondary"):
            st.session_state.modo_ui = "Rápido"
            st.rerun()
    with c_m2:
        if st.button("⚖️ Padrão", use_container_width=True, type="primary" if st.session_state.modo_ui == "Padrão" else "secondary"):
            st.session_state.modo_ui = "Padrão"
            st.rerun()
    with c_m3:
        if st.button("🧠 Profundo", use_container_width=True, type="primary" if st.session_state.modo_ui == "Profundo" else "secondary"):
            st.session_state.modo_ui = "Profundo"
            st.rerun()

    st.markdown("<hr style='border-color: #1F1F23; margin: 14px 0;'>", unsafe_allow_html=True)

    # Navegação Lateral: Conversas vs Memória (ChromaDB) RAG
    if st.button("💬  Conversas", use_container_width=True, type="primary" if st.session_state.aba_ativa == "conversa" else "secondary"):
        st.session_state.aba_ativa = "conversa"
        st.rerun()

    if st.button("🗄️  Memória (ChromaDB)   [RAG]", use_container_width=True, type="primary" if st.session_state.aba_ativa == "memoria" else "secondary"):
        st.session_state.aba_ativa = "memoria"
        st.rerun()

    st.markdown("<hr style='border-color: #1F1F23; margin: 14px 0;'>", unsafe_allow_html=True)
    st.markdown("<div style='font-size: 10px; font-weight: 600; letter-spacing: 0.08em; color: #71717A; margin-bottom: 8px;'>CONVERSAS RECENTES</div>", unsafe_allow_html=True)

    conversas_existentes = carregar_conversas()
    if not conversas_existentes:
        st.markdown("""
            <div style="text-align: center; padding: 36px 10px; color: #52525B; font-size: 12px;">
                <div style="font-size: 18px; margin-bottom: 6px; opacity: 0.5;">💬</div>
                Nenhuma conversa salva ainda.
            </div>
        """, unsafe_allow_html=True)
    else:
        for chat_id, titulo in conversas_existentes:
            label = f"💬 {titulo[:18]}..." if len(titulo) > 18 else f"💬 {titulo}"
            col_c1, col_c2 = st.columns([0.82, 0.18])
            with col_c1:
                if st.button(label, key=f"chat_{chat_id}", use_container_width=True):
                    st.session_state.chat_atual_id = chat_id
                    st.session_state.aba_ativa = "conversa"
                    st.rerun()
            with col_c2:
                if st.button("🗑️", key=f"del_{chat_id}"):
                    deletar_conversa(chat_id)
                    deletar_resumo_chat(chat_id)
                    if st.session_state.chat_atual_id == chat_id:
                        st.session_state.chat_atual_id = None
                    st.rerun()

    st.markdown("<hr style='border-color: #1F1F23; margin: 18px 0 10px 0;'>", unsafe_allow_html=True)

    # Rodapé da Sidebar: Configurações IA com ponto verde
    with st.popover("⚙️  Configurações IA  🟢", use_container_width=True):
        st.markdown("<h4 style='color: #F4F4F5; font-size: 14px; margin-bottom: 8px;'>⚡ Motor de IA & API Key</h4>", unsafe_allow_html=True)
        nova_chave = st.text_input(
            "GEMINI_API_KEY (Google AI Studio):",
            value=st.session_state.get("GEMINI_API_KEY", ""),
            type="password",
            placeholder="AIzaSy... (ou vazio para modo local/free)"
        )
        c_api1, c_api2 = st.columns(2)
        with c_api1:
            if st.button("💾 Salvar", use_container_width=True, key="save_api_side"):
                st.session_state["GEMINI_API_KEY"] = nova_chave.strip()
                st.success("Configuração aplicada!")
                st.rerun()
        with c_api2:
            if st.button("🧹 Limpar", use_container_width=True, key="clear_api_side"):
                st.session_state["GEMINI_API_KEY"] = ""
                st.rerun()
        st.caption("🔒 Isolada na sua sessão atual do navegador.")

# =============================================================
# BARRA SUPERIOR (TOP HEADER DO AI STUDIO)
# =============================================================

mensagens_salvas = carregar_mensagens(st.session_state.chat_atual_id) if st.session_state.chat_atual_id is not None else []
titulo_topo = "Nova Conversa"
if st.session_state.chat_atual_id is not None:
    for cid, t in conversas_existentes:
        if cid == st.session_state.chat_atual_id:
            titulo_topo = t[:28]
            break

col_top_esq, col_top_centro, col_top_dir = st.columns([0.36, 0.34, 0.30])

with col_top_esq:
    st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 10px; padding-top: 6px;">
            <span style="color: #71717A; font-size: 15px;">☰</span>
            <span style="font-weight: 600; font-size: 14px; color: #F4F4F5;">{titulo_topo}</span>
            <span style="background-color: #18181B; border: 1px solid #27272A; color: #A1A1AA; font-size: 11px; padding: 2px 8px; border-radius: 6px;">
                {info_modo['badge']}
            </span>
        </div>
    """, unsafe_allow_html=True)

with col_top_centro:
    ct1, ct2 = st.columns(2)
    with ct1:
        if st.button("💬 Conversa", use_container_width=True, key="top_tab_conv", type="primary" if st.session_state.aba_ativa == "conversa" else "secondary"):
            st.session_state.aba_ativa = "conversa"
            st.rerun()
    with ct2:
        if st.button("🗄️ Memória (ChromaDB)", use_container_width=True, key="top_tab_mem", type="primary" if st.session_state.aba_ativa == "memoria" else "secondary"):
            st.session_state.aba_ativa = "memoria"
            st.rerun()

with col_top_dir:
    cd1, cd2 = st.columns([0.45, 0.55])
    with cd1:
        if mensagens_salvas:
            texto_export = "\n\n".join([f"{'Você' if a=='user' else 'Lume'}: {t}" for a, t, _ in mensagens_salvas])
            st.download_button(
                "📥 Exportar",
                data=gerar_pdf_bytes(texto_export),
                file_name=f"conversa_lume_{st.session_state.chat_atual_id}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
        else:
            st.button("📥 Exportar", disabled=True, use_container_width=True)
    with cd2:
        if st.button("＋ Nova conversa", key="top_new_chat", use_container_width=True, type="primary"):
            st.session_state.chat_atual_id = None
            st.session_state.aba_ativa = "conversa"
            st.rerun()

st.markdown("<hr style='border-color: #18181B; margin: 10px 0 24px 0;'>", unsafe_allow_html=True)

# =============================================================
# ABA 1: PAINEL DE MEMÓRIA VETORIAL (CHROMADB RAG)
# =============================================================

if st.session_state.aba_ativa == "memoria":
    ids_fatos, docs_fatos = listar_todas_memorias()
    try:
        dados_r = collection_resumos.get()
        ids_r = dados_r.get("ids", [])
        docs_r = dados_r.get("documents", [])
    except Exception:
        ids_r, docs_r = [], []

    st.markdown("""
        <div style="margin-bottom: 18px;">
            <h3 style="color: #F4F4F5; font-size: 18px; font-weight: 600; margin-bottom: 4px;">🗄️ Memória Episódica Persistente (ChromaDB)</h3>
            <p style="color: #71717A; font-size: 13px; margin: 0;">Gerenciamento de fatos de longo prazo, embeddings vetoriais e resumos semânticos via RAG.</p>
        </div>
    """, unsafe_allow_html=True)

    # Cards de Métricas do ChromaDB
    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown(f"""
            <div style="background-color: #111114; border: 1px solid #27272A; border-radius: 12px; padding: 14px 18px;">
                <div style="color: #71717A; font-size: 11px; text-transform: uppercase;">Fatos Indexados</div>
                <div style="color: #F4F4F5; font-size: 22px; font-weight: 600; margin-top: 4px;">{len(ids_fatos)}</div>
            </div>
        """, unsafe_allow_html=True)
    with m2:
        st.markdown(f"""
            <div style="background-color: #111114; border: 1px solid #27272A; border-radius: 12px; padding: 14px 18px;">
                <div style="color: #71717A; font-size: 11px; text-transform: uppercase;">Resumos de Sessões</div>
                <div style="color: #F4F4F5; font-size: 22px; font-weight: 600; margin-top: 4px;">{len(ids_r)}</div>
            </div>
        """, unsafe_allow_html=True)
    with m3:
        st.markdown("""
            <div style="background-color: #111114; border: 1px solid #27272A; border-radius: 12px; padding: 14px 18px;">
                <div style="color: #71717A; font-size: 11px; text-transform: uppercase;">Dimensão do Espaço Latente</div>
                <div style="color: #10B981; font-size: 22px; font-weight: 600; margin-top: 4px;">384D (Cosine)</div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    col_add, col_search = st.columns(2)

    with col_add:
        st.markdown("##### ➕ Gravar Novo Fato na Memória")
        novo_fato = st.text_input("Digite um fato para gravar:", placeholder="Ex: O Gabriel desenvolveu o Lume para Deep Learning...", label_visibility="collapsed")
        if st.button("💾 Indexar no ChromaDB", use_container_width=True):
            if novo_fato.strip():
                salvar_memoria(novo_fato.strip())
                st.success("Fato indexado com sucesso!")
                st.rerun()

    with col_search:
        st.markdown("##### 🔍 Busca Semântica (RAG)")
        termo_busca = st.text_input("Pesquisar por similaridade:", placeholder="Digite um termo para consultar os vetores...", label_visibility="collapsed")
        if termo_busca:
            res_busca = consultar_memoria(termo_busca)
            st.info(res_busca if res_busca else "Nenhum vetor compatível encontrado.")

    st.markdown("<hr style='border-color: #1F1F23; margin: 20px 0;'>", unsafe_allow_html=True)
    tab_fatos, tab_resumos = st.tabs(["📋 Fatos Gravados", "🧠 Resumos de Conversas"])

    with tab_fatos:
        if not ids_fatos:
            st.caption("Nenhum fato registrado manualmente ainda.")
        else:
            for id_m, doc_m in zip(ids_fatos, docs_fatos):
                with st.expander(f"📌 `{id_m[:8]}` — {doc_m[:60]}..."):
                    txt_edit = st.text_area("Editar conteúdo:", value=doc_m, key=f"edit_{id_m}")
                    cb1, cb2 = st.columns(2)
                    with cb1:
                        if st.button("💾 Atualizar", key=f"up_{id_m}", use_container_width=True):
                            atualizar_memoria_por_id(id_m, txt_edit)
                            st.rerun()
                    with cb2:
                        if st.button("🗑️ Remover", key=f"rm_{id_m}", use_container_width=True):
                            deletar_memoria_por_id(id_m)
                            st.rerun()

    with tab_resumos:
        if not ids_r:
            st.caption("Nenhum resumo automático gerado ainda.")
        else:
            for id_r, doc_r in zip(ids_r, docs_r):
                st.markdown(f"- **`{id_r}`**: {doc_r}")

# =============================================================
# ABA 2: TELA PRINCIPAL DO CHAT (ESTILO AI STUDIO)
# =============================================================

else:
    if len(mensagens_salvas) == 0:
        # Estado inicial minimalista idêntico à imagem do Google AI Studio
        st.markdown("""
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 52vh; text-align: center; background: radial-gradient(circle at center, rgba(39,39,42,0.22) 0%, rgba(9,9,11,0) 65%);">
                <div style="font-size: 26px; font-weight: 300; letter-spacing: 0.38em; color: #D4D4D8; margin-bottom: 14px; padding-left: 0.38em;">
                    LUME
                </div>
                <div style="font-size: 20px; font-weight: 500; color: #F4F4F5; margin-bottom: 18px;">
                    Como posso ajudar?
                </div>
                <div style="width: 32px; height: 1px; background-color: #27272A; margin-bottom: 18px;"></div>
                <div style="font-size: 13px; color: #52525B;">
                    Digite sua mensagem abaixo para começar.
                </div>
            </div>
        """, unsafe_allow_html=True)
    else:
        for idx, (autor, texto, tempo_msg) in enumerate(mensagens_salvas):
            if autor == "user":
                st.markdown(
                    f'<div style="display: flex; justify-content: flex-end; margin: 18px 0;"><div style="background-color: #18181B; border: 1px solid #27272A; color: #F4F4F5; padding: 12px 18px; border-radius: 16px; max-width: 72%; font-size: 14px; line-height: 1.5;">{texto}</div></div>', 
                    unsafe_allow_html=True
                )
            else:
                tempo_txt = f" &nbsp;·&nbsp; ⏱️ {tempo_msg}" if tempo_msg else ""
                st.markdown(f"""
                    <div style="margin: 18px 0 6px 0; font-size: 12px; color: #71717A; font-weight: 500;">
                        ✦ Lume{tempo_txt}
                    </div>
                """, unsafe_allow_html=True)
                renderizar_conteudo_assistente(texto)

    # Barra de entrada flutuante com legenda do modo selecionado
    prompt_input = st.chat_input(
        info_modo["placeholder"], 
        accept_file=True, 
        file_type=["png", "jpg", "jpeg", "webp", "pdf", "csv", "xls", "xlsx"]
    )

    st.markdown(f"""
        <div style="max-width: 760px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: #71717A; padding: 2px 8px;">
            <div>
                <span style="background-color: #18181B; border: 1px solid #27272A; color: #D4D4D8; padding: 2px 8px; border-radius: 6px; margin-right: 6px;">
                    ✨ {info_modo['badge']}
                </span>
                {info_modo['desc']}
            </div>
            <div style="color: #52525B;">Enter envia · Shift+Enter quebra</div>
        </div>
        <div style="text-align: center; font-size: 11px; color: #3F3F46; margin-top: 8px;">
            Lume AI · Respostas geradas com suporte a ChromaDB e perfis de raciocínio.
        </div>
    """, unsafe_allow_html=True)

    if prompt_input:
        if hasattr(prompt_input, "text"):
            prompt_texto = prompt_input.text
            arquivos = prompt_input.files if isinstance(prompt_input.files, list) else [prompt_input.files] if prompt_input.files else []
        elif isinstance(prompt_input, dict):
            prompt_texto = prompt_input.get("text", "")
            arquivos_dict = prompt_input.get("files", [])
            arquivos = arquivos_dict if isinstance(arquivos_dict, list) else [arquivos_dict] if arquivos_dict else []
        else:
            prompt_texto = str(prompt_input)
            arquivos = []

        arquivos_processados = []
        nomes_arquivos = []
        if arquivos:
            for arq in arquivos:
                arquivos_processados.append({
                    "nome": arq.name,
                    "bytes": arq.getvalue(),
                    "tipo": arq.type
                })
                nomes_arquivos.append(arq.name)

        if not prompt_texto and arquivos_processados:
            prompt_texto = "Analise e descreva estes documentos em detalhes."

        txt_anexos = f"📄 [Anexos: {', '.join(nomes_arquivos)}]\n\n" if nomes_arquivos else ""
        texto_usuario_salvar = f"{txt_anexos}{prompt_texto}"

        if st.session_state.chat_atual_id is None:
            titulo_chat = prompt_texto[:25] if prompt_texto else "Nova Conversa"
            st.session_state.chat_atual_id = criar_nova_conversa(titulo_chat)

        chat_id_ativo = int(st.session_state.chat_atual_id)
        salvar_mensagem(chat_id_ativo, "user", texto_usuario_salvar)

        st.markdown(
            f'<div style="display: flex; justify-content: flex-end; margin: 18px 0;"><div style="background-color: #18181B; border: 1px solid #27272A; color: #F4F4F5; padding: 12px 18px; border-radius: 16px; max-width: 72%; font-size: 14px;">{texto_usuario_salvar}</div></div>', 
            unsafe_allow_html=True
        )

        st.markdown("<div style='margin: 18px 0 6px 0; font-size: 12px; color: #71717A; font-weight: 500;'>✦ Lume</div>", unsafe_allow_html=True)

        status_placeholder = st.empty()
        status_placeholder.markdown("""
            <div style="display: flex; align-items: center; gap: 8px; color: #71717A; font-size: 13px; margin: 8px 0;">
                <span>Lume está processando</span>
                <div style="display: inline-flex; gap: 4px; align-items: center;">
                    <div class="lume-dot lume-dot-1"></div>
                    <div class="lume-dot lume-dot-2"></div>
                    <div class="lume-dot lume-dot-3"></div>
                </div>
            </div>
        """, unsafe_allow_html=True)

        chave_atual_ui = st.session_state.get("GEMINI_API_KEY", "").strip()

        def gerador_limpo_com_status():
            primeiro_token = True
            for chunk in processar_resposta_ia_stream(
                prompt_texto,
                str(chat_id_ativo),
                modo_raciocinio,
                anexos=arquivos_processados,
                api_key_override=chave_atual_ui
            ):
                if chunk:
                    if primeiro_token:
                        status_placeholder.empty()
                        primeiro_token = False
                    yield chunk
            if primeiro_token:
                status_placeholder.empty()

        inicio = time.time()
        resposta_gerada = st.write_stream(gerador_limpo_com_status)
        tempo_total = time.time() - inicio

        texto_tempo = f"Processado em {tempo_total:.1f}s"
        texto_final = limpar_vazamentos_llm(resposta_gerada)
        salvar_mensagem(chat_id_ativo, "assistant", texto_final, texto_tempo)
        st.rerun()
