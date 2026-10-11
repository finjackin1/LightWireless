"""A janela do LightWireless no padrao "app simples" (skill interface-simples,
11/out/2026): barra propria, janela FIXA 5:4, tela principal em duas colunas
(luz + timer | temperatura), agenda e opcoes pela barra, tela da lampada
(achar na rede + chave) e rodape com o indicador de conexao.

A janela nunca fala com a rede: tudo pela `Lampada` (thread propria). Os
avisos dela chegam numa fila lida a cada 50 ms no laco da janela.
"""
from __future__ import annotations

import datetime
import logging
import os
import queue
import threading
import time
import tkinter as tk
from types import SimpleNamespace

from . import __version__, atalhos as AT, conta_tuya, descoberta, estilo as E, inicio_windows, kelvin, moldura, nuvem
from . import controles as C
from . import lampada as L
from .agenda import DIAS, Agenda, dias_texto
from .bandeja import Bandeja
from .caminhos import relatorios
from .config import de_arquivo_do_wizard, novo_id
from .controles import Botao, BotaoBarra, Chave, Menu, Painel, Passo, Rolagem
from .motor_atalhos import Motor

log = logging.getLogger(__name__)

# Janela de TAMANHO FIXO 5:4 (padrao da casa; ele deixou a proporcao comigo,
# 11/out/2026: controle de um aparelho -> 5:4). Em 100%; px() escala.
LARGURA, ALTURA = 500, 400
MARGEM = 14
COL_ESQ = 222            # coluna da luz / dica
TIMER_PASSOS = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 360, 480, 600, 720]
CARENCIA_S = 1.5         # depois de um comando, o eco atrasado nao repinta a tela
TAMANHO_DA_CHAVE = 16    # a localKey da Tuya tem sempre 16 caracteres
PEDIDO_MOSTRAR = relatorios() / ".mostrar"
# o empacotar pede para fechar antes de trocar o .exe: sai SEM a acao "ao sair"
# (um build nao pode acender/apagar a luz dele)
PEDIDO_SAIR = relatorios() / ".sair"

ESTADOS = {
    L.PARADO: "Parado",
    L.PROCURANDO: "Procurando a lâmpada…",
    L.CONECTADA: "Conectada",
    L.SEM_LAMPADA: "Lâmpada não configurada",
    L.ERRO: "Sem conexão",
}


def fmt_min(m: int) -> str:
    h, mm = divmod(int(m), 60)
    if not h:
        return f"{mm} min"
    return f"{h} h" if not mm else f"{h} h {mm:02d}"


def fmt_k(valor: int) -> str:
    return f"{kelvin.para_kelvin(valor)} K"


