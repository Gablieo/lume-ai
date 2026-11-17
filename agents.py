import os
import json
import time
import re
import streamlit as st
import streamlit.components.v1 as components
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

st.set_page_config(page_title="Lume", page_icon="✦", layout="wide")

# =============================================================
# PERSISTÊNCIA DE PASTAS
# =============================================================

ARQUIVO_PASTAS = "pastas_lume.json"

def carregar_dados_pastas() -> dict:
    if os.path.exists(ARQUIVO_PASTAS):
        try:
            with open(ARQUIVO_PASTAS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"pastas": ["Geral"], "chat_pasta": {}}

def salvar_dados_pastas(dados: dict):
    try:
        with open(ARQUIVO_PASTAS, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Erro ao salvar pastas: {e}")

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
    
    linhas = texto.split("\n")
    linhas_limpas = []
    
    for linha in linhas:
        linha_strip = linha.strip()
        if any(re.match(padrao, linha_strip, re.IGNORECASE) for padrao in padroes_vazamento):
            continue
        linhas_limpas.append(linha)
        
    resultado_texto = "\n".join(linhas_limpas).strip()
    resultado_texto = re.sub(r'^(FINAL ANSWER|FINAL_ANSWER|Thought:|Action:)\s*', '', resultado_texto, flags=re.IGNORECASE).strip()
    
    return resultado_texto

def renderizar_conteudo_assistente(texto: str):
    """Renderiza texto, imagens geradas e plots do Code Interpreter com tamanho controlado."""
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
    
    texto_limpo = sanitizar_texto_latin1(texto)
    linhas = texto_limpo.split("\n")
    
    for linha in linhas:
        linha_str = linha.strip()
        if not linha_str:
            pdf.ln(3)
            continue
        
        linha_safe = linha_str.encode('latin-1', 'replace').decode('latin-1')
        
        try:
            if linha_str.startswith("#"):
                pdf.set_font("Helvetica", "B", 13)
                pdf.set_text_color(30, 30, 35)
                texto_h = linha_safe.lstrip("#").strip()
                pdf.multi_cell(0, 7, text=texto_h)
                pdf.ln(2)
            elif linha_str.startswith(("* ", "- ")) or (len(linha_str) > 2 and linha_str[0].isdigit() and linha_str[1] == "."):
                pdf.set_font("Helvetica", "", 11)
                pdf.set_text_color(50, 50, 50)
                item_txt = f"- {linha_safe.lstrip('*-0123456789. ')}"
                pdf.multi_cell(0, 6, text=item_txt)
            else:
                pdf.set_font("Helvetica", "", 11)
                pdf.set_text_color(50, 50, 50)
                pdf.multi_cell(0, 6, text=linha_safe)
        except Exception as e:
            print(f"⚠️ Aviso ao renderizar PDF: {e}")
            continue
            
    return bytes(pdf.output())

# =============================================================
# CSS OFICIAL GEMINI DARK UI
# =============================================================

st.markdown("""
<style>
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { 
        background-color: #131316 !important; 
        color: #E3E3E3 !important; 
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    
    [data-testid="stBottom"], [data-testid="stBottom"] > div, div[data-testid="stBottomBlockContainer"] {
        background-color: #131316 !important; background: #131316 !important; border: none !important;
    }

    [data-testid="stSidebar"] { 
        background-color: #17171A !important; border-right: 1px solid #282830 !important; 
    }
    
    .stSidebar .stButton > button { 
        background-color: transparent !important; color: #C4C7C5 !important; 
        border: none !important; border-radius: 10px !important; text-align: left !important; 
        padding: 8px 10px !important; font-weight: 400 !important; transition: background-color 0.2s ease !important;
    }
    .stSidebar .stButton > button:hover { background-color: #282A2D !important; color: #FFFFFF !important; }

    div[data-baseweb="select"] > div { 
        background-color: #1E1F20 !important; border-color: #333538 !important; 
        color: #E3E3E3 !important; border-radius: 10px !important; 
    }

    /* Barra de entrada Gemini com suporte a anexo embutido */
    [data-testid="stChatInput"] { max-width: 820px !important; margin: 0 auto !important; padding-bottom: 24px !important; }
    
    [data-testid="stChatInput"] > div { 
        background-color: #1E1F20 !important; border: 1px solid #333538 !important; 
        border-radius: 28px !important; padding: 6px 14px !important; 
        box-shadow: 0 6px 24px rgba(0,0,0,0.35) !important; transition: all 0.2s ease-in-out !important; 
    }
    [data-testid="stChatInput"] > div:focus-within { 
        border-color: #444746 !important; background-color: #232427 !important; 
        box-shadow: 0 8px 30px rgba(0,0,0,0.5) !important; outline: none !important; 
    }
    [data-testid="stChatInput"] textarea { 
        color: #E3E3E3 !important; font-size: 15px !important; line-height: 1.5 !important; 
        background-color: transparent !important; border: none !important; box-shadow: none !important; 
    }
    [data-testid="stChatInput"] textarea::placeholder { color: #8E918F !important; }
    
    [data-testid="stChatInput"] button { 
        background-color: #282A2D !important; color: #E3E3E3 !important; border-radius: 50% !important; 
        border: none !important; width: 34px !important; height: 34px !important; transition: all 0.2s ease !important; 
    }
    [data-testid="stChatInput"] button:hover { background-color: #383B3E !important; color: #FFFFFF !important; }

    div[data-testid="stDownloadButton"] > button { 
        background-color: #1E1F20 !important; color: #C4C7C5 !important; border: 1px solid #333538 !important; 
        border-radius: 12px !important; padding: 4px 12px !important; font-size: 12px !important; transition: all 0.2s ease !important; 
    }
    div[data-testid="stDownloadButton"] > button:hover { 
        background-color: #282A2D !important; color: #FFFFFF !important; border-color: #444746 !important; 
    }
    
    .lume-dot {
        width: 6px;
        height: 6px;
        background-color: #8E918F;
        border-radius: 50%;
        display: inline-block;
        animation: lume-bounce 1.4s infinite ease-in-out both;
    }
    .lume-dot-1 { animation-delay: -0.32s; }
    .lume-dot-2 { animation-delay: -0.16s; }
    .lume-dot-3 { animation-delay: 0s; }
    
    @keyframes lume-bounce {
        0%, 80%, 100% { transform: scale(0.3); opacity: 0.3; }
        40% { transform: scale(1.1); opacity: 1; background-color: #E3E3E3; }
    }
    
    #MainMenu, footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

if "chat_atual_id" not in st.session_state:
    st.session_state.chat_atual_id = None

if "GEMINI_API_KEY" not in st.session_state:
    st.session_state["GEMINI_API_KEY"] = ""

# =============================================================
# BARRA LATERAL (SIDEBAR COM PASTAS E CONFIGURAÇÃO DE API)
# =============================================================

with st.sidebar:
    st.markdown("<h3 style='color: #E3E3E3; font-size: 18px; padding-left: 8px;'>✦ Lume</h3>", unsafe_allow_html=True)
    
    if st.button("➕  Nova conversa", use_container_width=True):
        st.session_state.chat_atual_id = None
        st.rerun()

    st.markdown("<p style='color: #8E918F; font-size: 12px; margin-top: 20px; padding-left: 8px;'>Modo de Raciocínio</p>", unsafe_allow_html=True)
    
    modo_raciocinio = st.selectbox(
        "Modo", 
        ["⚡⚡ Ultra Flash", "⚡ Rápido", "⚖️ Equilibrado", "🧠 Pensamento Profundo"], 
        index=2, 
        label_visibility="collapsed"
    )

    with st.popover("🔑 Configurar API", use_container_width=True):
        st.markdown("<h4 style='color: #E3E3E3;'>⚡ Chave da API (Sessão)</h4>", unsafe_allow_html=True)
        nova_chave = st.text_input(
            "GEMINI_API_KEY (Opcional):",
            value=st.session_state.get("GEMINI_API_KEY", ""),
            type="password",
            placeholder="AIzaSy... (isolada no seu navegador)"
        )
        col_api1, col_api2 = st.columns([1, 1])
        with col_api1:
            if st.button("💾 Aplicar", use_container_width=True, key="btn_salvar_api"):
                st.session_state["GEMINI_API_KEY"] = nova_chave.strip()
                st.success("Chave aplicada!")
                st.rerun()
        with col_api2:
            if st.button("🧹 Limpar", use_container_width=True, key="btn_limpar_api"):
                st.session_state["GEMINI_API_KEY"] = ""
                st.info("Usando modo padrão.")
                st.rerun()
        st.caption("🔒 Sua chave fica salva apenas na memória temporária da sua aba atual.")

    st.markdown("<hr style='border-color: #282830; margin: 15px 0;'>", unsafe_allow_html=True)
    st.markdown("<p style='color: #8E918F; font-size: 12px; padding-left: 8px;'>Memória Vetorial (ChromaDB)</p>", unsafe_allow_html=True)
    
    novo_fato = st.text_input("Fato manual", placeholder="Digite um fato para gravar...", key="input_memoria", label_visibility="collapsed")
    if st.button("💾 Gravar Fato", use_container_width=True):
        if novo_fato.strip():
            msg = salvar_memoria(novo_fato.strip())
            st.success(msg)
            st.rerun()

    with st.popover("⚙️ Gerenciar Banco Vetorial", use_container_width=True):
        st.markdown("<h4 style='color: #E3E3E3;'>🗂️ Gestão de Memória</h4>", unsafe_allow_html=True)
        sub_aba_listar, sub_aba_resumos, sub_aba_buscar, sub_aba_adicionar = st.tabs(["📋 Fatos", "🧠 Resumos", "🔍 Buscar", "➕ Criar"])

        with sub_aba_listar:
            ids, docs = listar_todas_memorias()
            if not ids:
                st.info("Nenhum fato manual registrado.")
            else:
                st.caption(f"Total: {len(ids)} fatos gravados")
                for id_m, doc_m in zip(ids, docs):
                    with st.expander(f"`{id_m[:8]}` — {doc_m[:35]}..."):
                        novo_texto = st.text_area("Conteúdo:", value=doc_m, key=f"pop_txt_{id_m}", height=80)
                        col_s, col_d = st.columns([1, 1])
                        with col_s:
                            if st.button("💾 Salvar", key=f"pop_save_{id_m}"):
                                res = atualizar_memoria_por_id(id_m, novo_texto)
                                st.success(res)
                                st.rerun()
                        with col_d:
                            if st.button("🗑️ Excluir", key=f"pop_del_{id_m}", type="primary"):
                                res = deletar_memoria_por_id(id_m)
                                st.warning(res)
                                st.rerun()

        with sub_aba_resumos:
            try:
                dados_r = collection_resumos.get()
                ids_r = dados_r.get("ids", [])
                docs_r = dados_r.get("documents", [])
                if not ids_r:
                    st.info("Nenhum resumo automático gerado ainda.")
                else:
                    st.caption(f"Total: {len(ids_r)} resumos salvos")
                    for id_r, doc_r in zip(ids_r, docs_r):
                        st.markdown(f"**`{id_r}`**:\n*{doc_r}*")
            except Exception as e:
                st.error(f"Erro ao carregar resumos: {e}")

        with sub_aba_buscar:
            termo_busca = st.text_input("Buscar por termos:", key="pop_busca")
            if termo_busca:
                resultado = consultar_memoria(termo_busca)
                st.info(resultado if resultado else "Nenhum resultado.")

        with sub_aba_adicionar:
            nova_info = st.text_area("Texto longo/fato:", key="pop_nova_info")
            id_ref_add = st.session_state.chat_atual_id if st.session_state.chat_atual_id else "novo"
            if st.button("📌 Adicionar", key=f"pop_btn_add_{id_ref_add}"):
                if nova_info.strip():
                    msg = salvar_memoria(nova_info.strip())
                    st.success(msg)
                    st.rerun()

    st.markdown("<hr style='border-color: #282830; margin: 15px 0;'>", unsafe_allow_html=True)
    
    # --- GERENCIAMENTO E AGRUPAMENTO POR PASTAS ---
    dados_pastas = carregar_dados_pastas()
    pastas_existentes = dados_pastas.get("pastas", ["Geral"])
    if "Geral" not in pastas_existentes:
        pastas_existentes.insert(0, "Geral")

    st.markdown("<p style='color: #8E918F; font-size: 12px; padding-left: 8px;'>Pastas & Conversas</p>", unsafe_allow_html=True)
    
    with st.popover("📁 Gerenciar Pastas", use_container_width=True):
        st.markdown("<h4 style='color: #E3E3E3;'>Organizador de Pastas</h4>", unsafe_allow_html=True)
        nova_pasta_nome = st.text_input("Nova pasta:", placeholder="Ex: Imagens, Estudos...", key="input_nova_pasta")
        if st.button("➕ Criar Pasta", use_container_width=True, key="btn_criar_pasta"):
            if nova_pasta_nome.strip() and nova_pasta_nome.strip() not in pastas_existentes:
                dados_pastas["pastas"].append(nova_pasta_nome.strip())
                salvar_dados_pastas(dados_pastas)
                st.rerun()
        
        pastas_remover = [p for p in pastas_existentes if p != "Geral"]
        if pastas_remover:
            st.markdown("<hr style='border-color: #282830; margin: 10px 0;'>", unsafe_allow_html=True)
            p_excluir = st.selectbox("Excluir pasta:", pastas_remover, key="sel_del_pasta")
            if st.button("🗑️ Excluir Pasta Selecionada", key="btn_del_pasta", type="primary", use_container_width=True):
                dados_pastas["pastas"].remove(p_excluir)
                for cid, p_nome in list(dados_pastas.get("chat_pasta", {}).items()):
                    if p_nome == p_excluir:
                        dados_pastas["chat_pasta"][cid] = "Geral"
                salvar_dados_pastas(dados_pastas)
                st.rerun()

    conversas_existentes = carregar_conversas()
    chat_pasta_map = dados_pastas.get("chat_pasta", {})

    # Agrupa conversas por pasta
    conversas_por_pasta = {p: [] for p in pastas_existentes}
    for chat_id, titulo in conversas_existentes:
        pasta_do_chat = chat_pasta_map.get(str(chat_id), "Geral")
        if pasta_do_chat not in conversas_por_pasta:
            conversas_por_pasta[pasta_do_chat] = []
        conversas_por_pasta[pasta_do_chat].append((chat_id, titulo))

    # Renderiza as pastas como acordions
    for nome_pasta in pastas_existentes:
        chats_na_pasta = conversas_por_pasta.get(nome_pasta, [])
        with st.expander(f"📁 {nome_pasta} ({len(chats_na_pasta)})", expanded=(nome_pasta == "Geral")):
            if not chats_na_pasta:
                st.caption("Nenhuma conversa.")
            else:
                for chat_id, titulo in chats_na_pasta:
                    label = f"💬 {titulo[:14]}..." if len(titulo) > 14 else f"💬 {titulo}"
                    col1, col2, col3 = st.columns([0.65, 0.18, 0.17])
                    
                    with col1:
                        if st.button(label, key=f"chat_{chat_id}", use_container_width=True):
                            st.session_state.chat_atual_id = chat_id
                            st.rerun()
                    
                    with col2:
                        with st.popover("📁"):
                            idx_atual = pastas_existentes.index(nome_pasta) if nome_pasta in pastas_existentes else 0
                            nova_p = st.selectbox("Mover:", pastas_existentes, index=idx_atual, key=f"mov_sel_{chat_id}")
                            if st.button("Mover", key=f"btn_mov_{chat_id}", use_container_width=True):
                                if "chat_pasta" not in dados_pastas:
                                    dados_pastas["chat_pasta"] = {}
                                dados_pastas["chat_pasta"][str(chat_id)] = nova_p
                                salvar_dados_pastas(dados_pastas)
                                st.rerun()
                                
                    with col3:
                        if st.button("🗑️", key=f"del_{chat_id}"):
                            deletar_conversa(chat_id)
                            deletar_resumo_chat(chat_id)
                            if str(chat_id) in dados_pastas.get("chat_pasta", {}):
                                del dados_pastas["chat_pasta"][str(chat_id)]
                                salvar_dados_pastas(dados_pastas)
                            if st.session_state.chat_atual_id == chat_id:
                                st.session_state.chat_atual_id = None
                            st.rerun()

# =============================================================
# HOLOGRAMA 3D CENTRAL
# =============================================================

HTML_HOLOGRAM_3D = """<!DOCTYPE html><html><head><style>body { margin: 0; background-color: #131316; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; font-family: sans-serif; } canvas { display: block; } .title { color: #E3E3E3; font-size: 18px; font-weight: 500; margin-top: 15px; } .sub { color: #8E918F; font-size: 13px; margin-top: 5px; }</style></head><body><canvas id="coreCanvas" width="500" height="380"></canvas><div class="title">Lume</div><div class="sub">O que temos para hoje?</div><script>const canvas=document.getElementById('coreCanvas'),ctx=canvas.getContext('2d'),width=500,height=380,cx=250,cy=190;let time=0;const coreNodes=[];for(let i=0;i<40;i++){let u=Math.random(),v=Math.random(),t=u*2*Math.PI,p=Math.acos(2*v-1),r=45;coreNodes.push([r*Math.sin(p)*Math.cos(t),r*Math.sin(p)*Math.sin(t),r*Math.cos(p)])}const ringConfigs=[{r:130,tx:0.4,ty:0.2,spd:0.02,col:"#444746",dash:!1,w:1},{r:140,tx:-0.5,ty:0.8,spd:-0.015,col:"#282830",dash:!0,w:1},{r:110,tx:1.2,ty:-0.5,spd:0.035,col:"#8E918F",dash:!1,w:1},{r:80,tx:0,ty:0,spd:0.05,col:"#FFFFFF",dash:!1,w:2}],rings=ringConfigs.map(c=>{let pts=[];for(let i=0;i<45;i++){let a=i/45*Math.PI*2;pts.push([c.r*Math.cos(a),c.r*Math.sin(a),0])}return{...c,pts,angle:0}});function rotate(x,y,z,ax,ay,az){let cosX=Math.cos(ax),sinX=Math.sin(ax),cosY=Math.cos(ay),sinY=Math.sin(ay),cosZ=Math.cos(az),sinZ=Math.sin(az);let y1=y*cosX-z*sinX,z1=y*sinX+z*cosX,x1=x;let x2=x1*cosY+z1*sinY,z2=-x1*sinY+z1*cosY,y2=y1;let x3=x2*cosZ-y2*sinZ,y3=x2*sinZ+y2*cosZ,z3=z2;return[x3,y3,z3]}function project(x,y,z){let f=300/(360+z);return[cx+x*f,cy+y*f,f]}function animate(){ctx.clearRect(0,0,width,height);time+=0.015;let gAx=time*0.4,gAy=time*0.7,q=[];rings.forEach(r=>{r.angle+=r.spd;let pPts=r.pts.map(p=>{let r1=rotate(p[0],p[1],p[2],0,0,r.angle),r2=rotate(r1[0],r1[1],r1[2],r.tx,r.ty,0),r3=rotate(r2[0],r2[1],r2[2],gAx,gAy,0),pr=project(r3[0],r3[1],r3[2]);return[...pr,r3[2]]});for(let i=0;i<pPts.length;i++){let p1=pPts[i],p2=pPts[(i+1)%pPts.length],zA=(p1[2]+p2[2])/2;if(!r.dash||i%2!==0)q.push({z:zA,type:'line',x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1],col:zA<-30?"#1c1c21":r.col,w:r.w})}});coreNodes.forEach(n=>{let r=rotate(n[0],n[1],n[2],gAx,gAy,time*1.5),pr=project(r[0],r[1],r[2]);q.push({z:r[2],type:'oval',x:pr[0],y:pr[1],r:Math.max(1,2.5*pr[2]),col:r[2]>0?"#FFFFFF":"#444746"})});q.sort((a,b)=>a.z-b.z);q.forEach(i=>{ctx.beginPath();if(i.type==='line'){ctx.moveTo(i.x1,i.y1);ctx.lineTo(i.x2,i.y2);ctx.strokeStyle=i.col;ctx.lineWidth=i.w;ctx.stroke()}else{ctx.arc(i.x,i.y,i.r,0,Math.PI*2);ctx.fillStyle=i.col;ctx.fill()}});requestAnimationFrame(animate)}animate();</script></body></html>"""

# =============================================================
# TELA PRINCIPAL DO CHAT
# =============================================================

if st.session_state.chat_atual_id is not None:
    mensagens_salvas = carregar_mensagens(st.session_state.chat_atual_id)
else:
    mensagens_salvas = []

mostrar_holograma = (len(mensagens_salvas) == 0)

if mostrar_holograma:
    components.html(HTML_HOLOGRAM_3D, height=470)
else:
    for idx, (autor, texto, tempo_msg) in enumerate(mensagens_salvas):
        if autor == "user":
            st.markdown(
                f'<div style="display: flex; justify-content: flex-end; margin: 20px 0;"><div style="background-color: #282A2D; color: #E3E3E3; padding: 12px 20px; border-radius: 20px; max-width: 70%; font-size: 15px;">{texto}</div></div>', 
                unsafe_allow_html=True
            )
        else:
            tempo_txt = f" &nbsp; ⏱️ {tempo_msg}" if tempo_msg else ""

            st.markdown(f"""
                <div style="margin: 20px 0 4px 0; font-size: 13px; color: #8E918F; font-weight: 500;">
                    Lume{tempo_txt}
                </div>
            """, unsafe_allow_html=True)
            
            # Renderiza com suporte a imagem e plots do Code Interpreter
            renderizar_conteudo_assistente(texto)
            
            prompt_usuario_anterior = ""
            if idx > 0 and mensagens_salvas[idx-1][0] == "user":
                # Remove a tag de anexos para não disparar gatilhos indevidos
                prompt_limpo = re.sub(r'📄 \[Anexos:.*?\]\n\n', '', mensagens_salvas[idx-1][1])
                prompt_usuario_anterior = prompt_limpo.lower()

            # Gatilhos explícitos para geração do botão de PDF
            palavras_chave_pdf = ["gerar pdf", "crie um pdf", "criar pdf", "transformar em pdf", "gerar relatório", "baixar arquivo"]
            if any(p in prompt_usuario_anterior for p in palavras_chave_pdf) and "[IMAGEM_GERADA:" not in texto:
                pdf_data = gerar_pdf_bytes(texto)
                st.download_button(
                    label="📄 Baixar em PDF",
                    data=pdf_data,
                    file_name=f"lume_documento_{st.session_state.chat_atual_id}_{idx}.pdf",
                    mime="application/pdf",
                    key=f"pdf_btn_{st.session_state.chat_atual_id}_{idx}"
                )

# =============================================================
# ENTRADA DO USUÁRIO E CRIAÇÃO AUTOMÁTICA DE CONVERSAS
# =============================================================

HTML_PENSANDO = """
    <div style="display: flex; align-items: center; gap: 8px; color: #8E918F; font-size: 14px; margin: 8px 0;">
        <span>Lume está processando</span>
        <div style="display: inline-flex; gap: 4px; align-items: center;">
            <div class="lume-dot lume-dot-1"></div>
            <div class="lume-dot lume-dot-2"></div>
            <div class="lume-dot lume-dot-3"></div>
        </div>
    </div>
"""

# PERMITE MÚLTIPLOS ARQUIVOS, PDFs, CSVs E PLANILHAS EXCEL
prompt_input = st.chat_input(
    "Peça ao Lume...", 
    accept_file=True, 
    file_type=["png", "jpg", "jpeg", "webp", "pdf", "csv", "xls", "xlsx"]
)

if prompt_input:
    # 1. Extração segura de texto e múltiplos arquivos
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

    # Lê os bytes e nomes de todos os arquivos anexados
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

    # 2. Criação lazy da conversa no banco
    if st.session_state.chat_atual_id is None:
        titulo_chat = prompt_texto[:25] if prompt_texto else "Conversa Lume"
        novo_id = criar_nova_conversa(titulo_chat)
        st.session_state.chat_atual_id = novo_id

    chat_id_ativo = int(st.session_state.chat_atual_id)

    # 3. Salva e exibe a mensagem do usuário
    salvar_mensagem(chat_id_ativo, "user", texto_usuario_salvar)

    st.markdown(
        f'<div style="display: flex; justify-content: flex-end; margin: 20px 0;"><div style="background-color: #282A2D; color: #E3E3E3; padding: 12px 20px; border-radius: 20px; max-width: 70%; font-size: 15px;">{texto_usuario_salvar}</div></div>', 
        unsafe_allow_html=True
    )

    # 4. Cabeçalho de resposta do Lume
    st.markdown("""
        <div style="margin: 20px 0 4px 0; font-size: 13px; color: #8E918F; font-weight: 500;">
            Lume
        </div>
    """, unsafe_allow_html=True)

    status_placeholder = st.empty()
    status_placeholder.markdown(HTML_PENSANDO, unsafe_allow_html=True)
    
    def gerador_limpo_com_status():
        primeiro_token = True
        for chunk in processar_resposta_ia_stream(prompt_texto, str(chat_id_ativo), modo_raciocinio, anexos=arquivos_processados):
            if chunk:
                if primeiro_token:
                    status_placeholder.empty()
                    primeiro_token = False
                yield chunk
        if primeiro_token:
            status_placeholder.empty()

    # 5. Consome streaming e calcula tempo
    inicio = time.time()
    resposta_gerada = st.write_stream(gerador_limpo_com_status)
    tempo_total = time.time() - inicio

    # 6. Salva no banco e atualiza a interface
    texto_tempo = f"Processado em {tempo_total:.1f}s"
    texto_final = limpar_vazamentos_llm(resposta_gerada)
    salvar_mensagem(chat_id_ativo, "assistant", texto_final, texto_tempo)

    st.rerun()