class Janela:
    def __init__(self, cfg, escondida: bool = False, lampada=None):
        self.cfg = cfg
        self.raiz = r = tk.Tk()
        # erro dentro de um clique/after vai pro log e a janela segue viva
        r.report_callback_exception = lambda *e: log.error("erro na interface", exc_info=e)
        self._job_gravar = None
        E.iniciar(r)
        r.title("LightWireless")
        r.configure(bg=E.SUPERFICIE_BORDA)
        moldura.preparar(r, visivel=not escondida)
        self._ja_mostrou = not escondida
        self.conexao = L.PROCURANDO
        self.detalhe = ""
        self.ligada = False
        self.temp = int(cfg["ultima_temperatura"])
        self._conhece_estado = False
        self._ignorar_estado_ate = 0.0
        self._job_sincronizar = None
        self._ao_abrir_feito = False
        self.timer_fim: datetime.datetime | None = None
        self.agenda = Agenda()
        self._tela = None
        self._capturando = None
        self._dica = None
        self._eventos: queue.Queue = queue.Queue()
        self._busca: queue.Queue = queue.Queue()
        self.cl = {"procurando": False, "achados": [], "tentativa": (0, 0), "recado": "",
                   "valores": {}, "regiao": cfg["nuvem"].get("regiao") or nuvem.REGIAO_PADRAO,
                   "consultando": False, "ja_procurou": False}
        # login na conta Tuya (so em memoria; o user code some ao gravar)
        self.lg = {"estado": "digitando", "user_code": "", "qr": "", "qr_n": 0, "qr_ate": 0.0,
                   "conta": [], "conta_pendente": None, "erro": ("", ""), "gravado": None, "voltar": "lampada"}
        self._sessao = None

        r.geometry(f"{E.px(LARGURA)}x{E.px(ALTURA)}")
        self.casca = tk.Frame(r, bg=E.FUNDO)
        self.casca.pack(fill="both", expand=True, padx=1, pady=1)
        self._barra()
        self.corpo = tk.Frame(self.casca, bg=E.FUNDO)
        self.corpo.pack(fill="both", expand=True)
        self._rodape()

        self.telas: dict[str, tk.Frame] = {}
        self.ir("inicio")
        self._posicionar()

        self.bandeja = Bandeja()
        self.bandeja.iniciar()
        self.motor = Motor()
        self.motor.iniciar()
        self.motor.aplicar(AT.guardados(self.cfg))

        self.luz = lampada if lampada is not None else self._nova_lampada()
        self.luz.iniciar()

        r.bind("<Key>", self._tecla_janela)
        r.protocol("WM_DELETE_WINDOW", self.esconder)
        if escondida and self.bandeja.disponivel:     # sem bandeja nao ha como achar a janela
            r.withdraw()
        elif escondida:
            self._ja_mostrou = True
            moldura.fixar_barra_de_tarefas(r)
        self._volta = 0
        try:
            PEDIDO_SAIR.unlink()                 # pedido velho (de outro build) nao fecha quem acabou de abrir
        except OSError:
            pass
        r.after(50, self._ciclo)

    def _nova_lampada(self):
        return L.Lampada(
            self.cfg,
            ao_estado=lambda dps: self._eventos.put(("estado", dps)),
            ao_conexao=lambda est, det: self._eventos.put(("conexao", est, det)),
        )

    # == barra, rodape ===================================================
    def _barra(self):
        b = tk.Frame(self.casca, bg=E.FUNDO_FUNDO, height=E.px(34))
        b.pack(fill="x")
        b.pack_propagate(False)
        self._logo = tk.Label(b, image=E.icone("marca", E.px(16), E.ACENTO), bg=E.FUNDO_FUNDO)
        self._logo.pack(side="left", padx=(E.px(10), E.px(6)))
        tit = tk.Label(b, text="LightWireless", bg=E.FUNDO_FUNDO, fg=E.TEXTO_2, font=E.fonte(10, "semibold"))
        tit.pack(side="left")
        self.b_fechar = BotaoBarra(b, "fechar", self.esconder, largura=40, ao_segurar=self.sair,
                                   dica="Fechar\nclique: esconde na bandeja\nsegurar: sai")
        self.b_fechar.pack(side="right")
        BotaoBarra(b, "minimizar", lambda: moldura.minimizar(self.raiz), dica="Minimizar").pack(side="right")
        self.b_opcoes = BotaoBarra(b, "opcoes", lambda: self._alternar_tela("opcoes"), dica="Opções")
        self.b_opcoes.pack(side="right")
        self.b_agenda = BotaoBarra(b, "agenda", lambda: self._alternar_tela("agenda"), dica="Agenda")
        self.b_agenda.pack(side="right")
        arr = moldura.Arrasto(self.raiz)
        for w in (b, tit, self._logo):
            arr.ligar(w)
            w.bind("<ButtonRelease-1>", lambda _e=None: self._guardar_posicao(), add="+")

    def _rodape(self):
        f = tk.Frame(self.casca, bg=E.FUNDO)
        f.pack(fill="x", side="bottom", pady=(0, E.px(8)))
        self._ind = tk.Label(f, bg=E.FUNDO)
        self._ind.pack(side="left", padx=(E.px(MARGEM), E.px(4)))
        self._ind_txt = tk.Label(f, bg=E.FUNDO, fg=E.TEXTO_2, font=E.fonte(9))
        self._ind_txt.pack(side="left")
        self._pintar_indicador()

    def _pintar_indicador(self):
        cor = {L.CONECTADA: E.VERDE, L.ERRO: E.ERRO}.get(self.conexao, E.APAGADO)
        self._ind.config(image=E.icone("wifi", E.px(14), cor))
        txt = ESTADOS.get(self.conexao, self.conexao)
        if self.conexao == L.CONECTADA and self.detalhe:
            txt += f" · {self.detalhe}"
        self._ind_txt.config(text=txt)

    # == telas =============================================================
    def _alternar_tela(self, nome):
        self.ir("inicio" if self._tela == nome else nome)

    def ir(self, nome: str, **kw):
        """Troca de tela. Principal e montada uma vez; as outras a cada visita."""
        self._capturando = None
        if self._tela == "lampada_conta" and nome != "lampada_conta":
            self._conta_cancelar()               # sair da tela cancela o login em curso
        congelar(self.raiz, True)
        try:
            if self._tela and self._tela in self.telas:
                t = self.telas[self._tela]
                t.pack_forget()
                if self._tela != "inicio":
                    t.destroy()
                    del self.telas[self._tela]
            if nome not in self.telas:
                t = tk.Frame(self.corpo, bg=E.FUNDO)
                getattr(self, "_tela_" + nome)(t, **kw)
                self.telas[nome] = t
            self.telas[nome].pack(fill="both", expand=True, padx=E.px(MARGEM), pady=(E.px(10), E.px(6)))
            self._tela = nome
            self.b_agenda.acender(nome.startswith("agenda"))
            self.b_opcoes.acender(nome == "opcoes")
            self._medir()
        finally:
            congelar(self.raiz, False)

    def _medir(self):
        self.raiz.update_idletasks()
        Painel.medir_todos(self.corpo)

    def _titulo_tela(self, pai, texto, voltar_para="inicio"):
        f = tk.Frame(pai, bg=E.FUNDO)
        f.pack(fill="x", pady=(0, E.px(8)))
        Botao(f, icone="voltar", ao_clicar=lambda: self.ir(voltar_para), largura=32, altura=32,
              raio=8, fundo=E.FUNDO, cor=E.FUNDO, lado_icone=16, dica="Voltar (Esc)").pack(side="left")
        tk.Label(f, text=texto, bg=E.FUNDO, fg=E.TEXTO, font=E.fonte(12, "semibold")).pack(
            side="left", padx=E.px(6))
        self._voltar_para = voltar_para
        return f

    def _cabeca_painel(self, painel, texto, legenda=None):
        f = tk.Frame(painel.dentro, bg=E.CAMADA_1)
        f.pack(fill="x")
        tk.Label(f, text=texto, bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10.5, "semibold")).pack(side="left")
        lb = None
        if legenda is not None:
            lb = tk.Label(f, text=legenda, bg=E.CAMADA_1, fg=E.TEXTO_2, font=E.fonte(9))
            lb.pack(side="left", padx=E.px(6))
        return f, lb

    def _duas_colunas(self, t, esq=COL_ESQ):
        t.grid_columnconfigure(0, minsize=E.px(esq), weight=0)
        t.grid_columnconfigure(1, weight=1)
        t.grid_rowconfigure(0, weight=1)
        a = tk.Frame(t, bg=E.FUNDO, width=E.px(esq))
        a.grid(row=0, column=0, sticky="nsew")
        a.pack_propagate(False)
        a.grid_propagate(False)
        b = tk.Frame(t, bg=E.FUNDO)
        b.grid(row=0, column=1, sticky="nsew", padx=(E.px(10), 0))
        return a, b

    def _texto(self, pai, texto, cor=E.TEXTO_2, tam=9.5, **pack):
        """Texto que QUEBRA na largura do painel (nada de texto cortado)."""
        lb = tk.Label(pai, text=texto, bg=E.CAMADA_1, fg=cor, font=E.fonte(tam), anchor="w", justify="left")
        lb.pack(fill="x", **pack)
        quebrar(pai, lb)
        return lb

    # -- principal --------------------------------------------------------------
    # Esquerda = a luz (ligar/apagar + timer) ou, sem conexao, a dica do que
    # fazer; direita = a temperatura (seis tons, de quente a frio).
    def _tela_inicio(self, t):
        esq, dir_ = self._duas_colunas(t)
        self.f_controles = tk.Frame(esq, bg=E.FUNDO)
        self.f_controles.pack(fill="both", expand=True)
        p = Painel(self.f_controles)
        p.pack(fill="x")
        _cab, self.l_luz = self._cabeca_painel(p, "Luz", "")
        linha = tk.Frame(p.dentro, bg=E.CAMADA_1)
        linha.pack(fill="x", pady=(E.px(10), 0))
        self.b_ligar = Botao(linha, texto="Ligar", icone="marca", ao_clicar=lambda: self.pedir_ligar(True),
                             largura=94, altura=78, raio=12, fundo=E.CAMADA_1, tam=11, lado_icone=20,
                             dica="Ligar (1)")
        self.b_ligar.pack(side="left", expand=True, fill="x")
        self.b_apagar = Botao(linha, texto="Apagar", icone="desligar", ao_clicar=lambda: self.pedir_ligar(False),
                              largura=94, altura=78, raio=12, fundo=E.CAMADA_1, tam=11, lado_icone=18,
                              dica="Apagar (0)")
        self.b_apagar.pack(side="left", expand=True, fill="x", padx=(E.px(6), 0))

        p = Painel(self.f_controles)
        p.pack(fill="x", pady=(E.px(10), 0))
        _cab, self.l_timer = self._cabeca_painel(p, "Apagar em", "")
        linha = tk.Frame(p.dentro, bg=E.CAMADA_1)
        linha.pack(fill="x", pady=(E.px(10), 0))
        self.p_timer = Passo(linha, self.cfg["timer_minutos"], 5, 720, 5, fmt_min, self._mudou_timer,
                             passos=TIMER_PASSOS, largura_texto=48)
        self.p_timer.pack(side="left")
        self.b_timer = Botao(linha, texto="Iniciar", ao_clicar=self._timer_botao, largura=72, altura=32,
                             raio=8, fundo=E.CAMADA_1, tam=10)
        self.b_timer.pack(side="right")

        # dica de conexao: ocupa a coluna da luz quando nao ha conexao
        self.p_dica = Painel(esq, borda=E.misturar(E.ALERTA, E.CAMADA_1, 0.45), encher=True)
        self.l_dica_tit = tk.Label(self.p_dica.dentro, bg=E.CAMADA_1, fg=E.ALERTA, anchor="w", justify="left",
                                   font=E.fonte(10.5, "semibold"), wraplength=E.px(COL_ESQ - 30))
        self.l_dica_tit.pack(fill="x")
        self.l_dica_txt = tk.Label(self.p_dica.dentro, bg=E.CAMADA_1, fg=E.TEXTO_2, anchor="w",
                                   justify="left", font=E.fonte(9.5), wraplength=E.px(COL_ESQ - 30))
        self.l_dica_txt.pack(fill="x", pady=(E.px(6), 0))
        self.b_dica = Botao(self.p_dica.dentro, texto="Procurar agora", ao_clicar=self._dica_botao,
                            largura=190, altura=34, raio=8, fundo=E.CAMADA_1, tam=10)
        self.b_dica.pack(side="bottom", fill="x")
        self._dica_visivel = False

        # temperatura
        p = Painel(dir_, encher=True)
        p.pack(fill="both", expand=True)
        _cab, self.l_temp = self._cabeca_painel(p, "Temperatura", "")
        grade = tk.Frame(p.dentro, bg=E.CAMADA_1)
        grade.pack(fill="both", expand=True, pady=(E.px(10), 0))
        self.b_temp = {}
        for i, v in enumerate(kelvin.ESCOLHAS):
            b = Botao(grade, texto=fmt_k(v), icone="marca", cor_icone=kelvin.cor(v),
                      ao_clicar=lambda v=v: self.pedir_temperatura(v), largura=110, altura=50, raio=10,
                      fundo=E.CAMADA_1, tam=10, peso="normal", lado_icone=18,
                      dica=("mais quente" if v == 0 else "mais fria" if v == 1000 else None))
            b.grid(row=i // 2, column=i % 2, sticky="nsew",
                   padx=(0 if i % 2 == 0 else E.px(6), 0), pady=(0 if i < 2 else E.px(6), 0))
            self.b_temp[v] = b
        for c in range(2):
            grade.grid_columnconfigure(c, weight=1, uniform="t")
        for rr in range(3):
            grade.grid_rowconfigure(rr, weight=1, uniform="t")
        self._texto(p.dentro, "teclas + e − mudam o tom", cor=E.APAGADO, tam=8.5, pady=(E.px(8), 0))
        self._pintar_luz()
        self._pintar_timer()
        self._pintar_dica()

    # -- agenda ---------------------------------------------------------------------
    def _opcoes_acao(self, incluir_nada=False):
        ops = [("Não fazer nada", "nada")] if incluir_nada else []
        ops += [("Ligar", "ligar"), ("Apagar", "desligar")]
        ops += [(f"Ligar em {fmt_k(v)}", f"t:{v}") for v in kelvin.ESCOLHAS]
        return ops

    def _rotulo_acao(self, acao: str) -> str:
        return next((n for n, a in self._opcoes_acao(True) if a == acao), acao)

    def _tela_agenda(self, t):
        self._titulo_tela(t, "Agenda")
        p = Painel(t, encher=True)
        p.pack(fill="both", expand=True)
        cab, _ = self._cabeca_painel(p, "Horários", "o PC precisa estar ligado")
        Botao(cab, icone="mais", ao_clicar=lambda: self.ir("agenda_item", aid=None), largura=28, altura=28,
              raio=8, fundo=E.CAMADA_1, cor=E.CAMADA_1, lado_icone=14, dica="Novo horário").pack(side="right")
        rol = self.rol_agenda = Rolagem(p.dentro)
        rol.pack(fill="both", expand=True)
        self._linhas_ag = {}
        itens = sorted(self.cfg["agenda"], key=lambda i: i["hora"])
        if not itens:
            tk.Label(rol.dentro, text="Nenhum horário ainda. Use o + para criar.", bg=E.CAMADA_1,
                     fg=E.TEXTO_2, font=E.fonte(9.5), anchor="w").pack(fill="x", pady=(E.px(8), 0))
        for it in itens:
            f = tk.Frame(rol.dentro, bg=E.CAMADA_1)
            f.pack(fill="x", pady=(E.px(8), 0))
            self._linhas_ag[it["id"]] = f
            tk.Label(f, text=it["hora"], bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(14, "semibold")).pack(side="left")
            Chave(f, it.get("ligado", True), lambda v, it=it: self._agenda_ligar(it, v)).pack(side="right")
            Botao(f, icone="editar", ao_clicar=lambda i=it["id"]: self.ir("agenda_item", aid=i), largura=32,
                  altura=32, raio=8, fundo=E.CAMADA_1, cor=E.CAMADA_1, lado_icone=14,
                  dica="Editar").pack(side="right", padx=(0, E.px(6)))
            tx = tk.Frame(f, bg=E.CAMADA_1)
            tx.pack(side="left", padx=E.px(12), fill="x", expand=True)
            a = tk.Label(tx, text=self._rotulo_acao(it["acao"]), bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10),
                         anchor="w", justify="left")
            a.pack(fill="x")
            d = tk.Label(tx, text=dias_texto(it["dias"]), bg=E.CAMADA_1, fg=E.TEXTO_2, font=E.fonte(9),
                         anchor="w", justify="left")
            d.pack(fill="x")
            quebrar(tx, a, d)
        rol.atualizar()

    def _agenda_ligar(self, it, v):
        it["ligado"] = v
        return self.cfg.gravar()

    def _tela_agenda_item(self, t, aid=None):
        it = next((i for i in self.cfg["agenda"] if i["id"] == aid), None)
        self._ai = dict(it) if it else {"id": None, "hora": "07:00", "dias": list(range(7)),
                                        "acao": "ligar", "ligado": True}
        self._ai["dias"] = list(self._ai["dias"])
        self._titulo_tela(t, "Horário", voltar_para="agenda")
        corpo = tk.Frame(t, bg=E.FUNDO)
        corpo.pack(fill="both", expand=True)
        esq, dir_ = self._duas_colunas(corpo)
        p = Painel(esq)
        p.pack(fill="x")
        self._cabeca_painel(p, "Hora")
        h, m = (int(x) for x in self._ai["hora"].split(":"))
        self.p_h = Passo(p.dentro, h, 0, 23, 1, lambda v: f"{v:02d} h", lambda v: self._ai_hora(h=v),
                         largura_texto=60)
        self.p_h.pack(anchor="w", pady=(E.px(8), 0))
        self.p_m = Passo(p.dentro, m, 0, 55, 5, lambda v: f"{v:02d} min", lambda v: self._ai_hora(m=v),
                         largura_texto=60)
        self.p_m.pack(anchor="w", pady=(E.px(6), 0))
        p = Painel(esq)
        p.pack(fill="x", pady=(E.px(10), 0))
        self._cabeca_painel(p, "Fazer")
        self.b_acao = Botao(p.dentro, texto=self._rotulo_acao(self._ai["acao"]), icone="seta_baixo",
                            ao_clicar=self._ai_menu_acao, largura=190, altura=34, raio=8,
                            fundo=E.CAMADA_1, tam=10, peso="normal", lado_icone=14)
        self.b_acao.pack(fill="x", pady=(E.px(8), 0))
        p = Painel(dir_)
        p.pack(fill="x")
        self._cabeca_painel(p, "Dias")
        grade = tk.Frame(p.dentro, bg=E.CAMADA_1)
        grade.pack(fill="x", pady=(E.px(8), 0))
        for d, nome in enumerate(DIAS):
            b = Botao(grade, texto=nome, largura=48, altura=32, raio=8, fundo=E.CAMADA_1, tam=9.5, peso="normal")
            b.ao_clicar = (lambda d=d, b=b: self._ai_dia(d, b))
            b.definir(selecionado=d in self._ai["dias"], ms=0)
            b.grid(row=d // 4, column=d % 4, sticky="ew", padx=(0, E.px(4)), pady=(0, E.px(4)))
        for c in range(4):
            grade.grid_columnconfigure(c, weight=1)
        Botao(dir_, texto="Salvar", ao_clicar=self._ai_salvar, largura=150, altura=38, fundo=E.FUNDO,
              tam=10.5).pack(side="bottom", fill="x")
        if aid:
            Botao(dir_, texto="Excluir", icone="lixo", ao_clicar=self._ai_excluir, largura=150, altura=38,
                  fundo=E.FUNDO, cor=E.FUNDO, cor_texto=E.ERRO, tam=10.5,
                  lado_icone=14).pack(side="bottom", fill="x", pady=(0, E.px(6)))

    def _ai_hora(self, h=None, m=None):
        hh, mm = (int(x) for x in self._ai["hora"].split(":"))
        self._ai["hora"] = f"{hh if h is None else h:02d}:{mm if m is None else m:02d}"

    def _ai_dia(self, d, b):
        dias = self._ai["dias"]
        if d in dias:
            if len(dias) == 1:
                return
            dias.remove(d)
        else:
            dias.append(d)
        b.definir(selecionado=d in dias)

    def _ai_menu_acao(self):
        def escolher(a):
            self._ai["acao"] = a
            self.b_acao.definir(texto=self._rotulo_acao(a))
        Menu(self.b_acao, [(n, (lambda a=a: escolher(a))) for n, a in self._opcoes_acao()],
             marcado=self._rotulo_acao(self._ai["acao"]), alinhar="direita")

    def _ai_salvar(self):
        if self._ai["id"]:
            for i, it in enumerate(self.cfg["agenda"]):
                if it["id"] == self._ai["id"]:
                    self.cfg["agenda"][i] = self._ai
        else:
            self._ai["id"] = novo_id()
            self.cfg["agenda"].append(self._ai)
        self.cfg.gravar()
        aid = self._ai["id"]
        self.ir("agenda")
        if aid in self._linhas_ag:                   # o horario salvo fica a vista
            self.rol_agenda.mostrar(self._linhas_ag[aid])

    def _ai_excluir(self):
        self.cfg["agenda"] = [i for i in self.cfg["agenda"] if i["id"] != self._ai["id"]]
        self.cfg.gravar()
        self.ir("agenda")

    # -- opcoes ------------------------------------------------------------------------
    def _linha(self, pai, nome, legenda):
        f = tk.Frame(pai, bg=E.CAMADA_1)
        f.pack(fill="x", pady=(E.px(10), 0))
        tx = tk.Frame(f, bg=E.CAMADA_1)
        tx.pack(side="left", fill="x", expand=True)
        tk.Label(tx, text=nome, bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10.5), anchor="w").pack(fill="x")
        if legenda:
            tk.Label(tx, text=legenda, bg=E.CAMADA_1, fg=E.TEXTO_2, font=E.fonte(9), anchor="w",
                     justify="left", wraplength=E.px(250)).pack(fill="x")
        return f

    def _tela_opcoes(self, t):
        """Uma coluna larga que rola: Programa, Lâmpada, Atalhos."""
        self._titulo_tela(t, "Opções")
        rol = self.rol_opcoes = Rolagem(t, fundo=E.FUNDO)
        rol.pack(fill="both", expand=True)
        area = rol.dentro
        p = Painel(area)
        p.pack(fill="x")
        self._cabeca_painel(p, "Programa")
        f = self._f_win = self._linha(p.dentro, "Iniciar com o Windows", "abre escondido na bandeja")
        win = inicio_windows.ativo()
        Chave(f, win, self._opc_windows).pack(side="right")
        # sub-opcao: so aparece com a de origem ligada (padrao da casa)
        self.f_rapido = tk.Frame(p.dentro, bg=E.CAMADA_1)
        f = self._linha(self.f_rapido, "Iniciar mais rápido", "junto com o login; pede o administrador uma vez")
        f.pack_configure(padx=(E.px(16), 0))
        self.ch_rapido = Chave(f, win and inicio_windows.rapido(), self._opc_rapido)
        self.ch_rapido.pack(side="right")
        if win:
            self.f_rapido.pack(fill="x")
        f = self._linha(p.dentro, "Iniciar minimizado", "abre direto na bandeja")
        Chave(f, bool(self.cfg.opcoes.get("comecar_na_bandeja")), self._opc_minimizado).pack(side="right")

        p = Painel(area)
        p.pack(fill="x", pady=(E.px(10), 0))
        self._cabeca_painel(p, "Lâmpada")
        f = self._linha(p.dentro, "Ao conectar", "o que fazer assim que o programa abre")
        self.b_ao_abrir = Botao(f, texto=self._rotulo_acao(self.cfg.opcoes.get("ao_abrir", "nada")),
                                icone="seta_baixo", ao_clicar=self._opc_menu_ao_abrir, largura=150, altura=32,
                                raio=8, fundo=E.CAMADA_1, tam=9.5, peso="normal", lado_icone=12)
        self.b_ao_abrir.pack(side="right")
        f = self._linha(p.dentro, "Ao sair", "ao fechar o programa (segurar o X)")
        self.b_ao_sair = Botao(f, texto=self._rotulo_acao(self.cfg.opcoes.get("ao_sair", "nada")),
                               icone="seta_baixo", ao_clicar=self._opc_menu_ao_sair, largura=150, altura=32,
                               raio=8, fundo=E.CAMADA_1, tam=9.5, peso="normal", lado_icone=12)
        self.b_ao_sair.pack(side="right")
        configurada = self.cfg.completo
        f = self._linha(p.dentro, "Qual lâmpada",
                        f"{self.cfg['nome'] or 'lâmpada'} · …{self.cfg['id'][-6:]}" if configurada
                        else "nenhuma configurada")
        Botao(f, texto="Trocar" if configurada else "Configurar", icone="chave", ao_clicar=lambda: self.ir("lampada"),
              largura=150, altura=32, raio=8, fundo=E.CAMADA_1, tam=10, lado_icone=14).pack(side="right")
        f = self._linha(p.dentro, "Chave", "a lâmpada foi resetada? pegue a nova pela conta Tuya")
        Botao(f, texto="Atualizar chave", ao_clicar=lambda: self._conta_abrir("opcoes"),
              largura=150, altura=32, raio=8, fundo=E.CAMADA_1, tam=10).pack(side="right")

        p = Painel(area)
        p.pack(fill="x", pady=(E.px(10), 0))
        cab, _ = self._cabeca_painel(p, "Atalhos", "Ctrl, Alt ou Win + uma tecla")
        self.b_novo_atalho = Botao(cab, icone="mais", ao_clicar=self._atalho_menu, largura=28, altura=28,
                                   raio=8, fundo=E.CAMADA_1, cor=E.CAMADA_1, lado_icone=14, dica="Novo atalho")
        self.b_novo_atalho.pack(side="right")
        self.f_atalhos = tk.Frame(p.dentro, bg=E.CAMADA_1)
        self.f_atalhos.pack(fill="x")
        self._montar_atalhos()
        tk.Label(area, text=f"LightWireless {__version__} · relatórios em relatorios\\", bg=E.FUNDO,
                 fg=E.APAGADO, font=E.fonte(8.5)).pack(anchor="w", pady=(E.px(8), 0))

    def _opc_menu_ao_abrir(self):
        def escolher(a):
            self.cfg.opcoes["ao_abrir"] = a
            self.cfg.gravar()
            self.b_ao_abrir.definir(texto=self._rotulo_acao(a))
        Menu(self.b_ao_abrir, [(n, (lambda a=a: escolher(a))) for n, a in self._opcoes_acao(True)],
             marcado=self._rotulo_acao(self.cfg.opcoes.get("ao_abrir", "nada")), alinhar="direita")

    def _opc_windows(self, v):
        ok = inicio_windows.definir(v)
        if ok:
            self.cfg.opcoes["iniciar_com_windows"] = v
            self.cfg.gravar()
            if v:
                self.f_rapido.pack(fill="x", after=self._f_win)
                self.ch_rapido.definir(inicio_windows.rapido())
            else:
                self.f_rapido.pack_forget()
                self.ch_rapido.definir(False)
            self._medir()
        return ok

    def _opc_minimizado(self, v):
        self.cfg.opcoes["comecar_na_bandeja"] = v
        return self.cfg.gravar()

    def _opc_menu_ao_sair(self):
        def escolher(a):
            self.cfg.opcoes["ao_sair"] = a
            self.cfg.gravar()
            self.b_ao_sair.definir(texto=self._rotulo_acao(a))
        Menu(self.b_ao_sair, [(n, (lambda a=a: escolher(a))) for n, a in self._opcoes_acao(True)],
             marcado=self._rotulo_acao(self.cfg.opcoes.get("ao_sair", "nada")), alinhar="direita")

    def _opc_rapido(self, v):
        ok = inicio_windows.definir_rapido(v)
        log.info("iniciar mais rapido %s: %s", v, "ok" if ok else "falhou")
        return ok


    def _montar_atalhos(self):
        for w in self.f_atalhos.winfo_children():
            w.destroy()
        falhas = self.motor.falhas() if hasattr(self, "motor") else set()
        lista = AT.em_ordem(self.cfg)
        self._linhas_atalho = {}
        if not lista:
            tk.Label(self.f_atalhos, text="Nenhum atalho. Use o + para criar.", bg=E.CAMADA_1,
                     fg=E.TEXTO_2, font=E.fonte(9.5), anchor="w").pack(fill="x", pady=(E.px(8), 0))
        for acao, teclas in lista:
            f = tk.Frame(self.f_atalhos, bg=E.CAMADA_1)
            f.pack(fill="x", pady=(E.px(8), 0))
            tk.Label(f, text=AT.rotulo(acao), bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10),
                     anchor="w").pack(side="left")
            Botao(f, icone="lixo", ao_clicar=lambda a=acao: self._atalho_tirar(a), largura=28, altura=30,
                  raio=8, fundo=E.CAMADA_1, cor=E.CAMADA_1, lado_icone=13, dica="Tirar").pack(side="right")
            b = Botao(f, texto=teclas or "definir", largura=170, altura=30, raio=8, fundo=E.CAMADA_1,
                      tam=9.5, peso="normal",
                      cor_texto=E.ERRO if acao in falhas else (E.TEXTO if teclas else E.TEXTO_2),
                      dica="Clique e aperte as teclas" if acao not in falhas else
                      "O Windows recusou: outro programa já usa")
            b.ao_clicar = (lambda a=acao, b=b: self._atalho_capturar(a, b))
            b.pack(side="right", padx=(0, E.px(4)))
            self._linhas_atalho[acao] = (f, b)
        if self._tela == "opcoes":
            self._medir()

    def _atalho_menu(self):
        livres = AT.livres(self.cfg)
        if not livres:
            return

        def novo(a):
            # a linha nova fica a vista e ja pede as teclas (bug "o + nao faz nada", 10/out)
            self.cfg.atalhos[a] = ""
            self._montar_atalhos()
            f, b = self._linhas_atalho[a]
            self.rol_opcoes.mostrar(f)
            self.raiz.after(60, lambda: self._atalho_capturar(a, b))
        Menu(self.b_novo_atalho, [(AT.rotulo(a), (lambda a=a: novo(a))) for a in livres], alinhar="direita")

    def _atalho_tirar(self, a):
        self.cfg.atalhos.pop(a, None)
        self.cfg.gravar()
        self.motor.aplicar(AT.guardados(self.cfg))
        self._montar_atalhos()

    def _atalho_capturar(self, acao, b):
        if self._capturando and self._capturando[1] is b:
            return
        self._capturando = (acao, b, set())
        b.definir(texto="aperte as teclas…")
        log.info("capturando teclas para %s", acao)
        C.ativar(self.raiz, b)
        b.bind("<KeyPress>", self._cap_tecla)
        b.bind("<KeyRelease>", self._cap_solta)
        b.bind("<space>", self._cap_tecla)
        b.bind("<Return>", self._cap_tecla)
        self.raiz.after(300, lambda: self._capturando and self._capturando[1] is b and
                        b.bind("<FocusOut>", lambda _e=None: self._cap_fim(None), add="+"))
        self._cap_vigiar(b)

    def _cap_vigiar(self, b, antes=None):
        """RESERVA da captura (11/out/2026, teclado US-Internacional): tecla de
        acento (' ") e pontuacao com AltGr NAO chegam ao Tk -- o log dele so
        tinha "capturando", nenhuma tecla. So enquanto captura, le direto do
        Windows quais teclas de pontuacao desceram e segue o caminho normal."""
        if not (self._capturando and self._capturando[1] is b):
            return
        try:
            import ctypes
            gaks = ctypes.windll.user32.GetAsyncKeyState
            agora = {vk for vk in AT.TECLAS_OEM if gaks(vk) & 0x8000}
            if antes is not None:
                for vk in sorted(agora - antes):
                    presos = self._capturando[2]
                    for vk_m, nome in ((0x11, "Control_L"), (0x12, "Alt_L"), (0x10, "Shift_L"),
                                       (0x5B, "Win_L"), (0x5C, "Win_L")):
                        if gaks(vk_m) & 0x8000:
                            presos.add(nome)
                    log.info("captura pela reserva: tecla 0x%X", vk)
                    self._cap_tecla(SimpleNamespace(keysym="", keycode=vk))
                    if not (self._capturando and self._capturando[1] is b):
                        return
        except Exception:
            log.exception("reserva da captura")
            return
        self.raiz.after(30, lambda: self._cap_vigiar(b, agora))

    def _cap_tecla(self, e):
        if not self._capturando:
            return
        acao, b, presos = self._capturando
        if e.keysym == "Escape":
            self._cap_fim(None)
            return "break"
        if AT.e_modificador(e.keysym):
            presos.add(e.keysym)
            b.definir(texto=AT.escrever(presos) + "+…")
            return "break"
        tecla = AT.tecla_fisica(e.keysym, getattr(e, "keycode", 0) or 0)
        teclas = AT.escrever(presos, tecla)
        motivo = AT.porque_nao(presos, tecla)
        dono = AT.quem_usa(self.cfg, teclas, menos=acao)
        if not motivo and not dono and not livre_no_windows(teclas, self.cfg.atalhos.get(acao, "")):
            motivo = "outro programa"
        if motivo or dono:
            curto = ("use Ctrl/Alt/Win + tecla" if "Falta Ctrl" in motivo or "Falta a tecla" in motivo
                     else f"já usado: {AT.rotulo(dono)}" if dono
                     else "outro programa já usa" if motivo == "outro programa" else "tecla não aceita")
            b.cor_texto = E.ERRO
            b.definir(texto=curto)
            log.info("atalho recusado: %s (%s)", teclas, motivo or "ja usado")

            def voltar():
                if self._capturando and self._capturando[1] is b:
                    b.cor_texto = E.TEXTO
                    b.definir(texto="aperte as teclas…")
            self.raiz.after(1600, voltar)
            return "break"
        self._cap_fim(teclas)
        return "break"

    def _cap_solta(self, e):
        if self._capturando and AT.e_modificador(e.keysym):
            self._capturando[2].discard(e.keysym)
        return "break"

    def _cap_fim(self, teclas):
        if not self._capturando:
            return
        acao, b, _ = self._capturando
        self._capturando = None
        log.info("captura de %s: %s", acao, teclas or "cancelada")
        for seq in ("<KeyPress>", "<KeyRelease>", "<space>", "<Return>"):
            b.unbind(seq)
        if teclas:
            self.cfg.atalhos[acao] = teclas
            self.cfg.gravar()
            self.motor.aplicar(AT.guardados(self.cfg))
            self.raiz.after(300, self._montar_atalhos)
        else:
            self.raiz.after(10, self._montar_atalhos)

    # -- lampada: achar na rede + chave (substitui a antiga "primeira vez") ----------
    # A chave nao viaja pela rede: vem da nuvem da Tuya, colada, ou do
    # devices.json do assistente. A varredura acha o aparelho (id + ip).
    def _procurar_na_rede(self):
        if self.cl["procurando"]:
            return
        self.cl.update(procurando=True, achados=[], tentativa=(0, 0), recado="", ja_procurou=True)

        def varrer():
            try:
                achados = descoberta.procurar_insistindo(
                    ao_tentar=lambda c, t: self._busca.put(("tentativa", c, t)))
            except Exception:
                achados = []
            self._busca.put(("achados", achados))
        threading.Thread(target=varrer, daemon=True).start()

    def _tela_lampada(self, t):
        if not self.cl["ja_procurou"]:
            self._procurar_na_rede()
        self._titulo_tela(t, "Lâmpada", voltar_para="inicio")
        corpo = tk.Frame(t, bg=E.FUNDO)
        corpo.pack(fill="both", expand=True)
        esq, dir_ = self._duas_colunas(corpo)
        # esquerda: o que a rede mostrou
        p = Painel(esq, encher=True)
        p.pack(fill="both", expand=True)
        self._cabeca_painel(p, "Na rede")
        if self.cl["procurando"]:
            c, tot = self.cl["tentativa"]
            self._texto(p.dentro, "Escutando a rede… os aparelhos se anunciam sozinhos, de tempos em tempos.", pady=(E.px(8), 0))
            if c > 1:
                self._texto(p.dentro, f"tentativa {c} de {tot}", cor=E.APAGADO, tam=8.5, pady=(E.px(4), 0))
        elif self.cl["achados"]:
            for a in self.cl["achados"]:
                f = tk.Frame(p.dentro, bg=E.CAMADA_1)
                f.pack(fill="x", pady=(E.px(8), 0))
                tk.Label(f, image=E.icone("marca", E.px(18), E.ACENTO if descoberta.compativel(a) else E.APAGADO),
                         bg=E.CAMADA_1).pack(side="left", padx=(0, E.px(8)))
                tx = tk.Frame(f, bg=E.CAMADA_1)
                tx.pack(side="left", fill="x", expand=True)
                tk.Label(tx, text=descoberta.descrever(a), bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10),
                         anchor="w").pack(fill="x")
                tk.Label(tx, text="pronta para conectar" if descoberta.compativel(a)
                         else "fala outra versão do protocolo", bg=E.CAMADA_1,
                         fg=E.TEXTO_2 if descoberta.compativel(a) else E.ALERTA, font=E.fonte(9),
                         anchor="w").pack(fill="x")
        else:
            self._texto(p.dentro, f"Não achei nenhuma. Escutei {descoberta.CICLOS_PADRAO * descoberta.SEGUNDOS_PADRAO} s: "
                        "a lâmpada está ligada? o PC está na mesma rede?",
                        pady=(E.px(8), 0))
        Botao(p.dentro, texto="Procurando…" if self.cl["procurando"] else "Procurar de novo",
              ao_clicar=self._lampada_procurar, largura=190, altura=34, raio=8, fundo=E.CAMADA_1,
              tam=10).pack(side="bottom", fill="x")
        # direita: de onde vem a chave
        p = Painel(dir_, encher=True)
        p.pack(fill="both", expand=True)
        self._cabeca_painel(p, "Chave", "vem uma vez só")
        self._texto(p.dentro, "Ela não viaja pela rede: existe só na nuvem do fabricante.", pady=(E.px(8), E.px(2)))
        for texto, acao, dica in (
                ("Entrar com conta Tuya", lambda: self._conta_abrir("lampada"), "User Code + QR no app Tuya Smart"),
                ("Colar a chave", lambda: self.ir("lampada_colar"), "16 caracteres"),
                ("Usar o devices.json", self._lampada_arquivo, "arquivo do assistente do tinytuya"),
                ("Portal de desenvolvedor", lambda: self.ir("lampada_nuvem"), "Access ID e Secret do projeto na Tuya")):
            Botao(p.dentro, texto=texto, ao_clicar=acao, largura=200, altura=34, raio=8, fundo=E.CAMADA_1,
                  tam=10, dica=dica).pack(fill="x", pady=(E.px(5), 0))
        if self.cl["recado"]:
            self._texto(p.dentro, self.cl["recado"], cor=E.ERRO, pady=(E.px(8), 0))

    def _lampada_procurar(self):
        self._procurar_na_rede()
        self.ir("lampada")

    def _id_da_rede(self) -> str:
        servem = [a for a in self.cl["achados"] if a.get("id")]
        return servem[0]["id"] if servem else ""

    def _ip_da_rede(self, id_: str) -> str:
        return next((a.get("ip", "") for a in self.cl["achados"] if a.get("id") == id_), "")

    def _entrada(self, pai, valor, segredo=False):
        e = tk.Entry(pai, bg=E.CAMADA_2, fg=E.TEXTO, insertbackground=E.TEXTO, relief="flat",
                     font=E.fonte(10.5), highlightthickness=1, highlightbackground=E.BORDA_1,
                     highlightcolor=E.FOCO, selectbackground=E.ACENTO, selectforeground="#FFFFFF",
                     show="•" if segredo else "")
        e.insert(0, valor)
        e.pack(fill="x", pady=(E.px(4), 0), ipady=E.px(4))
        return e

    def _tela_lampada_nuvem(self, t):
        self._titulo_tela(t, "Buscar na nuvem", voltar_para="lampada")
        corpo = tk.Frame(t, bg=E.FUNDO)
        corpo.pack(fill="both", expand=True)
        dir_, esq = self._duas_colunas(corpo, esq=170)        # campos a esquerda (largo), botoes a direita
        dir_.grid_configure(column=1, padx=(E.px(10), 0))
        esq.grid_configure(column=0, padx=0)
        corpo.grid_columnconfigure(0, weight=1, minsize=0)
        corpo.grid_columnconfigure(1, weight=0, minsize=E.px(170))
        p = Painel(esq, encher=True)
        p.pack(fill="both", expand=True)
        self._cabeca_painel(p, "Projeto na Tuya")
        self._texto(p.dentro, "Os dois campos ficam em iot.tuya.com › Project Overview.", pady=(E.px(4), 0))
        v = self.cl["valores"]
        nv = self.cfg["nuvem"]
        self._texto(p.dentro, "Access ID", cor=E.TEXTO, tam=9.5, pady=(E.px(8), 0))
        self.e_api_id = self._entrada(p.dentro, v.get("id", nv.get("api_id", "")))
        self._texto(p.dentro, "Access Secret", cor=E.TEXTO, tam=9.5, pady=(E.px(8), 0))
        self.e_api_secret = self._entrada(p.dentro, v.get("secret", nv.get("api_secret", "")), segredo=True)
        if self.cl["recado"]:
            self._texto(p.dentro, self.cl["recado"], cor=E.ERRO, pady=(E.px(8), 0))
        p = Painel(dir_)
        p.pack(fill="x")
        self._cabeca_painel(p, "Região")
        self.b_regiao = Botao(p.dentro, texto=nuvem.REGIOES.get(self.cl["regiao"], self.cl["regiao"]),
                              icone="seta_baixo", ao_clicar=self._nuvem_menu_regiao, largura=140, altura=32,
                              raio=8, fundo=E.CAMADA_1, tam=9.5, peso="normal", lado_icone=12)
        self.b_regiao.pack(fill="x", pady=(E.px(8), 0))
        self.b_buscar = Botao(dir_, texto="Buscando…" if self.cl["consultando"] else "Buscar",
                              ao_clicar=self._nuvem_buscar, largura=150, altura=38, fundo=E.FUNDO, tam=10.5)
        self.b_buscar.pack(side="bottom", fill="x")
        if self.cl["consultando"]:
            self.b_buscar.definir(habilitado=False, ms=0)

    def _nuvem_guardar(self):
        for nome, chave in (("e_api_id", "id"), ("e_api_secret", "secret")):
            e = getattr(self, nome, None)
            if e is not None and e.winfo_exists():
                self.cl["valores"][chave] = e.get().strip()

    def _nuvem_menu_regiao(self):
        def escolher(cod):
            self.cl["regiao"] = cod
            self.b_regiao.definir(texto=nuvem.REGIOES[cod])
        Menu(self.b_regiao, [(n, (lambda c=c: escolher(c))) for c, n in nuvem.REGIOES.items()],
             marcado=nuvem.REGIOES.get(self.cl["regiao"]), alinhar="direita")

    def _nuvem_buscar(self):
        if self.cl["consultando"]:
            return
        self._nuvem_guardar()
        self.cl.update(consultando=True, recado="")
        v, regiao, id_rede = dict(self.cl["valores"]), self.cl["regiao"], self._id_da_rede()
        id_ap = id_rede or self.cfg["id"]

        def na_thread():
            try:
                self._busca.put(("nuvem",) + tuple(nuvem.buscar(regiao, v.get("id", ""), v.get("secret", ""), id_ap)))
            except Exception:
                log.exception("busca na nuvem")
                self._busca.put(("nuvem", [], "Não consegui consultar a nuvem."))
        threading.Thread(target=na_thread, daemon=True).start()
        self.ir("lampada_nuvem")

    def _nuvem_chegou(self, aparelhos, recado):
        self.cl["consultando"] = False
        if recado:
            self.cl["recado"] = recado
        else:
            escolhido = None
            for a in self.cl["achados"]:
                escolhido = nuvem.achar(aparelhos, a.get("id", "")) if a.get("id") else None
                if escolhido:
                    break
            if escolhido is None and len(aparelhos) == 1:
                escolhido = aparelhos[0]
            if escolhido is None:
                self.cl["recado"] = (f"A nuvem devolveu {len(aparelhos)} aparelhos e nenhum é o desta rede. "
                                     "O app do celular vinculado é o desta lâmpada?")
            else:
                v = self.cl["valores"]
                self._usar_lampada(escolhido["id"], escolhido["chave"], escolhido.get("nome", ""),
                                   self._ip_da_rede(escolhido["id"]),
                                   {"regiao": self.cl["regiao"], "api_id": v.get("id", ""),
                                    "api_secret": v.get("secret", "")})
                return
        if self._tela == "lampada_nuvem":
            self.ir("lampada_nuvem")

    def _tela_lampada_colar(self, t):
        self._titulo_tela(t, "Colar a chave", voltar_para="lampada")
        p = Painel(t, encher=True)
        p.pack(fill="both", expand=True)
        id_rede = self._id_da_rede() or self.cfg["id"]
        self._cabeca_painel(p, "Chave local", f"aparelho …{id_rede[-6:]}" if id_rede else "")
        if not id_rede:
            self._texto(p.dentro, "Primeiro a lâmpada tem que aparecer na rede (tela anterior): "
                        "a chave precisa do identificador dela.", cor=E.ALERTA, pady=(E.px(8), 0))
        self._texto(p.dentro, "Sem máscara de propósito: confira se colou inteira (16 caracteres).", pady=(E.px(8), 0))
        self.e_chave = self._entrada(p.dentro, self.cl["valores"].get("chave", ""))
        self.e_chave.bind("<Return>", lambda _e=None: self._colar_salvar())
        self.l_colar = self._texto(p.dentro, self.cl["recado"], cor=E.ERRO, pady=(E.px(8), 0))
        Botao(p.dentro, texto="Salvar", ao_clicar=self._colar_salvar, largura=150, altura=38,
              fundo=E.CAMADA_1, tam=10.5).pack(side="bottom", anchor="e")
        self.raiz.after(50, lambda: self.e_chave.winfo_exists() and self.e_chave.focus_set())

    def _colar_salvar(self):
        limpa = "".join(self.e_chave.get().split())
        self.cl["valores"]["chave"] = limpa
        id_ = self._id_da_rede() or self.cfg["id"]
        if not limpa:
            erro = "Cole a chave da lâmpada no campo."
        elif len(limpa) != TAMANHO_DA_CHAVE:
            erro = f"A chave tem {TAMANHO_DA_CHAVE} caracteres; essa tem {len(limpa)}. Copiou inteira?"
        elif not id_:
            erro = "A lâmpada ainda não apareceu na rede: volte e procure de novo."
        else:
            self._usar_lampada(id_, limpa, self.cfg["nome"] if id_ == self.cfg["id"] else "",
                               self._ip_da_rede(id_) or (self.cfg["ip"] if id_ == self.cfg["id"] else ""))
            return
        self.l_colar.config(text=erro)

    def _lampada_arquivo(self):
        from tkinter import filedialog
        caminho = filedialog.askopenfilename(
            parent=self.raiz, title="Escolha o devices.json do assistente",
            filetypes=[("Arquivo do assistente", "devices.json"), ("JSON", "*.json"), ("Todos", "*.*")])
        if not caminho:
            return
        achados = [a for a in self.cl["achados"] if a.get("id")]
        d = de_arquivo_do_wizard(caminho, achados[0]["id"] if len(achados) == 1 else "")
        if not d:
            self.cl["recado"] = "Esse arquivo não tem nenhuma lâmpada com chave. É mesmo o devices.json?"
            self.ir("lampada")
            return
        self._usar_lampada(d["id"], d["chave"], d["nome"], self._ip_da_rede(d["id"]) or d["ip"])

    # -- conta Tuya: User Code + QR (conta_tuya.py) -------------------------------
    # Maquina de estados explicita em self.lg["estado"]:
    #   digitando -> pedindo -> aguardando -> buscando -> (escolher) -> gravado
    #   qualquer um -> erro (com a acao certa) ; cancelar -> digitando
    # A rede so na Sessao (thread); ela fala pela fila self._conta, lida no laco.
    def _conta_abrir(self, voltar="lampada"):
        self._conta_cancelar()
        self.lg.update(estado="digitando", voltar=voltar, erro=("", ""), conta=[], gravado=None)
        self.ir("lampada_conta")

    def _conta_cancelar(self):
        if self._sessao is not None:
            self._sessao.cancelar()
            self._sessao = None
            conta_tuya.relatar("cancelado")
        if self.lg["estado"] in ("pedindo", "aguardando", "buscando", "escolher"):
            self.lg["estado"] = "digitando"
        self.lg.update(conta=[], conta_pendente=None)   # a lista tem chaves: nao fica na memoria

    def _conta_ir(self, estado, **kw):
        self.lg.update(estado=estado, **kw)
        if self._tela == "lampada_conta":
            self.ir("lampada_conta")

    def _tela_lampada_conta(self, t):
        g = self.lg
        st = g["estado"]
        self._titulo_tela(t, "Entrar com conta Tuya", voltar_para=g["voltar"])
        corpo = tk.Frame(t, bg=E.FUNDO)
        corpo.pack(fill="both", expand=True)
        esq, dir_ = self._duas_colunas(corpo)
        # esquerda: o passo a passo (o mesmo em todos os estados, o atual em destaque)
        p = Painel(esq, encher=True)
        p.pack(fill="both", expand=True)
        self._cabeca_painel(p, "Como funciona")
        passos = (("digitando", "1. No app Tuya Smart: Eu › ⚙ › Conta e segurança › User Code. Digite aqui."),
                  ("aguardando", "2. No app, toque no leitor (canto de cima da tela inicial) e aponte para o QR."),
                  ("confirmar", "3. Toque em Confirmar login. O programa pega a chave e grava."))
        atual = {"digitando": "digitando", "erro": "digitando", "pedindo": "aguardando",
                 "aguardando": "aguardando", "buscando": "confirmar", "escolher": "confirmar",
                 "gravado": "confirmar"}[st]
        for chave, txt in passos:
            self._texto(p.dentro, txt, cor=E.TEXTO if chave == atual else E.TEXTO_2, pady=(E.px(8), 0))
        if st in ("pedindo", "aguardando", "buscando"):     # o Cancelar ocupa o lugar da nota
            Botao(p.dentro, texto="Cancelar", ao_clicar=self._conta_cancelar_tela, largura=190, altura=34,
                  raio=8, fundo=E.CAMADA_1, tam=10).pack(side="bottom", fill="x")
        else:
            self._texto(p.dentro, "A nuvem é usada só agora. Senha, login e User Code não são guardados.",
                        cor=E.APAGADO, tam=8.5, pady=(E.px(10), 0))
        # direita: o estado
        p = Painel(dir_, encher=True)
        p.pack(fill="both", expand=True)
        getattr(self, "_conta_" + st)(p)

    def _conta_cancelar_tela(self):
        self._conta_cancelar()
        self._conta_ir("digitando")

    def _conta_digitando(self, p, erro=""):
        self._cabeca_painel(p, "User Code")
        self.e_user_code = self._entrada(p.dentro, self.lg["user_code"])
        self.e_user_code.bind("<Return>", lambda _e=None: self._conta_gerar())
        if erro:
            self._texto(p.dentro, erro, cor=E.ERRO, pady=(E.px(8), 0))
        self.b_gerar = Botao(p.dentro, texto="Gerar QR", ao_clicar=self._conta_gerar, largura=150, altura=38,
                             fundo=E.CAMADA_1, tam=10.5)
        self.b_gerar.pack(side="bottom", anchor="e")
        self.raiz.after(50, lambda: self.e_user_code.winfo_exists() and self.e_user_code.focus_set())

    def _conta_erro(self, p):
        tipo, texto = self.lg["erro"]
        if tipo in ("user_code", "rede", "sdk"):
            self._conta_digitando(p, texto)      # corrige o code ou so tenta de novo
            return
        self._cabeca_painel(p, "Não deu certo")
        self._texto(p.dentro, texto, cor=E.ERRO, pady=(E.px(8), 0))
        rot = {"expirado": "Gerar novo QR", "vazia": "Tentar de novo"}.get(tipo, "Tentar de novo")
        self.b_gerar = Botao(p.dentro, texto=rot, ao_clicar=self._conta_gerar, largura=150, altura=38,
                             fundo=E.CAMADA_1, tam=10.5)
        self.b_gerar.pack(side="bottom", anchor="e")

    def _conta_pedindo(self, p):
        self._cabeca_painel(p, "QR")
        self._texto(p.dentro, "Pedindo o QR à Tuya…", pady=(E.px(8), 0))

    def _conta_aguardando(self, p):
        self._cabeca_painel(p, "Escaneie no Tuya Smart", f"QR {self.lg['qr_n']}" if self.lg["qr_n"] > 1 else None)
        lado = E.px(186)
        cv = tk.Canvas(p.dentro, width=lado, height=lado, bg="#FFFFFF", highlightthickness=0, bd=0)
        cv.pack(pady=(E.px(8), 0))
        desenhar_qr(cv, self.lg["qr"], lado)
        self.l_qr_resta = tk.Label(p.dentro, text="", bg=E.CAMADA_1, fg=E.TEXTO_2, font=E.fonte(9))
        self.l_qr_resta.pack(pady=(E.px(6), 0))
        self._conta_relogio()

    def _conta_relogio(self):
        lb = getattr(self, "l_qr_resta", None)
        if self.lg["estado"] != "aguardando" or lb is None or not lb.winfo_exists():
            return
        resta = max(0, int(self.lg["qr_ate"] - time.monotonic()))
        txt = f"esperando a confirmação · {resta // 60}:{resta % 60:02d}"
        if lb.cget("text") != txt:
            lb.config(text=txt)

    def _conta_buscando(self, p):
        self._cabeca_painel(p, "Login feito")
        self._texto(p.dentro, "Lendo os aparelhos da sua conta…", pady=(E.px(8), 0))

    def _conta_escolher(self, p):
        self._cabeca_painel(p, "Qual é a lâmpada?")
        if self.lg["erro"][1]:                  # em cima da lista: nunca fica escondido embaixo
            self._texto(p.dentro, self.lg["erro"][1], cor=E.ERRO, pady=(E.px(6), 0))
        rol = Rolagem(p.dentro)
        rol.pack(fill="both", expand=True, pady=(E.px(6), 0))
        for d in self.lg["conta"]:
            f = tk.Frame(rol.dentro, bg=E.CAMADA_1)
            f.pack(fill="x", pady=(E.px(4), 0))
            Botao(f, texto="Usar", ao_clicar=lambda d=d: self._conta_usar(d), largura=64, altura=30, raio=8,
                  fundo=E.CAMADA_1, tam=9.5).pack(side="right")
            tx = tk.Frame(f, bg=E.CAMADA_1)
            tx.pack(side="left", fill="x", expand=True)
            tk.Label(tx, text=d["nome"] or "(sem nome)", bg=E.CAMADA_1, fg=E.TEXTO, font=E.fonte(10),
                     anchor="w").pack(fill="x")
            leg = ["lâmpada" if d.get("categoria") == conta_tuya.CATEGORIA_LAMPADA else (d.get("categoria") or "?"),
                   "nesta rede" if d.get("na_rede") else "não vista na rede", conta_tuya.fim(d["id"])]
            lb = tk.Label(tx, text=" · ".join(leg), bg=E.CAMADA_1, fg=E.ACENTO if d.get("na_rede") else E.TEXTO_2,
                          font=E.fonte(8.5), anchor="w", justify="left")
            lb.pack(fill="x")
            quebrar(tx, lb)
        rol.atualizar()

    def _conta_gravado(self, p):
        d = self.lg["gravado"] or {}
        self._cabeca_painel(p, "Pronto")
        self._texto(p.dentro, f"{d.get('nome') or 'Lâmpada'} gravada ({conta_tuya.fim(d.get('id', ''))}"
                    f"{', ' + d['ip'] if d.get('ip') else ''}).", cor=E.TEXTO, pady=(E.px(8), 0))
        self._texto(p.dentro, "Daqui pra frente o programa fala só com ela, na rede de casa. O rodapé mostra a conexão.",
                    pady=(E.px(6), 0))
        Botao(p.dentro, texto="Ir para o início", ao_clicar=lambda: self.ir("inicio"), largura=150, altura=38,
              fundo=E.CAMADA_1, tam=10.5).pack(side="bottom", anchor="e")

    def _conta_gerar(self):
        if self.lg["estado"] not in ("digitando", "erro"):
            return                                # sem clique duplo
        e = getattr(self, "e_user_code", None)
        if e is not None and e.winfo_exists():
            self.lg["user_code"] = "".join(e.get().split())
        if not self.lg["user_code"]:
            self._conta_ir("erro", erro=("user_code", "Digite o User Code do app Tuya Smart."))
            return
        if not self.cl["ja_procurou"]:
            self._procurar_na_rede()              # o casamento usa a varredura
        conta_tuya.relatar("--- login iniciado")
        self._sessao = conta_tuya.Sessao(self.lg["user_code"], queue.Queue()).iniciar()
        self._conta_ir("pedindo", erro=("", ""))

    def _conta_evento(self, ev):
        tipo = ev[0]
        if tipo == "qr":
            _, texto, n, validade = ev
            self._conta_ir("aguardando", qr=texto, qr_n=n, qr_ate=time.monotonic() + validade)
        elif tipo == "buscando":
            self._conta_ir("buscando")
        elif tipo == "erro":
            self._sessao = None
            self._conta_ir("erro", erro=(ev[1], ev[2]))
        elif tipo == "conta":
            self._sessao = None
            self._conta_chegou(ev[1])

    def _conta_chegou(self, conta):
        if self.cl["procurando"]:                 # espera a varredura acabar para casar
            self.lg["conta_pendente"] = conta
            self._conta_ir("buscando")
            return
        modo, r = conta_tuya.casar(conta, self.cl["achados"])
        conta_tuya.relatar(f"casamento: {modo}" + (f" {conta_tuya.fim(r['id'])}" if modo == "um" else ""))
        if modo == "um":
            self._conta_usar(r)
        else:
            self._conta_ir("escolher", conta=r, erro=("", ""))

    def _conta_usar(self, d):
        if self.lg["estado"] not in ("buscando", "escolher"):
            return
        motivo = conta_tuya.chave_valida(d.get("chave", ""))
        if motivo:
            conta_tuya.relatar(f"chave recusada {conta_tuya.fim(d['id'])}: {conta_tuya.mascarar(d.get('chave', ''))}")
            self._conta_ir("escolher", conta=self.lg["conta"] or [d], erro=("chave", motivo))
            return
        ip = d.get("ip") or self._ip_da_rede(d["id"]) or (self.cfg["ip"] if d["id"] == self.cfg["id"] else "")
        if not self.cfg.definir_lampada(d["id"], d["chave"], d.get("nome", ""), ip):
            self._conta_ir("erro", erro=("gravar", "Não consegui gravar na pasta do programa. "
                                                 "Ela está protegida contra escrita?"))
            return
        conta_tuya.relatar(f"gravado: id {conta_tuya.fim(d['id'])}, chave {conta_tuya.mascarar(d['chave'])}, "
                           f"ip {ip or '(a descobrir)'}")
        log.info("lampada configurada pela conta Tuya: ...%s", d["id"][-6:])
        self.lg.update(user_code="", conta=[])    # nada da sessao fica na memoria
        self._reiniciar_lampada()
        self._conta_ir("gravado", gravado={"id": d["id"], "nome": d.get("nome", ""), "ip": ip})

    def _reiniciar_lampada(self):
        try:
            self.luz.encerrar(espera_s=1.0)
        except Exception:
            pass
        self.luz = self._nova_lampada()
        self._conhece_estado = False
        self._ao_abrir_feito = True              # nao mexer na luz so por ter configurado
        self._dica = None
        self.conexao = L.PROCURANDO
        self.luz.iniciar()
        self._pintar_tudo()

    def _usar_lampada(self, id_, chave, nome, ip, nuvem_=None):
        """Grava a lampada nova e reconecta com ela (o arquivo de origem NAO e
        copiado: dele so interessam dois campos)."""
        if not self.cfg.definir_lampada(id_, chave, nome, ip, nuvem_):
            self.cl["recado"] = "Não consegui gravar na pasta do programa. Ela está protegida contra escrita?"
            self.ir("lampada")
            return
        log.info("lampada configurada: ...%s", id_[-6:])
        self.cl.update(recado="", valores={})
        self._reiniciar_lampada()
        self.ir("inicio")

    # == acoes na lampada ==================================================
    def _conectada(self) -> bool:
        return self.conexao == L.CONECTADA

    def pedir_ligar(self, ligar: bool, origem="tela") -> bool:
        if not self._conectada():
            return False
        self.luz.ligar(ligar)
        log.info("%s (%s)", "ligar" if ligar else "apagar", origem)
        self.ligada = ligar
        self._depois_do_comando()
        if not ligar:
            self.timer_fim = None
            self._pintar_timer()
        return True

    def pedir_temperatura(self, v: int, origem="tela") -> bool:
        if not self._conectada():
            return False
        v = max(0, min(1000, int(v)))
        self.luz.temperatura(v)                 # acende junto: escolher um tom = quero essa luz
        log.info("temperatura %s (%s)", v, origem)
        self.ligada, self.temp = True, v
        self.cfg["ultima_temperatura"] = v
        self.gravar_depois()
        self._depois_do_comando()
        return True

    def _andar_temperatura(self, passo: int):
        esc = list(kelvin.ESCOLHAS)
        i = min(range(len(esc)), key=lambda k: abs(esc[k] - self.temp))
        self.pedir_temperatura(esc[max(0, min(len(esc) - 1, i + passo))], "atalho")

    def _depois_do_comando(self):
        """Pinta na hora; o eco atrasado nao repinta; 1,6 s depois le o real."""
        self._ignorar_estado_ate = time.monotonic() + CARENCIA_S
        self._pintar_luz()
        self._pintar_bandeja()
        if self._job_sincronizar:
            self.raiz.after_cancel(self._job_sincronizar)
        self._job_sincronizar = self.raiz.after(int(CARENCIA_S * 1000) + 100, self._sincronizar)

    def _sincronizar(self):
        self._job_sincronizar = None
        self.luz.atualizar()

    def executar(self, acao: str, origem: str):
        if acao == "ligar":
            self.pedir_ligar(True, origem)
        elif acao == "desligar":
            self.pedir_ligar(False, origem)
        elif acao.startswith("t:"):
            self.pedir_temperatura(int(acao[2:]), origem)

    def _mudou_timer(self, v):
        self.cfg["timer_minutos"] = v
        self.gravar_depois()

    def _timer_botao(self):
        if self.timer_fim:
            self.timer_fim = None
        else:
            if not self.ligada:
                self.pedir_ligar(True, "timer")
            self.timer_fim = datetime.datetime.now() + datetime.timedelta(minutes=self.p_timer.valor)
            log.info("timer: apaga as %s", self.timer_fim.strftime("%H:%M"))
        self._pintar_timer()

    def gravar_depois(self):
        """Cliques seguidos gravam o config uma vez so (0,8 s depois do ultimo)."""
        if self._job_gravar:
            self.raiz.after_cancel(self._job_gravar)
        self._job_gravar = self.raiz.after(800, self._gravar_agora)

    def _gravar_agora(self):
        self._job_gravar = None
        self.cfg.gravar()

    # == dica de conexao ========================================================
    DICAS = {
        "sem_lampada": ("Falta configurar a lâmpada",
                        "O programa precisa da chave dela uma vez só. Ela vem da sua conta Tuya "
                        "(User Code + QR), colada, ou do devices.json.", "Configurar"),
        "nao_achado": ("Não encontrei a lâmpada",
                       "• Ela está ligada no interruptor?\n• O PC está na mesma rede (mesmo roteador)?\n"
                       "O programa continua procurando sozinho.", "Procurar agora"),
        "sem_resposta": ("A lâmpada não respondeu",
                         "Ela aparece na rede mas recusou a conversa. Se foi pareada de novo no app do "
                         "celular, a chave mudou: Opções › Atualizar chave.", "Tentar agora"),
    }

    def _calcular_dica(self, estado, detalhe):
        if estado in (L.CONECTADA, L.PARADO):
            return None
        if estado == L.SEM_LAMPADA:
            return "sem_lampada"
        if estado == L.ERRO:
            return "nao_achado" if "nao encontrada" in (detalhe or "") else "sem_resposta"
        return self._dica                       # procurando: mantem a que estava (sem piscar)

    def _pintar_dica(self):
        if not hasattr(self, "p_dica"):
            return
        d = self._dica
        if d:
            tit, txt, botao = self.DICAS[d]
            if self.l_dica_tit.cget("text") != tit:
                self.l_dica_tit.config(text=tit)
                self.l_dica_txt.config(text=txt)
                self.b_dica.definir(texto=botao, ms=0)
        if bool(d) != self._dica_visivel:
            # a dica ocupa o lugar da luz (que sem conexao nao serve); a janela nao muda de tamanho
            self._dica_visivel = bool(d)
            if d:
                self.f_controles.pack_forget()
                self.p_dica.pack(fill="both", expand=True)
            else:
                self.p_dica.pack_forget()
                self.f_controles.pack(fill="both", expand=True)
                self.l_dica_tit.config(text="")
            self._medir()

    def _dica_botao(self):
        if self._dica == "sem_lampada":
            self.ir("lampada")
        else:
            self.luz.procurar_agora()

    # == pintura do estado =====================================================
    def _pintar_tudo(self):
        self._pintar_indicador()
        self._pintar_luz()
        self._pintar_timer()
        self._pintar_dica()
        self._pintar_bandeja()

    def _pintar_luz(self):
        if not hasattr(self, "b_ligar"):
            return
        ok = self._conectada() and self._conhece_estado   # sem conexao nao ha estado real para mostrar
        # o botao do estado atual diz o estado (pedido dele, 11/out/2026): Ligada / Apagada
        self.b_ligar.definir(selecionado=ok and self.ligada, habilitado=self._conectada(),
                             texto="Ligada" if ok and self.ligada else "Ligar")
        self.b_apagar.definir(selecionado=ok and not self.ligada, habilitado=self._conectada(),
                              texto="Apagada" if ok and not self.ligada else "Apagar")
        for v, b in self.b_temp.items():
            b.definir(selecionado=ok and self.ligada and v == self.temp, habilitado=self._conectada())
        if not ok:
            self.l_luz.config(text="")
            self.l_temp.config(text="")
        else:
            self.l_luz.config(text="acesa" if self.ligada else "apagada")
            self.l_temp.config(text=fmt_k(self.temp) if self.ligada else "")

    def _pintar_timer(self):
        if not hasattr(self, "b_timer"):
            return
        if self.timer_fim:
            self.l_timer.config(text="apaga às " + self.timer_fim.strftime("%H:%M"))
            self.b_timer.definir(texto="Cancelar", selecionado=True)
        else:
            self.l_timer.config(text="")
            self.b_timer.definir(texto="Iniciar", selecionado=False)
        self.b_timer.definir(habilitado=self._conectada() or bool(self.timer_fim))

    def _pintar_bandeja(self):
        acesa = self._conectada() and self._conhece_estado and self.ligada
        self.bandeja.definir_cor(kelvin.cor(self.temp) if acesa else None)
        if not self._conectada():
            txt = ESTADOS.get(self.conexao, "")
        else:
            txt = f"acesa, {fmt_k(self.temp)}" if self.ligada else "apagada"
        self.bandeja.definir_titulo(f"LightWireless: {txt}")

    # == laco ===================================================================
    def _ciclo(self):
        try:
            self._ciclo_dentro()
        except Exception:
            log.exception("erro no laco da janela")
        self.raiz.after(50, self._ciclo)

    def _ciclo_dentro(self):
        while True:
            try:
                ev = self._eventos.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "conexao":
                _, est, det = ev
                self.conexao = est
                if est == L.CONECTADA:
                    self.detalhe = det
                else:
                    self._conhece_estado = False
                self._dica = self._calcular_dica(est, det)
                self._pintar_tudo()
            elif ev[0] == "estado":
                self._chegou_estado(ev[1])
        while True:
            try:
                ev = self._busca.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "tentativa":
                self.cl["tentativa"] = (ev[1], ev[2])
                if self._tela == "lampada" and ev[1] > 1:
                    self.ir("lampada")
            elif ev[0] == "achados":
                self.cl.update(procurando=False, achados=ev[1])
                if self._tela == "lampada":
                    self.ir("lampada")
                pend, self.lg["conta_pendente"] = self.lg["conta_pendente"], None
                if pend is not None and self.lg["estado"] == "buscando":
                    self._conta_chegou(pend)
            elif ev[0] == "nuvem":
                self._nuvem_chegou(ev[1], ev[2])
        # cada sessao tem a sua fila: a de uma sessao cancelada nunca mais e lida
        while self._sessao is not None:
            try:
                ev = self._sessao.eventos.get_nowait()
            except queue.Empty:
                break
            self._conta_evento(ev)
        if self._volta % 5 == 0:
            self._conta_relogio()
        agora = datetime.datetime.now()
        if self.timer_fim and agora >= self.timer_fim:
            self.timer_fim = None
            self.pedir_ligar(False, "timer")
            self._pintar_timer()
        for p in self.bandeja.pedidos():
            if p == "mostrar":
                self.mostrar()
            elif p == "alternar":
                self.pedir_ligar(not self.ligada, "bandeja")
        for a in self.motor.pedidos():
            self._atalho(a)
        self._volta += 1
        if self._volta >= 100:            # ~5 s
            self._volta = 0
            for it in self.agenda.vencidos(self.cfg["agenda"]):
                log.info("agenda %s: %s", it["hora"], it["acao"])
                self.executar(it["acao"], "agenda")
        if self._volta % 10 == 0 and PEDIDO_MOSTRAR.exists():
            try:
                os.remove(PEDIDO_MOSTRAR)
            except OSError:
                pass
            self.mostrar()
        if self._volta % 10 == 5 and PEDIDO_SAIR.exists():
            try:
                os.remove(PEDIDO_SAIR)
            except OSError:
                pass
            log.info("pedido de saida (novo build)")
            self.sair(acao_de_saida=False)

    def _chegou_estado(self, dps: dict):
        if not self._ao_abrir_feito:
            self._ao_abrir_feito = True
            a = self.cfg.opcoes.get("ao_abrir", "nada")
            ja = bool(dps.get(L.DP_LIGADO))
            if a == "ligar" and ja:
                a = "nada"                     # ja estava acesa: nao mexe no que esta bom
            if a != "nada":
                self.raiz.after(300, lambda a=a: self.executar(a, "ao abrir"))
        if time.monotonic() < self._ignorar_estado_ate and self._conhece_estado:
            return
        self.ligada = bool(dps.get(L.DP_LIGADO))
        if dps.get(L.DP_MODO, "white") == "white" and dps.get(L.DP_TEMPERATURA) is not None:
            self.temp = int(dps.get(L.DP_TEMPERATURA))
        self._conhece_estado = True
        self._pintar_luz()
        self._pintar_bandeja()

    def _atalho(self, a):
        if a == "alternar":
            self.pedir_ligar(not self.ligada, "atalho")
        elif a == "forcar_ligar":
            self.pedir_ligar(True, "atalho")
        elif a == "forcar_desligar":
            self.pedir_ligar(False, "atalho")
        elif a == "temperatura_anterior":
            self._andar_temperatura(-1)
        elif a == "temperatura_seguinte":
            self._andar_temperatura(+1)
        elif a == "mostrar_janela":
            if self.raiz.state() == "withdrawn":
                self.mostrar()
            else:
                self.esconder()

    def _tecla_janela(self, e):
        if self._capturando or isinstance(e.widget, tk.Entry):
            return
        if e.keysym == "Escape" and self._tela != "inicio":
            self.ir(getattr(self, "_voltar_para", "inicio"))
        elif self._tela == "inicio" and e.char:
            if e.char == "1":
                self.pedir_ligar(True, "teclado")
            elif e.char == "0":
                self.pedir_ligar(False, "teclado")
            elif e.char in "+=":
                self._andar_temperatura(+1)
            elif e.char in "-_":
                self._andar_temperatura(-1)

    # == janela ===================================================================
    def _posicionar(self):
        j = self.cfg["janela"]
        w, h = E.px(LARGURA), E.px(ALTURA)
        if j.get("x") is not None:
            # area de TODOS os monitores: monitor desligado/trocado nao deixa a janela perdida fora
            try:
                import ctypes
                gm = ctypes.windll.user32.GetSystemMetrics
                vx, vy, vw, vh = gm(76), gm(77), gm(78), gm(79)
            except Exception:
                vx, vy, vw, vh = 0, 0, self.raiz.winfo_screenwidth(), self.raiz.winfo_screenheight()
            x = max(vx, min(j["x"], vx + vw - w))
            y = max(vy, min(j["y"], vy + vh - h))
            self.raiz.geometry(f"{w}x{h}+{x}+{y}")
        else:
            moldura.centralizar(self.raiz, w, h)

    def _guardar_posicao(self):
        self.cfg["janela"] = {"x": self.raiz.winfo_x(), "y": self.raiz.winfo_y()}
        self.cfg.gravar()

    def mostrar(self):
        r = self.raiz
        r.deiconify()
        if not self._ja_mostrou:
            self._ja_mostrou = True
            moldura.fixar_barra_de_tarefas(r)
        r.lift()
        r.focus_force()

    def esconder(self):
        if self.bandeja.disponivel:
            self.raiz.withdraw()
        else:
            moldura.minimizar(self.raiz)

    def sair(self, acao_de_saida: bool = True):
        log.info("saindo")
        self._conta_cancelar()                   # login em curso: a thread larga sozinha
        try:
            self.raiz.withdraw()                 # a janela some na hora; a despedida vem depois
        except Exception:
            pass
        if self._job_gravar:                     # nada de config pendente perdido
            self.raiz.after_cancel(self._job_gravar)
            self._gravar_agora()
        a = self.cfg.opcoes.get("ao_sair", "nada") if acao_de_saida else "nada"
        if a != "nada" and self._conectada():
            # direto na lampada (a janela ja esta indo embora): entra na fila ANTES do "sair" e e executado
            if a == "ligar":
                self.luz.ligar(True)
            elif a == "desligar":
                self.luz.ligar(False)
            elif a.startswith("t:"):
                self.luz.temperatura(max(0, min(1000, int(a[2:]))))
            log.info("ao sair: %s enviado", a)
        try:
            self.motor.encerrar()
            self.bandeja.encerrar()
        finally:
            self.raiz.after(50, self.raiz.destroy)

    def encerrar_lampada(self):
        try:
            self.luz.encerrar()
        except Exception:
            pass

    def rodar(self):
        self.raiz.mainloop()


def quebrar(caixa: tk.Widget, *rotulos: tk.Label):
    """Texto de largura livre QUEBRA linha na largura da caixa em vez de ser cortado."""
    def ajustar(e):
        for r in rotulos:
            if int(r.cget("wraplength") or 0) != e.width:
                r.configure(wraplength=max(20, e.width))
    caixa.bind("<Configure>", ajustar, add="+")


def livre_no_windows(teclas: str, atual: str = "") -> bool:
    """O Windows deixa registrar esta combinacao AGORA? (11/out/2026: no PC dele
    Ctrl+Alt+; e Ctrl+Alt+' ja eram de outro programa -- o atalho seria gravado
    e ficaria vermelho.) Registra e solta na hora. A combinacao que ESTA acao ja
    usa e nossa mesma: nao conta como ocupada."""
    if not teclas or teclas.strip().lower() == (atual or "").strip().lower():
        return True
    try:
        import ctypes
        from .motor_atalhos import traduzir
        par = traduzir(teclas)
        if par is None:
            return True                       # quem recusa formato e o porque_nao
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 0xBFFE, par[0], par[1]):
            return False
        u.UnregisterHotKey(None, 0xBFFE)
    except Exception:
        log.debug("teste de atalho livre falhou", exc_info=True)
    return True


def desenhar_qr(cv: tk.Canvas, texto: str, lado: int) -> bool:
    """QR desenhado no Canvas a partir da matriz (segno, sem dependencias).
    Modulo de tamanho INTEIRO (bordas nitidas, leitura facil), centralizado."""
    try:
        import segno
        m = segno.make(texto, error="m", micro=False).matrix
    except Exception:
        log.exception("QR nao desenhado")
        cv.create_text(lado // 2, lado // 2, text="QR indisponível", fill="#000000")
        return False
    borda = 3                                 # zona quieta (o fundo do canvas ja e branco)
    n = len(m) + 2 * borda
    mod = max(1, lado // n)
    x0 = y0 = (lado - mod * n) // 2 + borda * mod
    for y, linha in enumerate(m):
        x = 0
        while x < len(linha):                 # junta modulos pretos seguidos num retangulo so
            if linha[x]:
                ini = x
                while x < len(linha) and linha[x]:
                    x += 1
                cv.create_rectangle(x0 + ini * mod, y0 + y * mod, x0 + x * mod, y0 + (y + 1) * mod,
                                    fill="#000000", outline="", width=0)
            else:
                x += 1
    return True


def congelar(janela: tk.Tk, sim: bool):
    """WM_SETREDRAW na janela de cima: monta tudo e pinta de uma vez (so com a
    janela ja mapeada; antes disso abria "minimizada", bug de 10/out/2026)."""
    if not janela.winfo_ismapped():
        return
    try:
        import ctypes
        h = ctypes.windll.user32.GetParent(janela.winfo_id())
        ctypes.windll.user32.SendMessageW(h, 0x000B, 0 if sim else 1, 0)
        if not sim:
            ctypes.windll.user32.RedrawWindow(h, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)
    except Exception:
        pass
